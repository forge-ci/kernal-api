//! `gfx` docks on Linux: a wlr-layer-shell surface drawn by egui-wgpu.
//!
//! eframe cannot host a dock. winit 0.30 creates every Wayland window through
//! `xdg_shell.create_window` (an `xdg_toplevel`), and a `wl_surface` that has
//! taken the xdg role can never take the layer-shell role, which is what
//! makes the compositor anchor a surface to an edge and reserve an exclusive
//! zone. So this module is a second surface path that never touches winit:
//! smithay-client-toolkit owns its own Wayland connection and layer surface,
//! wgpu renders into that surface through raw handles, and the frame itself
//! is the same `draw` / `FrameImp` the eframe path uses, so an `App` cannot
//! tell which path it runs on. X11 and non-layer-shell compositors (GNOME's
//! Mutter) report the capability as unsupported, with the reason in the
//! `run` error.

use std::ptr::NonNull;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use raw_window_handle::{
    RawDisplayHandle, RawWindowHandle, WaylandDisplayHandle, WaylandWindowHandle,
};
use smithay_client_toolkit::compositor::{CompositorHandler, CompositorState};
use smithay_client_toolkit::output::{OutputHandler, OutputState};
use smithay_client_toolkit::reexports::calloop::EventLoop;
use smithay_client_toolkit::reexports::calloop_wayland_source::WaylandSource;
use smithay_client_toolkit::reexports::client::globals::{
    registry_queue_init, GlobalList, GlobalListContents,
};
use smithay_client_toolkit::reexports::client::protocol::{
    wl_output, wl_pointer, wl_registry, wl_seat, wl_surface,
};
use smithay_client_toolkit::reexports::client::{Connection, Dispatch, Proxy, QueueHandle};
use smithay_client_toolkit::registry::{ProvidesRegistryState, RegistryState};
use smithay_client_toolkit::seat::pointer::{PointerEvent, PointerEventKind, PointerHandler};
use smithay_client_toolkit::seat::{Capability, SeatHandler, SeatState};
use smithay_client_toolkit::shell::wlr_layer::{
    Anchor, KeyboardInteractivity, Layer, LayerShell, LayerShellHandler, LayerSurface,
    LayerSurfaceConfigure,
};
use smithay_client_toolkit::shell::WaylandSurface;
use smithay_client_toolkit::{
    delegate_compositor, delegate_layer, delegate_output, delegate_pointer, delegate_registry,
    delegate_seat, registry_handlers,
};

use crate::gfx::{App, Edge, Event, GfxError, GfxErrorKind, Support, Theme, WindowOptions};
use crate::gfx_egui::{
    apply_theme, block_on, draw, input, read_back, screenshot_event, Shared,
};

/// The layer-shell global a dock needs.
const LAYER_SHELL: &str = "zwlr_layer_shell_v1";

/// Longest sleep between frames when the app asked for none: the loop still
/// wakes to notice a closed connection.
const IDLE: Duration = Duration::from_secs(1);

pub(crate) fn support() -> Support {
    if unavailable().is_none() {
        Support::Supported
    } else {
        Support::Unsupported
    }
}

/// Why this session cannot show a dock, probed once per process.
fn unavailable() -> Option<&'static str> {
    static PROBE: OnceLock<Option<String>> = OnceLock::new();
    PROBE.get_or_init(probe).as_deref()
}

struct Probe;

impl Dispatch<wl_registry::WlRegistry, GlobalListContents> for Probe {
    fn event(
        _: &mut Self,
        _: &wl_registry::WlRegistry,
        _: wl_registry::Event,
        _: &GlobalListContents,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

fn probe() -> Option<String> {
    let Ok(conn) = Connection::connect_to_env() else {
        return Some("not a Wayland session (no reachable WAYLAND_DISPLAY)".to_owned());
    };
    let globals = match registry_queue_init::<Probe>(&conn) {
        Ok((globals, _queue)) => globals,
        Err(error) => return Some(format!("Wayland registry unavailable: {error}")),
    };
    if has_layer_shell(&globals) {
        None
    } else {
        Some(format!(
            "the compositor does not offer {LAYER_SHELL} (GNOME's Mutter does not; KWin and wlroots compositors do)"
        ))
    }
}

fn has_layer_shell(globals: &GlobalList) -> bool {
    globals.contents().with_list(|list| {
        list.iter()
            .any(|global| global.interface == LAYER_SHELL)
    })
}

fn backend_error(error: impl std::fmt::Display) -> GfxError {
    GfxError::new(GfxErrorKind::Backend, error.to_string())
}

pub(crate) fn run(
    options: &WindowOptions,
    edge: Edge,
    thickness: f32,
    app: Box<dyn App>,
) -> Result<(), GfxError> {
    if let Some(reason) = unavailable() {
        return Err(GfxError::new(
            GfxErrorKind::Unsupported,
            format!("Placement::Dock: {reason}"),
        ));
    }
    let conn = Connection::connect_to_env().map_err(backend_error)?;
    let (globals, queue) = registry_queue_init::<Dock>(&conn).map_err(backend_error)?;
    let qh = queue.handle();
    let compositor = CompositorState::bind(&globals, &qh).map_err(backend_error)?;
    let layer_shell = LayerShell::bind(&globals, &qh).map_err(|error| {
        GfxError::new(GfxErrorKind::Unsupported, format!("{LAYER_SHELL}: {error}"))
    })?;

    let surface = compositor.create_surface(&qh);
    let layer = layer_shell.create_layer_surface(
        &qh,
        surface.clone(),
        Layer::Top,
        Some(options.title.clone()),
        None,
    );
    let depth = logical_px(thickness);
    let (anchor, size) = match edge {
        Edge::Top => (Anchor::TOP | Anchor::LEFT | Anchor::RIGHT, (0, depth)),
        Edge::Bottom => (Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT, (0, depth)),
        Edge::Left => (Anchor::LEFT | Anchor::TOP | Anchor::BOTTOM, (depth, 0)),
        Edge::Right => (Anchor::RIGHT | Anchor::TOP | Anchor::BOTTOM, (depth, 0)),
    };
    layer.set_anchor(anchor);
    layer.set_size(size.0, size.1);
    layer.set_exclusive_zone(i32::try_from(depth).unwrap_or(i32::MAX));
    layer.set_keyboard_interactivity(KeyboardInteractivity::None);
    // The first commit carries no buffer: the compositor answers with the
    // configure that gives the edge length.
    layer.commit();

    let gpu = Gpu::new(&conn, &surface, options.transparent)?;
    let ctx = egui::Context::default();
    apply_theme(&ctx, options.theme);

    let mut dock = Dock {
        registry: RegistryState::new(&globals),
        outputs: OutputState::new(&globals, &qh),
        seats: SeatState::new(&globals, &qh),
        _compositor: compositor,
        _layer_shell: layer_shell,
        _layer: layer,
        surface,
        size: (0, 0),
        scale: 1,
        configured: false,
        frame_pending: false,
        closed: false,
        pointer: None,
        egui_events: Vec::new(),
        gpu,
        ctx,
        app,
        transparent: options.transparent,
        events: Vec::new(),
        start: Instant::now(),
        next_frame: Some(Instant::now()),
        resized: false,
        qh: qh.clone(),
        _conn: conn.clone(),
    };

    let mut event_loop: EventLoop<'_, Dock> = EventLoop::try_new().map_err(backend_error)?;
    WaylandSource::new(conn, queue)
        .insert(event_loop.handle())
        .map_err(|error| backend_error(error.error))?;
    while !dock.closed {
        let timeout = dock
            .next_frame
            .map_or(IDLE, |at| at.saturating_duration_since(Instant::now()).min(IDLE));
        event_loop
            .dispatch(Some(timeout), &mut dock)
            .map_err(backend_error)?;
        dock.maybe_draw()?;
    }
    Ok(())
}

/// Logical pixels to a whole, non-zero protocol size.
fn logical_px(value: f32) -> u32 {
    // Validated finite and positive by `WindowOptions::validate`.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss, reason = "clamped")]
    let px = value.round().clamp(1.0, 16384.0) as u32;
    px
}

struct Gpu {
    state: egui_wgpu::RenderState,
    surface: wgpu::Surface<'static>,
    config: wgpu::SurfaceConfiguration,
}

impl Gpu {
    fn new(
        conn: &Connection,
        surface: &wl_surface::WlSurface,
        transparent: bool,
    ) -> Result<Self, GfxError> {
        let display = NonNull::new(conn.backend().display_ptr().cast::<std::ffi::c_void>())
            .ok_or_else(|| backend_error("null wl_display"))?;
        let window = NonNull::new(surface.id().as_ptr().cast::<std::ffi::c_void>())
            .ok_or_else(|| backend_error("null wl_surface"))?;
        let config = egui_wgpu::WgpuConfiguration::default();
        let instance = block_on(config.wgpu_setup.new_instance());
        // SAFETY: both handles come from `conn` and `surface`, which `Dock`
        // owns alongside this `Gpu` and drops after it (see its field order).
        let target = unsafe {
            instance.create_surface_unsafe(wgpu::SurfaceTargetUnsafe::RawHandle {
                raw_display_handle: Some(RawDisplayHandle::Wayland(WaylandDisplayHandle::new(
                    display,
                ))),
                raw_window_handle: RawWindowHandle::Wayland(WaylandWindowHandle::new(window)),
            })
        }
        .map_err(backend_error)?;
        let state = block_on(egui_wgpu::RenderState::create(
            &config,
            &instance,
            Some(&target),
            egui_wgpu::RendererOptions::default(),
        ))
        .map_err(backend_error)?;
        let caps = target.get_capabilities(&state.adapter);
        let alpha_mode = if transparent
            && caps
                .alpha_modes
                .contains(&wgpu::CompositeAlphaMode::PreMultiplied)
        {
            wgpu::CompositeAlphaMode::PreMultiplied
        } else {
            caps.alpha_modes
                .first()
                .copied()
                .unwrap_or(wgpu::CompositeAlphaMode::Auto)
        };
        let mut usage = wgpu::TextureUsages::RENDER_ATTACHMENT;
        if caps.usages.contains(wgpu::TextureUsages::COPY_SRC) {
            // Lets `Frame::request_screenshot` read the presented frame.
            usage |= wgpu::TextureUsages::COPY_SRC;
        }
        let config = wgpu::SurfaceConfiguration {
            usage,
            format: state.target_format,
            alpha_mode,
            view_formats: vec![state.target_format],
            ..target
                .get_default_config(&state.adapter, 1, 1)
                .ok_or_else(|| backend_error("the GPU cannot present to this surface"))?
        };
        Ok(Self {
            state,
            surface: target,
            config,
        })
    }

    fn resize(&mut self, width: u32, height: u32) {
        self.config.width = width.max(1);
        self.config.height = height.max(1);
        self.surface.configure(&self.state.device, &self.config);
    }

    /// Paint `jobs` into `output`, present it, and read it back first when
    /// `capture` is set and the surface allows copies.
    fn paint(
        &self,
        output: wgpu::SurfaceTexture,
        jobs: &[egui::ClippedPrimitive],
        textures: &mut egui::TexturesDelta,
        screen: &egui_wgpu::ScreenDescriptor,
        clear: [f32; 4],
        capture: bool,
    ) -> Result<Option<egui::ColorImage>, GfxError> {
        let state = &self.state;
        let mut renderer = state.renderer.write();
        for (id, deltas) in &textures.set {
            for delta in deltas {
                renderer.update_texture(&state.device, &state.queue, *id, delta);
            }
        }
        let mut encoder = state
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor::default());
        let user_buffers =
            renderer.update_buffers(&state.device, &state.queue, &mut encoder, jobs, screen);
        let view = output
            .texture
            .create_view(&wgpu::TextureViewDescriptor::default());
        {
            let pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("gfx dock"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &view,
                    depth_slice: None,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color {
                            r: f64::from(clear[0]),
                            g: f64::from(clear[1]),
                            b: f64::from(clear[2]),
                            a: f64::from(clear[3]),
                        }),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                ..wgpu::RenderPassDescriptor::default()
            });
            renderer.render(&mut pass.forget_lifetime(), jobs, screen);
        }
        for id in &textures.free {
            renderer.free_texture(id);
        }
        drop(renderer);
        textures.clear();

        let readable = self.config.usage.contains(wgpu::TextureUsages::COPY_SRC);
        let captured = if capture && readable {
            let image = read_back(
                &state.device,
                &state.queue,
                &output.texture,
                encoder,
                user_buffers,
            )?;
            Some(to_rgba(image, self.config.format))
        } else {
            state
                .queue
                .submit(user_buffers.into_iter().chain([encoder.finish()]));
            None
        };
        state.queue.present(output);
        Ok(captured)
    }
}

/// Field order is drop order: `gpu` holds raw pointers into `surface` and
/// the connection, so it goes first and `_conn` last.
struct Dock {
    gpu: Gpu,
    registry: RegistryState,
    outputs: OutputState,
    seats: SeatState,
    _compositor: CompositorState,
    _layer_shell: LayerShell,
    _layer: LayerSurface,
    surface: wl_surface::WlSurface,
    /// Logical size from the last configure.
    size: (u32, u32),
    /// Integer buffer scale of the output the dock is on.
    scale: i32,
    configured: bool,
    /// A `wl_surface.frame` callback is outstanding: drawing now would only
    /// queue behind it.
    frame_pending: bool,
    closed: bool,
    pointer: Option<wl_pointer::WlPointer>,
    egui_events: Vec<egui::Event>,
    ctx: egui::Context,
    app: Box<dyn App>,
    transparent: bool,
    events: Vec<Event>,
    start: Instant,
    /// When the next frame is due; `None` until input or the app asks.
    next_frame: Option<Instant>,
    resized: bool,
    qh: QueueHandle<Self>,
    _conn: Connection,
}

impl Dock {
    fn maybe_draw(&mut self) -> Result<(), GfxError> {
        let due = self.next_frame.is_some_and(|at| at <= Instant::now());
        if !self.configured || self.frame_pending || !due {
            return Ok(());
        }
        self.draw()
    }

    #[allow(clippy::cast_precision_loss, reason = "surface sizes are far below 2^24")]
    fn draw(&mut self) -> Result<(), GfxError> {
        let (width, height) = self.size;
        let scale = u32::try_from(self.scale.max(1)).unwrap_or(1);
        let (px_w, px_h) = (width * scale, height * scale);
        if self.resized {
            self.gpu.resize(px_w, px_h);
            self.surface.set_buffer_scale(self.scale.max(1));
            self.resized = false;
        }
        let output = match self.gpu.surface.get_current_texture() {
            wgpu::CurrentSurfaceTexture::Success(frame)
            | wgpu::CurrentSurfaceTexture::Suboptimal(frame) => frame,
            wgpu::CurrentSurfaceTexture::Outdated | wgpu::CurrentSurfaceTexture::Lost => {
                self.resized = true;
                return Ok(());
            }
            other => return Err(backend_error(format!("surface texture: {other:?}"))),
        };

        let input = egui::RawInput {
            screen_rect: Some(egui::Rect::from_min_size(
                egui::Pos2::ZERO,
                egui::vec2(width as f32, height as f32),
            )),
            time: Some(self.start.elapsed().as_secs_f64()),
            // Keyboard interactivity is off: a dock never holds focus.
            focused: false,
            events: std::mem::take(&mut self.egui_events),
            ..egui::RawInput::default()
        };
        self.ctx
            .set_pixels_per_point(self.scale.max(1) as f32);
        let mut shared = Shared::new(std::mem::take(&mut self.events));
        let app = &mut *self.app;
        let transparent = self.transparent;
        let full = self.ctx.run_ui(input, |ui| {
            let mut translated = Vec::new();
            ui.ctx()
                .input(|input| input::translate(&input.events, &mut translated));
            shared.events.extend(translated);
            draw(ui, transparent, app, &mut shared);
        });
        self.events.clear();
        if let Some(theme) = shared.theme {
            self.set_theme(theme);
        }

        let egui::FullOutput {
            shapes,
            pixels_per_point,
            mut textures_delta,
            viewport_output,
            ..
        } = full;
        let jobs = self.ctx.tessellate(shapes, pixels_per_point);
        let screen = egui_wgpu::ScreenDescriptor {
            size_in_pixels: [px_w, px_h],
            pixels_per_point,
        };
        let clear = if self.transparent {
            [0.0; 4]
        } else {
            self.ctx
                .global_style()
                .visuals
                .panel_fill
                .to_normalized_gamma_f32()
        };
        // Ask for the next frame callback before the commit that present does.
        self.surface.frame(&self.qh, self.surface.clone());
        self.frame_pending = true;
        let captured = self.gpu.paint(
            output,
            &jobs,
            &mut textures_delta,
            &screen,
            clear,
            shared.screenshot,
        )?;
        if let Some(image) = captured {
            // Delivered as an event next frame, as on the eframe path.
            self.events.push(screenshot_event(&image));
            self.request_frame(Duration::ZERO);
        }

        if shared.close {
            self.closed = true;
        }
        let delay = viewport_output
            .get(&egui::ViewportId::ROOT)
            .map_or(Duration::MAX, |viewport| viewport.repaint_delay);
        self.next_frame = None;
        self.request_frame(delay);
        Ok(())
    }

    fn set_theme(&mut self, theme: Theme) {
        apply_theme(&self.ctx, theme);
        self.request_frame(Duration::ZERO);
    }

    /// Schedule a frame `delay` from now, keeping an earlier one.
    fn request_frame(&mut self, delay: Duration) {
        let Some(at) = Instant::now().checked_add(delay) else {
            return;
        };
        self.next_frame = Some(self.next_frame.map_or(at, |next| next.min(at)));
    }
}

/// The read-back bytes are in surface order; `screenshot_event` wants RGBA.
fn to_rgba(image: egui::ColorImage, format: wgpu::TextureFormat) -> egui::ColorImage {
    if !matches!(
        format,
        wgpu::TextureFormat::Bgra8Unorm | wgpu::TextureFormat::Bgra8UnormSrgb
    ) {
        return image;
    }
    let pixels = image
        .pixels
        .iter()
        .map(|px| egui::Color32::from_rgba_premultiplied(px.b(), px.g(), px.r(), px.a()))
        .collect();
    egui::ColorImage::new(image.size, pixels)
}

impl CompositorHandler for Dock {
    fn scale_factor_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        factor: i32,
    ) {
        if factor != self.scale {
            self.scale = factor;
            self.resized = true;
            self.request_frame(Duration::ZERO);
        }
    }

    fn transform_changed(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: wl_output::Transform,
    ) {
    }

    fn frame(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &wl_surface::WlSurface, _: u32) {
        self.frame_pending = false;
    }

    fn surface_enter(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: &wl_output::WlOutput,
    ) {
    }

    fn surface_leave(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_surface::WlSurface,
        _: &wl_output::WlOutput,
    ) {
    }
}

impl LayerShellHandler for Dock {
    fn closed(&mut self, _: &Connection, _: &QueueHandle<Self>, _: &LayerSurface) {
        self.closed = true;
    }

    fn configure(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &LayerSurface,
        configure: LayerSurfaceConfigure,
        _: u32,
    ) {
        let (width, height) = configure.new_size;
        let size = (width.max(1), height.max(1));
        if size != self.size {
            self.size = size;
            self.resized = true;
        }
        self.configured = true;
        // SCTK acks the configure; the first buffer must follow it.
        self.frame_pending = false;
        self.request_frame(Duration::ZERO);
    }
}

impl OutputHandler for Dock {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.outputs
    }

    fn new_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn update_output(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {}

    fn output_destroyed(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_output::WlOutput) {
    }
}

impl SeatHandler for Dock {
    fn seat_state(&mut self) -> &mut SeatState {
        &mut self.seats
    }

    fn new_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}

    fn new_capability(
        &mut self,
        _: &Connection,
        qh: &QueueHandle<Self>,
        seat: wl_seat::WlSeat,
        capability: Capability,
    ) {
        if capability == Capability::Pointer && self.pointer.is_none() {
            self.pointer = self.seats.get_pointer(qh, &seat).ok();
        }
    }

    fn remove_capability(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: wl_seat::WlSeat,
        capability: Capability,
    ) {
        if capability == Capability::Pointer {
            if let Some(pointer) = self.pointer.take() {
                pointer.release();
            }
        }
    }

    fn remove_seat(&mut self, _: &Connection, _: &QueueHandle<Self>, _: wl_seat::WlSeat) {}
}

/// Linux evdev button codes.
const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;
const BTN_MIDDLE: u32 = 0x112;

impl PointerHandler for Dock {
    #[allow(clippy::cast_possible_truncation, reason = "surface coordinates fit f32")]
    fn pointer_frame(
        &mut self,
        _: &Connection,
        _: &QueueHandle<Self>,
        _: &wl_pointer::WlPointer,
        events: &[PointerEvent],
    ) {
        for event in events {
            if event.surface != self.surface {
                continue;
            }
            let pos = egui::pos2(event.position.0 as f32, event.position.1 as f32);
            let button = |code| match code {
                BTN_LEFT => Some(egui::PointerButton::Primary),
                BTN_RIGHT => Some(egui::PointerButton::Secondary),
                BTN_MIDDLE => Some(egui::PointerButton::Middle),
                _ => None,
            };
            let translated = match event.kind {
                PointerEventKind::Enter { .. } | PointerEventKind::Motion { .. } => {
                    Some(egui::Event::PointerMoved(pos))
                }
                PointerEventKind::Leave { .. } => Some(egui::Event::PointerGone),
                PointerEventKind::Press { button: code, .. }
                | PointerEventKind::Release { button: code, .. } => {
                    button(code).map(|button| egui::Event::PointerButton {
                        pos,
                        button,
                        pressed: matches!(event.kind, PointerEventKind::Press { .. }),
                        modifiers: egui::Modifiers::NONE,
                    })
                }
                PointerEventKind::Axis {
                    horizontal,
                    vertical,
                    ..
                } => Some(egui::Event::MouseWheel {
                    unit: egui::MouseWheelUnit::Point,
                    delta: egui::vec2(-horizontal.absolute as f32, -vertical.absolute as f32),
                    modifiers: egui::Modifiers::NONE,
                    phase: egui::TouchPhase::Move,
                    source: egui::MouseWheelSource::Unknown,
                }),
            };
            if let Some(event) = translated {
                self.egui_events.push(event);
                self.request_frame(Duration::ZERO);
            }
        }
    }
}

impl ProvidesRegistryState for Dock {
    fn registry(&mut self) -> &mut RegistryState {
        &mut self.registry
    }
    registry_handlers![OutputState, SeatState];
}

delegate_compositor!(Dock);
delegate_output!(Dock);
delegate_seat!(Dock);
delegate_pointer!(Dock);
delegate_layer!(Dock);
delegate_registry!(Dock);

#[cfg(test)]
mod tests {
    use super::{logical_px, to_rgba};

    #[test]
    fn thickness_rounds_to_a_whole_nonzero_size() {
        assert_eq!(logical_px(36.4), 36);
        assert_eq!(logical_px(0.2), 1);
    }

    #[test]
    fn bgra_surfaces_read_back_as_rgba() {
        let bgra = egui::ColorImage::new(
            [1, 1],
            vec![egui::Color32::from_rgba_premultiplied(30, 20, 10, 255)],
        );
        let rgba = to_rgba(bgra.clone(), wgpu::TextureFormat::Bgra8Unorm);
        assert_eq!(rgba.pixels[0].to_array(), [10, 20, 30, 255]);
        let same = to_rgba(bgra, wgpu::TextureFormat::Rgba8Unorm);
        assert_eq!(same.pixels[0].to_array(), [30, 20, 10, 255]);
    }
}
