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
| Frame lifecycle | `App::frame(&mut Frame)` once per frame; `App::close_requested() -> bool` | Immediate mode: the app owns all state; a frame is a pure function of state plus events. `Frame::close`, `Frame::request_repaint` drive the loop. |
| Widget subset | `label`, `heading`, `button`, `toggle`, `text_field`, `slider`, `separator`, `space`, `horizontal`, `scroll`, `list`, `selectable_row` | The intersection every candidate backend can render. Each interactive widget returns "acted on this frame" as `bool`; no widget handles or response objects. See "Provisional widget set". |
| Input | `Frame::events() -> &[Event]`: `Key`, `Text`, `PointerMoved`, `PointerButton`, `PointerLeft`, `Scroll` (pixels), `Focused`, `Screenshot` | Logical pixels, delivery order. `Frame::request_screenshot()` reads back the window's own rendered framebuffer (not the compositor's) and delivers it in a later frame as `Event::Screenshot { width, height, rgba }` (physical pixels, straight-alpha sRGB); it works wherever the renderer does, including llvmpipe, so CI can prove what was drawn. Backend-specific events (IME, touch, accessibility, clipboard) are dropped, not passed through. |
| Theme tokens | `Theme { mode, accent, background, text_size, spacing, corner_radius }`, `Color` | Tokens, not a style tree. A backend maps them onto its own style system. |
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

Run the example with `LD_LIBRARY_PATH` including the Vulkan loader
(`/run/current-system/sw/share/nix-ld/lib` on NixOS) outside `soldr`; without
it wgpu falls back to GL, and the windowless GL path fails with "Parent
device is lost" on this host.

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
(`KeyboardInteractivity::None`), so it gets no key or text events. This is
the same structure as voxtype's `voxtype-osd-native`. Both crates were
already in the tree under winit, and they are pinned to winit's versions.

Proven under `kwin_wayland --virtual` (1280x800, on a private D-Bus):
`examples/gfx-dock.rs` configures at 1280x36 and draws its label and clock.
KWin's scripting API reports `MaximizeArea x=0 y=36 w=1280 h=764` and the
dock at `0,0 1280x36`, and a maximised window placed at `0,36 1280x764`.
KWin's own screenshot shows the bar above that window's title bar.

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

## Deliberately not portable

- **Custom painting / raw GPU access.** No canvas, mesh, texture or
  `wgpu::Device` handle. Exposing one would make the renderer the contract.
- **Backend widgets, layouts, IDs and response objects.** Anything outside the
  subset needs a facade widget first.
- **Fine-grained styling.** Per-widget colours, fonts, strokes.
- **Multiple windows / viewports, menus, tray, dialogs.** `system-tray` and
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
