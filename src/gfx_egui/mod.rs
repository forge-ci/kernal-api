//! Private egui backend for [`crate::gfx`]. Every egui, eframe, wgpu and
//! winit name stays inside this module tree.

pub(crate) mod input;
mod offscreen;

pub(crate) use offscreen::{block_on, read_back, snapshot};

use std::ops::RangeInclusive;
use std::sync::{Arc, Mutex, PoisonError};

use crate::gfx::{
    App, BackendKind, Capabilities, Color, Event, GfxError, GfxErrorKind, Placement, Support,
    Theme, ThemeMode, WindowOptions,
};

pub(crate) fn capabilities() -> Capabilities {
    Capabilities {
        backend: BackendKind::Egui,
        // Each depends on the compositor / window manager; winit forwards
        // the request and a host may ignore it (see docs/gfx-backend.md).
        transparent: Support::BestEffort,
        click_through: Support::BestEffort,
        always_on_top: Support::BestEffort,
        undecorated: Support::Supported,
        absolute_placement: Support::BestEffort,
        // eframe creates the window before monitors can be enumerated.
        monitor_placement: Support::Unsupported,
        // A layer-shell surface on Linux Wayland, probed once per process;
        // see docs/gfx-backend.md, "Docks".
        dock: crate::native_gfx_dock::support(),
    }
}

pub(crate) fn run(options: WindowOptions, app: Box<dyn App>) -> Result<(), GfxError> {
    if let Placement::Dock { edge, thickness } = options.placement {
        // winit creates every Wayland window as an xdg_toplevel, a role a
        // surface can never trade for the layer-shell role a dock needs, so
        // a dock is a separate surface path that does not go through eframe.
        return crate::native_gfx_dock::run(&options, edge, thickness, app);
    }
    let native = native_options(&options);
    let title = options.title.clone();
    let theme = options.theme;
    let transparent = options.transparent;
    eframe::run_native(
        &title,
        native,
        Box::new(move |cc| {
            apply_theme(&cc.egui_ctx, theme);
            Ok(Box::new(Adapter {
                app,
                theme,
                pending_theme: None,
                transparent,
                events: Vec::new(),
                screenshots: Arc::default(),
            }))
        }),
    )
    .map_err(|error| GfxError::new(GfxErrorKind::Backend, error.to_string()))
}

fn native_options(options: &WindowOptions) -> eframe::NativeOptions {
    let (width, height) = options.size;
    let mut viewport = egui::ViewportBuilder::default()
        .with_title(options.title.clone())
        .with_inner_size([width, height])
        .with_resizable(options.resizable)
        .with_decorations(options.decorations)
        .with_transparent(options.transparent)
        .with_mouse_passthrough(options.click_through);
    if options.always_on_top {
        viewport = viewport.with_window_level(egui::WindowLevel::AlwaysOnTop);
    }
    let mut centered = false;
    match options.placement {
        Placement::At { x, y } => viewport = viewport.with_position([x, y]),
        Placement::Centered => centered = true,
        // Rejected by `WindowOptions::validate` before reaching here.
        Placement::Default | Placement::Monitor { .. } | Placement::Dock { .. } => {}
    }
    eframe::NativeOptions {
        viewport,
        centered,
        renderer: eframe::Renderer::Wgpu,
        ..eframe::NativeOptions::default()
    }
}

pub(crate) fn color32(color: Color) -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(color.r, color.g, color.b, color.a)
}

pub(crate) fn apply_theme(ctx: &egui::Context, theme: Theme) {
    let mut visuals = match theme.mode {
        ThemeMode::Light => egui::Visuals::light(),
        ThemeMode::Dark => egui::Visuals::dark(),
    };
    let accent = color32(theme.accent);
    visuals.selection.bg_fill = accent;
    visuals.widgets.active.bg_fill = accent;
    visuals.hyperlink_color = accent;
    if let Some(background) = theme.background {
        visuals.panel_fill = color32(background);
        visuals.window_fill = color32(background);
    }
    let radius = egui::CornerRadius::same(corner(theme.corner_radius));
    for widget in [
        &mut visuals.widgets.noninteractive,
        &mut visuals.widgets.inactive,
        &mut visuals.widgets.hovered,
        &mut visuals.widgets.active,
        &mut visuals.widgets.open,
    ] {
        widget.corner_radius = radius;
    }
    ctx.set_visuals(visuals);
    ctx.global_style_mut(|style| {
        style.spacing.item_spacing = egui::vec2(theme.spacing, theme.spacing);
        for (text_style, font) in &mut style.text_styles {
            font.size = match text_style {
                egui::TextStyle::Heading => theme.text_size * 1.5,
                egui::TextStyle::Small => theme.text_size * 0.75,
                _ => theme.text_size,
            };
        }
    });
}

/// Vertical gap between list rows: half the theme spacing.
fn list_spacing(spacing: f32) -> f32 {
    (spacing / 2.0).max(0.0)
}

/// Clamp a token radius into egui's `u8` corner radius.
fn corner(radius: f32) -> u8 {
    if radius.is_nan() || radius <= 0.0 {
        0
    } else if radius >= f32::from(u8::MAX) {
        u8::MAX
    } else {
        // In range and non-negative after the checks above.
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss, reason = "range checked")]
        let value = radius.round() as u8;
        value
    }
}

struct Adapter {
    app: Box<dyn App>,
    theme: Theme,
    pending_theme: Option<Theme>,
    transparent: bool,
    events: Vec<Event>,
    /// Captures completed by the renderer, delivered as events next frame.
    screenshots: Arc<Mutex<Vec<Event>>>,
}

impl eframe::App for Adapter {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        if let Some(theme) = self.pending_theme.take() {
            self.theme = theme;
            apply_theme(&ctx, theme);
        }
        self.events.clear();
        ctx.input(|input| input::translate(&input.events, &mut self.events));
        self.events.append(
            &mut self
                .screenshots
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        if ctx.input(|input| input.viewport().close_requested()) && !self.app.close_requested() {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
        }
        let mut shared = Shared {
            events: std::mem::take(&mut self.events),
            theme: None,
            close: false,
            screenshot: false,
        };
        draw(ui, self.transparent, &mut *self.app, &mut shared);
        self.events = shared.events;
        self.pending_theme = shared.theme;
        if self.pending_theme.is_some() {
            ctx.request_repaint();
        }
        if shared.screenshot {
            let sink = Arc::clone(&self.screenshots);
            let waker = ctx.clone();
            ctx.request_screenshot(move |image| {
                sink.lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .push(screenshot_event(&image));
                waker.request_repaint();
            });
            ctx.request_repaint();
        }
        if shared.close {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }

    fn clear_color(&self, visuals: &egui::Visuals) -> [f32; 4] {
        if self.transparent {
            [0.0; 4]
        } else {
            visuals.panel_fill.to_normalized_gamma_f32()
        }
    }
}

/// Lay out one frame of `app` in a central panel.
pub(crate) fn draw(ui: &mut egui::Ui, transparent: bool, app: &mut dyn App, shared: &mut Shared) {
    let panel = egui::CentralPanel::default();
    let panel = if transparent {
        panel.frame(egui::Frame::NONE)
    } else {
        panel
    };
    panel.show(ui, |ui| {
        let mut frame = crate::gfx::Frame {
            imp: FrameImp { ui, shared },
        };
        app.frame(&mut frame);
    });
}

/// What one frame of app code asked of the backend.
pub(crate) struct Shared {
    pub(crate) events: Vec<Event>,
    pub(crate) theme: Option<Theme>,
    pub(crate) close: bool,
    pub(crate) screenshot: bool,
}

impl Shared {
    pub(crate) fn new(events: Vec<Event>) -> Self {
        Self {
            events,
            theme: None,
            close: false,
            screenshot: false,
        }
    }
}

/// A captured frame as a neutral event: straight-alpha sRGB bytes.
pub(crate) fn screenshot_event(image: &egui::ColorImage) -> Event {
    let [width, height] = image.size;
    let rgba: Vec<u8> = image
        .pixels
        .iter()
        .flat_map(egui::Color32::to_srgba_unmultiplied)
        .collect();
    Event::Screenshot {
        width: u32::try_from(width).unwrap_or(u32::MAX),
        height: u32::try_from(height).unwrap_or(u32::MAX),
        rgba: rgba.into(),
    }
}

pub(crate) struct FrameImp<'a> {
    ui: &'a mut egui::Ui,
    shared: &'a mut Shared,
}

impl FrameImp<'_> {
    pub(crate) fn events(&self) -> &[Event] {
        &self.shared.events
    }

    pub(crate) fn label(&mut self, text: &str) {
        self.ui.label(text);
    }

    pub(crate) fn heading(&mut self, text: &str) {
        self.ui.heading(text);
    }

    pub(crate) fn button(&mut self, text: &str) -> bool {
        self.ui.button(text).clicked()
    }

    pub(crate) fn toggle(&mut self, value: &mut bool, text: &str) -> bool {
        self.ui.checkbox(value, text).changed()
    }

    pub(crate) fn text_field(&mut self, value: &mut String) -> bool {
        let response = self.ui.text_edit_singleline(value);
        if self.ui.ctx().memory(|memory| memory.focused().is_none()) {
            response.request_focus();
        }
        response.changed()
    }

    pub(crate) fn slider(&mut self, value: &mut f64, range: RangeInclusive<f64>, text: &str) -> bool {
        self.ui
            .add(egui::Slider::new(value, range).text(text))
            .changed()
    }

    pub(crate) fn separator(&mut self) {
        self.ui.separator();
    }

    pub(crate) fn space(&mut self, amount: f32) {
        self.ui.add_space(amount);
    }

    pub(crate) fn horizontal(&mut self, body: &mut dyn FnMut(FrameImp<'_>)) {
        let shared = &mut *self.shared;
        self.ui.horizontal(|ui| body(FrameImp { ui, shared }));
    }

    pub(crate) fn scroll(&mut self, height: f32, body: &mut dyn FnMut(FrameImp<'_>)) {
        let shared = &mut *self.shared;
        egui::ScrollArea::vertical()
            .max_height(height.max(0.0))
            .min_scrolled_height(height.max(0.0))
            .auto_shrink([false, false])
            .show(self.ui, |ui| body(FrameImp { ui, shared }));
    }

    pub(crate) fn list(&mut self, body: &mut dyn FnMut(FrameImp<'_>)) {
        let shared = &mut *self.shared;
        self.ui.vertical(|ui| {
            let spacing = ui.spacing().item_spacing.y;
            ui.spacing_mut().item_spacing.y = list_spacing(spacing);
            body(FrameImp { ui, shared });
        });
    }

    pub(crate) fn selectable_row(&mut self, text: &str, selected: bool) -> bool {
        let width = self.ui.available_width();
        let row = egui::Button::selectable(selected, text)
            .min_size(egui::vec2(width, 0.0))
            .frame_when_inactive(selected);
        let response = self.ui.add(row);
        // Scroll into view only on the transition to selected, so a wheel
        // scroll away from a still-selected row is not undone next frame.
        let was_selected = self.ui.ctx().data_mut(|data| {
            let was = data.get_temp::<bool>(response.id).unwrap_or(false);
            data.insert_temp(response.id, selected);
            was
        });
        if selected && !was_selected {
            response.scroll_to_me(None);
        }
        response.clicked()
    }

    pub(crate) fn set_theme(&mut self, theme: Theme) {
        self.shared.theme = Some(theme);
    }

    pub(crate) fn close(&mut self) {
        self.shared.close = true;
    }

    pub(crate) fn request_repaint(&mut self) {
        self.ui.ctx().request_repaint();
    }

    pub(crate) fn request_screenshot(&mut self) {
        self.shared.screenshot = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Run one headless frame (no window, no GPU) over `events`.
    fn frame(
        ctx: &egui::Context,
        events: Vec<egui::Event>,
        mut body: impl FnMut(&mut crate::gfx::Frame<'_>),
    ) {
        let input = egui::RawInput {
            screen_rect: Some(egui::Rect::from_min_size(
                egui::Pos2::ZERO,
                egui::vec2(400.0, 300.0),
            )),
            events,
            ..egui::RawInput::default()
        };
        let mut output = ctx.run_ui(input, |ui| {
            let mut shared = Shared {
                events: Vec::new(),
                theme: None,
                close: false,
                screenshot: false,
            };
            let mut frame = crate::gfx::Frame {
                imp: FrameImp {
                    ui,
                    shared: &mut shared,
                },
            };
            body(&mut frame);
        });
        // Headless: there is no renderer to upload font textures to.
        output.textures_delta.clear();
    }

    fn click(pos: egui::Pos2, pressed: bool) -> egui::Event {
        egui::Event::PointerButton {
            pos,
            button: egui::PointerButton::Primary,
            pressed,
            modifiers: egui::Modifiers::NONE,
        }
    }

    #[test]
    fn screenshot_is_straight_alpha_rgba() {
        let image = egui::ColorImage::new(
            [2, 1],
            vec![
                egui::Color32::from_rgba_unmultiplied(255, 0, 0, 255),
                egui::Color32::from_rgba_unmultiplied(0, 0, 255, 128),
            ],
        );
        let Event::Screenshot {
            width,
            height,
            rgba,
        } = screenshot_event(&image)
        else {
            panic!("not a screenshot event");
        };
        assert_eq!((width, height), (2, 1));
        assert_eq!(rgba.len(), 8);
        assert_eq!(&rgba[..4], &[255, 0, 0, 255]);
        assert_eq!(rgba[7], 128);
        assert!(rgba[6] > 240, "blue un-premultiplied: {rgba:?}");
    }

    #[test]
    fn list_spacing_is_half_the_theme_spacing() {
        assert!((list_spacing(8.0) - 4.0).abs() < f32::EPSILON);
        assert!(list_spacing(-2.0).abs() < f32::EPSILON);
    }

    #[test]
    fn scroll_clips_tall_content_to_the_region() {
        let ctx = egui::Context::default();
        let mut outer = egui::Rect::NOTHING;
        let mut inner_bottom = 0.0;
        for _ in 0..2 {
            frame(&ctx, Vec::new(), |f| {
                let top = f.imp.ui.cursor().top();
                f.scroll(100.0, |f| {
                    for i in 0..50 {
                        f.selectable_row(&format!("row {i}"), false);
                    }
                    inner_bottom = f.imp.ui.min_rect().bottom();
                });
                outer = egui::Rect::from_x_y_ranges(0.0..=1.0, top..=f.imp.ui.cursor().top());
            });
        }
        assert!(inner_bottom - outer.top() > 500.0, "content is taller than the region");
        let used = outer.height();
        assert!((100.0..120.0).contains(&used), "region takes ~100 px, took {used}");
    }

    #[test]
    fn selectable_row_reports_a_click() {
        let ctx = egui::Context::default();
        let rect = std::cell::Cell::new(egui::Rect::NOTHING);
        let mut clicked = Vec::new();
        let mut draw = |events| {
            frame(&ctx, events, |f| {
                let hit = f.list(|f| {
                    let first = f.selectable_row("first", true);
                    rect.set(f.imp.ui.min_rect());
                    let second = f.selectable_row("second", false);
                    (first, second)
                });
                clicked.push(hit);
            });
        };
        draw(Vec::new());
        let first = rect.get();
        let target = egui::pos2(first.center().x, first.top() + 4.0);
        draw(vec![egui::Event::PointerMoved(target)]);
        draw(vec![click(target, true)]);
        draw(vec![click(target, false)]);
        assert!(clicked.iter().any(|&(first, _)| first), "first row clicked: {clicked:?}");
        assert!(clicked.iter().all(|&(_, second)| !second), "second row untouched: {clicked:?}");
    }

    #[test]
    fn text_field_takes_focus_when_nothing_has_it() {
        let ctx = egui::Context::default();
        let mut query = String::new();
        frame(&ctx, Vec::new(), |f| {
            f.text_field(&mut query);
        });
        frame(&ctx, vec![egui::Event::Text("fi".into())], |f| {
            f.text_field(&mut query);
        });
        assert_eq!(query, "fi");
    }
}
