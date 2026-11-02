//! Private egui backend for [`crate::gfx`]. Every egui, eframe, wgpu and
//! winit name stays inside this module tree.

mod input;

use std::ops::RangeInclusive;

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
    }
}

pub(crate) fn run(options: WindowOptions, app: Box<dyn App>) -> Result<(), GfxError> {
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
        Placement::Default | Placement::Monitor { .. } => {}
    }
    eframe::NativeOptions {
        viewport,
        centered,
        renderer: eframe::Renderer::Wgpu,
        ..eframe::NativeOptions::default()
    }
}

fn color32(color: Color) -> egui::Color32 {
    egui::Color32::from_rgba_unmultiplied(color.r, color.g, color.b, color.a)
}

fn apply_theme(ctx: &egui::Context, theme: Theme) {
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
        if ctx.input(|input| input.viewport().close_requested()) && !self.app.close_requested() {
            ctx.send_viewport_cmd(egui::ViewportCommand::CancelClose);
        }
        let mut shared = Shared {
            events: std::mem::take(&mut self.events),
            theme: None,
            close: false,
        };
        let panel = egui::CentralPanel::default();
        let panel = if self.transparent {
            panel.frame(egui::Frame::NONE)
        } else {
            panel
        };
        let app = &mut self.app;
        panel.show(ui, |ui| {
            let mut frame = crate::gfx::Frame {
                imp: FrameImp {
                    ui,
                    shared: &mut shared,
                },
            };
            app.frame(&mut frame);
        });
        self.events = shared.events;
        self.pending_theme = shared.theme;
        if self.pending_theme.is_some() {
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

struct Shared {
    events: Vec<Event>,
    theme: Option<Theme>,
    close: bool,
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
        self.ui.text_edit_singleline(value).changed()
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

    pub(crate) fn set_theme(&mut self, theme: Theme) {
        self.shared.theme = Some(theme);
    }

    pub(crate) fn close(&mut self) {
        self.shared.close = true;
    }

    pub(crate) fn request_repaint(&mut self) {
        self.ui.ctx().request_repaint();
    }
}
