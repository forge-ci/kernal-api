# GFX backend facade

`kernal_api::gfx` lets an application open a window and draw an
immediate-mode UI without depending on, or naming, any GUI backend. This
records the decisions; the rustdoc on `src/gfx/` is the API reference.

## Features

| Feature    | Adds                                                                    |
| ---------- | ----------------------------------------------------------------------- |
| `gfx`      | The neutral vocabulary only. No dependencies. `run` returns `NoBackend`. |
| `gfx-egui` | `gfx` plus the private egui backend (eframe, egui, egui-wgpu, wgpu, winit; on Linux also smithay-client-toolkit and raw-window-handle for docks). |

Neither is default. Every backend crate is an optional `dep:` and lives in
`OWNED_IMPLEMENTATION_CRATES`, so the `kernal_api_boundary` Dylint rejects a
client that depends on or names one, and rejects this crate if it puts one in
a public type position or `pub use`s one.

## Public vocabulary

| Concern | Facade type | Decision |
| --- | --- | --- |
| App / event loop ownership | `gfx::run(WindowOptions, impl App) -> Result<(), GfxError>` | `run` owns the event loop on the calling thread (the main thread on macOS) until the window closes. One window per `run`; `run` may be called again on the same thread after it returns (a resident app opens a fresh window per show). No async: a GUI loop is not a Tokio task. |
| Window description | `WindowOptions` (builder): title, size, resizable, decorations, always-on-top, transparent, click-through, `Placement` (`Default`, `Centered`, `At`, `Monitor`, `Dock { edge: Edge, thickness }`), initial `Theme` | Fields are private; the builder is the contract, so fields can be added without breaking callers. |
| Drop-down panels | `Frame::show_panel(&PanelOptions) -> Result<(), GfxError>`, `App::panel(&mut Frame)`, `Event::PanelDismissed`, `PanelOptions::new(w, h).edge(Edge).offset(f32)`, `Capabilities::panels` | One panel at a time, beside a running dock. See "Panels". |
| Dock length | `WindowOptions::dock_length(DockLength::{Full, Fixed(f32)})`, `WindowOptions::dock_align(DockAlign::{Start, Center, End})`, `Frame::set_dock_length(DockLength) -> Result<(), GfxError>` | Default `Full` (today's behaviour). A shorter dock still reserves exactly its thickness; panel offsets are measured from the dock's start. See "Docks". |
| Frame lifecycle | `App::frame(&mut Frame)` once per frame; `App::close_requested() -> bool` | Immediate mode: the app owns all state; a frame is a pure function of state plus events. `Frame::close`, `Frame::request_repaint`, `Frame::request_repaint_after(Duration)` drive the loop. |
| Layout metrics | `Frame::text_width(&str, size) -> f32`, `Frame::line_height(size) -> f32`, `Frame::available_width() -> f32`, `Frame::available_height() -> f32` | Plain logical-pixel `f32`s, read per frame. See "Layout metrics". |
| Widget subset | `label`, `heading`, `button`, `button_click` (which pointer button: `Some(PointerButton::Secondary)` for a context click), `toggle`, `text_field`, `slider`, `separator`, `space`, `horizontal`, `scroll`, `list`, `selectable_row` | The intersection every candidate backend can render. Each interactive widget returns "acted on this frame" as `bool`; no widget handles or response objects. See "Provisional widget set". |
| Input | `Frame::events() -> &[Event]`: `Key`, `Text`, `PointerMoved`, `PointerButton`, `PointerLeft`, `Scroll` (pixels), `Focused`, `Screenshot`, `PanelDismissed` | Logical pixels, delivery order. `Frame::request_screenshot()` reads back the window's own rendered framebuffer (not the compositor's) and delivers it in a later frame as `Event::Screenshot { width, height, rgba }` (physical pixels, straight-alpha sRGB); it works wherever the renderer does, including llvmpipe, so CI can prove what was drawn. Backend-specific events (IME, touch, accessibility, clipboard) are dropped, not passed through. |
| Theme tokens | `Theme { mode, accent, background, foreground, panel, text_size, spacing, corner_radius }`, `Color`; builders `with_mode/with_accent/with_background/with_foreground/with_panel`, `for_panel()` | Tokens, not a style tree. A backend maps them onto its own style system. `foreground` overrides body text; `panel` fills drop-down panels (`for_panel()` is the theme a panel surface draws with: `background` replaced by `panel`). Added for talos-bar's `[theme]` config: `#[non_exhaustive]`, so callers outside the crate build a `Theme` from `default()`/`with_mode()` and the builders. |
| Windowless render | `gfx::snapshot(&WindowOptions, &mut impl App) -> Result<Snapshot, GfxError>`; `Snapshot::{width, height, rgba}` | Lays the UI out at `options.size` (one pixel per point) over three input-less settling frames and paints it offscreen. No window, no compositor, so it works on a locked or display-less session where `request_screenshot` cannot. Same pixel layout as `Event::Screenshot`; fields are private. It draws what a **focused** window shows (see "Snapshot parity"). Placement is ignored, so a `Dock` renders on any host. |
| Capability query | `gfx::capabilities() -> Capabilities` of `Support::{Supported, BestEffort, Unsupported}` | `run` validates options against it first and fails with `GfxErrorKind::Unsupported`; it never silently degrades. |

Every public enum and `Capabilities` is `#[non_exhaustive]`. The frame
context is a facade struct with a private, feature-selected implementation
field; there is no public backend trait and no `impl` of a backend trait for
a facade type.

## When frames happen

A frame is one call to `App::frame`. Within `run`, it happens only when the
windowing system lets the window draw:

- On first show, on input, after `Frame::request_repaint` (or a backend
  repaint for `set_theme` / `request_screenshot`), **if** the window is being
  painted.
- On Wayland, frames are paced by the compositor. After each present the
  backend (winit, via egui-wgpu's `pre_present_notify`) waits for the
  compositor's `wl_surface.frame` callback before running the next frame. A
  surface the compositor is not painting (locked session, minimised or
  hidden window, and on some compositors an off-screen workspace) gets no
  callback, so **no frames run at all** until it is painted again.
  `request_repaint` cannot override this, and a pending
  `request_screenshot` is delivered only when frames resume. Present mode
  makes no difference. See issue #561 for the trace.
- X11 and Xvfb have no frame callbacks, so frames run on request. A Vulkan
  present to an unmapped XWayland window can still block.

`Frame::request_repaint_after(d)` asks for a frame no later than `d` from
now: the backend wakes the app no later than this; a compositor that is not
drawing the window may still withhold frames. It is a deadline, not a
continuous repaint, so an idle clock costs one frame per tick instead of one
per vblank. Request it again every frame that still needs the timer. On the
dock path the deadline schedules the next frame directly; on the window path
it is the backend's scheduled repaint.

Observed on the unlocked host `wayland-0` session (KWin 6.7.4, issue #561),
with `examples/gfx-frame-count.rs` for 5 s: `request_repaint` every frame ran
262 frames; `request_repaint_after(1s)` ran 10 (startup plus one per
second). The same window while the session was locked ran 1 frame in 10 s.

So `App::frame` is not a timer. A frame means "the window may be drawn now".
App logic that must make progress while a window is unpainted (IPC refresh,
timeouts) belongs on another thread. Pixels that are needed regardless of
the compositor come from `gfx::snapshot`, which never opens a window.

## Snapshot parity

`gfx::snapshot` and a live window run the same `draw` over the same theme, so
for the same app, size and focus state they produce the same pixels.
`examples/gfx-parity.rs` renders one popover-shaped UI both ways. Under
`kwin_wayland --virtual` the two 640x400 captures differ in 260 of 256,000
pixels, every one by less than 8 of 255 in each channel, all on glyph edges
(antialiasing; the window and the snapshot may get different adapters and
surface formats). Geometry, insets and widget states are identical.

The one input that changes the picture is **keyboard focus**. A snapshot is
laid out as a focused window: the text field that takes focus draws its
focus outline and cursor. A window without keyboard focus draws the same
field dark with no outline. An earlier windowed capture taken under a locked
session, where the window never got keyboard focus, looked different for
that reason alone. Rendering the snapshot with focus off reproduces it.

### Which adapter a snapshot uses

wgpu picks the adapter, and its environment variables steer it:
`WGPU_BACKEND` (`vulkan`, `gl`), `WGPU_ADAPTER_NAME` (substring match),
`WGPU_POWER_PREF`, plus the Vulkan loader's `VK_ICD_FILENAMES`.

**"Parent device is lost"** from `gfx::snapshot` means wgpu's **GL** backend
running on **NVIDIA's EGL** (`libEGL_nvidia`, chosen by glvnd). Measured on
a two-NVIDIA-GPU NixOS host (2026-11-02). GL on Mesa's EGL works, and Vulkan
works on either NVIDIA GPU and on lavapipe. Load and adapter choice make no
difference. A process ends up on GL when it cannot dlopen `libvulkan.so.1`,
which a NixOS binary outside a dev shell cannot do by default:

| Environment | Result |
| --- | --- |
| nothing (no Vulkan loader on the path → GL → NVIDIA EGL) | Parent device is lost |
| `WGPU_BACKEND=gl` (+ `LIBGL_ALWAYS_SOFTWARE=1`, still NVIDIA EGL) | Parent device is lost |
| `WGPU_ADAPTER_NAME=3060` / `=llvmpipe` without the loader | Parent device is lost (still GL) |
| `WGPU_BACKEND=gl __EGL_VENDOR_LIBRARY_FILENAMES=…/50_mesa.json` | works |
| `LD_LIBRARY_PATH=/run/current-system/sw/share/nix-ld/lib` (Vulkan, NVIDIA) | works |
| same + `WGPU_ADAPTER_NAME=2080` / `WGPU_POWER_PREF=low` | works |
| same + `WGPU_BACKEND=vulkan VK_ICD_FILENAMES=…/lvp_icd.x86_64.json` (lavapipe) | works, 3/3 |

CI and any host-independent render should pin software Vulkan:

    WGPU_BACKEND=vulkan VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.x86_64.json   # Debian/Ubuntu mesa-vulkan-drivers

On NixOS put the Vulkan loader on the path
(`LD_LIBRARY_PATH=/run/current-system/sw/share/nix-ld/lib`) and use
`/run/opengl-driver/share/vulkan/icd.d/lvp_icd.x86_64.json`.

## Docks

`Placement::Dock { edge, thickness }` asks for a panel that spans one screen
edge and makes the desktop reserve that space, so maximised windows stop
short of it. It is the shape a status bar needs, and it is a different
surface kind from a window on every host:

| Host | Native shape | Today |
| --- | --- | --- |
| Linux, Wayland with `zwlr_layer_shell_v1` (KWin, wlroots compositors) | layer-shell surface, anchored to the edge, exclusive zone = thickness | `Supported` |
| Linux, GNOME (Mutter has no layer-shell), X11 | none / `_NET_WM_STRUT` dock window | `Unsupported` |
| macOS | `NSPanel` at status-bar level | `Unsupported` (not wired yet) |
| Windows | `SHAppBarMessage` AppBar | `Unsupported` (not wired yet) |

`capabilities().dock` is decided per session: on Linux the first call
connects to `WAYLAND_DISPLAY` once and looks for the layer-shell global.
`run` returns `GfxErrorKind::Unsupported` with the exact reason, for example
`Placement::Dock: not a Wayland session (no reachable WAYLAND_DISPLAY)`.

**Why it is not eframe.** winit 0.30 creates every Wayland window as an
`xdg_toplevel` (`platform_impl/linux/wayland/window/mod.rs`,
`xdg_shell.create_window`). Its Wayland extension traits expose only the
toplevel pointer and the app id (`WindowExtWayland::xdg_toplevel`,
`WindowAttributesExtWayland::with_name`), and a `wl_surface` that has taken
the xdg role can never take the layer-shell role. eframe 0.37 builds on that
window and has no hook to replace it. So a dock cannot be made from the
`gfx::run` window path at all.

**What it is instead.** `src/platform_linux/gfx_dock.rs` is a second surface
path that does not use winit: smithay-client-toolkit opens its own Wayland
connection and layer surface, wgpu renders into that `wl_surface` through raw
window handles, and egui-wgpu paints the frame. The frame itself is the egui
backend's `draw` and `FrameImp`, so an `App` cannot tell which path it runs
on: widgets, theme, `Event::Pointer*`, `Scroll`, `request_repaint`,
`request_screenshot` and `close` all behave the same. Frames are paced by
`wl_surface.frame` like the eframe path. The dock never takes keyboard focus
(`KeyboardInteractivity::None`), so it gets no key or text events; its
panels do (see "Panels"). This is
the same structure as voxtype's `voxtype-osd-native`. Both crates were
already in the tree under winit, and they are pinned to winit's versions.

Proven under `kwin_wayland --virtual` (1280x800, on a private D-Bus):
`examples/gfx-dock.rs` configures at 1280x36 and draws its label and clock.
KWin's scripting API reports `MaximizeArea x=0 y=36 w=1280 h=764` and the
dock at `0,0 1280x36`, and a maximised window placed at `0,36 1280x764`.
KWin's own screenshot shows the bar above that window's title bar.

**Shorter than the edge.** `WindowOptions::dock_length(DockLength::Fixed(px))`
(or `Frame::set_dock_length` while running, for a dock sized to its content
with `text_width`) makes the dock `px` long, placed by `dock_align`. The
desktop must still reserve exactly the thickness, never more and never a
gap-free screen. `zwlr_layer_shell_v1` only honours an exclusive zone on a
surface anchored to one edge, or to an edge and both edges perpendicular to
it; a corner anchor reserves nothing. So `Center` anchors to its edge alone
(the compositor centres it) and `Start`/`End` stay anchored across the edge
and are shortened by a margin computed from the output's logical size
(learned from `wl_surface.enter`; until then the dock is full length). The
length is clamped to the edge. `PanelOptions::offset` is measured from the
dock's start, so a panel still opens under the widget the app measured.

Proven under `kwin_wayland --virtual` with `examples/gfx-dock.rs --fit
start|center|end` (237 px of content) on a 1280x800 output: the dock is at
`0,0 1280x36` full, `0,0 237x36` start, `521.5,0 237x36` centre and
`1043,0 237x36` end, and KWin's `MaximizeArea` is `x=0 y=36 w=1280 h=764` in
all four cases: 36 px reserved, once.

## Layout metrics

For layouts that must decide what fits (a dock dropping zones that do not):

- `Frame::text_width(text, size)`: the width of `text` on one line, no
  wrapping, in the backend's body font at `size` logical pixels, which is the
  font `label` draws with. Measured by the backend's own text layout, never
  from fixed per-character metrics. The empty string is `0`.
- `Frame::line_height(size)`: the height of one line in that font.
- `Frame::available_width()` / `available_height()`: the space left at the
  current layout position. At the top level of a frame this is the real
  surface size (for a dock, the size the compositor assigned, not
  `WindowOptions::size`) less the panel margin; inside `horizontal` or
  `scroll` it is what remains there.

All four are per-frame values: they follow the active theme's text style and
the current surface, so read them in the frame that uses them and do not
cache them across theme or size changes.

## Provisional widget set

Added for result lists (the talos-bar search popover is the first consumer):

- `Frame::scroll(height, |f| ..)`: a vertical region `height` logical pixels
  tall and layout-wide; taller content is clipped and scrolls (wheel, drag,
  bar). No horizontal scrolling, no programmatic offset.
- `Frame::list(|f| ..)`: the list convention. Rows are stacked with
  `Theme::spacing / 2` between them; groups are split with `separator`. No
  other list styling exists, so consumers do not invent any.
- `Frame::selectable_row(text, selected) -> bool`: a full-width row,
  accent-filled when `selected`, with the backend's hover/press feedback;
  `true` when clicked. Selection state is the app's. On the frame a row
  becomes selected it scrolls itself into the enclosing `scroll`, so arrow-key
  navigation keeps the selection visible while a wheel scroll away from a
  selection that has not changed is left alone.
- `text_field` focus: when nothing holds keyboard focus, the first text field
  in a frame takes it, so a single-field window types immediately. There is no
  explicit focus API yet.

Keyboard navigation is the app's: read `Key::ArrowUp`/`ArrowDown`/`Enter`
from `Frame::events` and move `selected`. Rows identify themselves by layout
position, which is enough for a list rebuilt each frame.

## Panels

A bar needs drop-downs under its own cells (a task list under the tasks
cell, an agenda under the clock): borderless, above ordinary windows,
anchored to the screen edge the bar is on, opened and closed by the bar.
`Placement::At` cannot express that on Wayland (the compositor ignores client
positions for toplevels), and `run` drives exactly one window, so panels are
their own capability:

```rust
impl App for Bar {
    fn frame(&mut self, frame: &mut Frame<'_>) {
        if frame.events().iter().any(|e| matches!(e, Event::PanelDismissed)) {
            self.open = false;
        }
        if frame.button("Tasks") { self.open = !self.open; }
        if self.open {
            let panel = PanelOptions::new(420.0, 300.0).edge(Edge::Top).offset(112.0);
            // Every frame it stays up; Err(Unsupported) off a dock.
            let _ = frame.show_panel(&panel);
        }
    }
    fn panel(&mut self, frame: &mut Frame<'_>) {
        // the panel's own frame: its events, its size
    }
}
```

- **Immediate mode.** The dock frame calls `show_panel` on every frame the
  panel should be visible; a frame that does not call it closes it. Changing
  the options resizes or moves it. `App::panel` draws it, with its own events
  (positions relative to the panel). One panel at a time.
- **Placement.** Anchored to `edge` at `offset` along it, with a zero
  exclusive zone, so the compositor keeps it clear of the dock's reserved
  space: a top panel opens directly under a top dock.
- **Focus and dismissal.** The panel asks for keyboard focus on demand. It
  receives named keys (`Escape`, arrows, Enter, Tab, Home/End, Page keys,
  Backspace/Delete, Space), mapped from evdev codes, and typed text: the
  compositor's XKB keymap is compiled with libxkbcommon, loaded at run time
  as winit loads it (`src/platform_linux/gfx_keymap.rs`), so a `text_field`
  in a panel types in the user's layout. Shortcuts (Ctrl/Alt/Super held)
  type nothing. A held key repeats on the client, as Wayland requires, at the
  delay and rate the compositor sends in `wl_keyboard.repeat_info` (the
  desktop's keyboard settings; 600 ms and 25/s until it arrives, rate 0 turns
  it off). Text keys and Backspace/Delete/arrows/Home/End/Page keys repeat;
  Escape, Enter and Tab do not, so a held Enter submits once. Nested KWin
  sends 50/s after 250 ms: a 1 s hold typed about 38 characters. A
  host without libxkbcommon gets named keys only. When focus moves to another window (a click
  away), the backend closes the panel and the next `App::frame` sees
  `Event::PanelDismissed`. Input to the panel also schedules a dock frame, so
  state the panel changed (Esc) reaches `frame` at once.
- **Where.** `Capabilities::panels` equals `Capabilities::dock`: on Linux it
  is a second `zwlr_layer_shell_v1` surface (layer `top`) of the same
  connection, rendered on the dock's wgpu device with its own swapchain,
  egui context and frame clock. `show_panel` returns `Unsupported` from an
  app not run as a dock (an eframe window, `snapshot`) and on hosts without
  docks. A panel's content can still be snapshotted by rendering the same
  drawing code in `App::frame` of a `snapshot`.

Proven under `kwin_wayland --virtual` (1280x800) with talos-bar-rs and real
EIS input: a click on the bar's tasks cell mapped a panel KWin reports at
`x=112 y=36 w=420 h=520` (`active=true`), Esc removed it, a click on the
clock mapped one at `x=180 y=36 w=460 h=293`, and a click into a maximised
window behind it dismissed it.

## Monitors and translucency

**Which monitor.** `WindowOptions::monitor(Monitor)` says which output a
`Placement::Dock` (and every panel it opens) appears on:
`Monitor::Primary` (the default: the desktop's choice) or
`Monitor::Named("HDMI-A-1")`, the connector name the desktop gives the
output. `Capabilities::dock_monitor` reports it, exactly where `dock` is. On
the layer-shell path the dock reads the outputs' names on a private queue of
its own connection before creating its surface, and creates the layer surface
(and panels) on the matching output.

A named monitor that is not connected at start-up falls back to the
desktop's choice instead of failing: a bar that will not start while its
monitor sleeps or is unplugged is worse than a bar on the wrong monitor, and
the Qt talos-bar made the same call. The fallback is not silent: the backend
writes one line to stderr naming the outputs it did find, and the app gets
`Event::Monitor { name, fallback: true }`. Every dock gets
`Event::Monitor { name, fallback }` once it is shown, naming the output it
landed on. The backend does not move back by itself when the named output
reappears; an app that wants that watches `Frame::monitors` and calls
`Frame::set_monitor` (talos-bar does).

**Moving a running dock.** `Frame::monitors()` lists the outputs as
`MonitorInfo { name, current }` (no entry is called primary: Wayland gives
clients no such notion), and `Frame::set_monitor(Monitor)` moves the dock
after the frame. A name not in the list is `GfxErrorKind::UnknownMonitor` and
changes nothing; off a dock (snapshot, eframe windows, `App::panel`) the list
is empty and the call is `Unsupported`, as `Capabilities::dock_relocation`
says. A layer surface's output is fixed when it is created, so the backend
destroys the main surface (the compositor drops its exclusive zone on the old
output) and creates it again on the new one, with the same shape, theme,
egui context and renderer (so uploaded textures survive); an open panel is
closed with `Event::PanelDismissed`, and `Event::Monitor { fallback: false }`
follows on the new output. Under the same two-output nested KWin,
`gfx-dock --cycle 4` moved the dock four times: `MaximizeArea` went from
`Virtual-0 0,36 1280x764 / Virtual-1 1280,0 1280x800` to
`Virtual-0 0,0 1280x800 / Virtual-1 1280,36 1280x764` and back, each time.

Proven under `kwin_wayland --virtual --output-count 2` (two 1280x800
outputs, `Virtual-0` at 0,0 and `Virtual-1` at 1280,0): with
`Monitor::Named("Virtual-1")` the dock is at `1280,0 1280x36` on
`Virtual-1`, KWin's `MaximizeArea` is `x=1280 y=36 w=1280 h=764` there and
unchanged on `Virtual-0` (`x=0 y=0 w=1280 h=800`), and a panel opens on
`Virtual-1`. With `Named("HDMI-A-9")` the dock falls back to `Virtual-0`.

**Translucency.** `WindowOptions::transparent(true)` composites the surface
with premultiplied alpha and clears it to the theme's background colour
*including its alpha* (the panel colour for panels); the central panel then
draws no fill of its own, so the alpha is applied once. Widgets and text
stay opaque. Without a theme background the surface is fully clear, as
before. Under nested KWin a dock and a panel with background
`rgba(32,35,38,179)` (70%) over a window whose pixels are `239,240,241`
captured as `93,97,99`; solving `out = a*S + (1-a)*D` per channel gives
`a = 0.705, 0.698, 0.700` against the requested `179/255 = 0.702`.

## Deliberately not portable

- **Custom painting / raw GPU access.** No canvas, mesh, texture or
  `wgpu::Device` handle. Exposing one would make the renderer the contract.
- **Backend widgets, layouts, IDs and response objects.** Anything outside the
  subset needs a facade widget first.
- **Fine-grained styling.** Per-widget colours, fonts, strokes.
- **Multiple windows / viewports, menus, tray, dialogs.** (A dock's one
  drop-down panel is the exception; see "Panels".) `system-tray` and
  `webview` remain separate capabilities.
- **Window-manager behaviour.** Docks are their own capability (see "Docks").
  Transparency, click-through, always-on-top and
  absolute placement are `BestEffort` under egui: winit forwards the request
  and the compositor may refuse (Wayland ignores client positioning;
  transparency needs a compositor). Monitor selection is `Unsupported` under
  egui, because eframe creates the window before monitors can be enumerated.
- **Raw window handles.** Embedding a foreign surface is not in the facade.

## The egui backend

`src/gfx_egui/` (private, `gfx-egui`) adapts `eframe` with the wgpu renderer:
`WindowOptions` becomes an `egui::ViewportBuilder`, `App` is wrapped in an
`eframe::App` adapter that renders inside a `CentralPanel`, egui events are
translated to `gfx::Event` and the theme tokens to `egui::Visuals`/`Style`.
eframe is used instead of hand-wiring egui-winit and egui-wgpu because it
already owns the winit loop, surface lifecycle and DPI handling; the other
four crates are declared so their versions are pinned alongside it and the
lint names them. No host `cfg` is needed: winit does the host selection, so
the backend sits outside the platform trees by the platform-boundary rule.

## A second backend

A backend (Slint, a native shell) is a sibling private module
`src/gfx_<name>/` behind feature `gfx-<name>` that provides the same
crate-private surface the egui module does: `capabilities()`, `run()`, and a
`FrameImp` with the widget methods. `src/gfx/mod.rs` selects it by feature
alongside `gfx_egui`, and adds a `BackendKind` variant. Facade types do not
change. Retained-mode toolkits implement the immediate-mode subset by diffing
the frame's widget sequence against the previous frame; that cost is theirs,
not the facade's.

Per the house rule (no trait without two real backends), the backend surface
is a crate-private convention today. When the second backend lands, the
shared shape becomes a private `trait Backend`, still never public. Enabling
two backend features at once is a decision for that PR (compile error vs.
priority order); today there is only one.

## Stability

- Stable: the feature names, `run`/`App` shape, `WindowOptions` builder
  methods, `Capabilities`/`Support`, `GfxErrorKind`, `Color`/`ThemeMode`.
- Provisional: the widget subset (will grow), `Event`/`Key` coverage, the
  `Theme` token list, and the `BestEffort` classification per host.
