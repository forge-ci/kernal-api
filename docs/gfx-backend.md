# GFX backend facade

`kernal_api::gfx` lets an application open a window and draw an
immediate-mode UI without depending on, or naming, any GUI backend. This
records the decisions; the rustdoc on `src/gfx/` is the API reference.

## Features

| Feature    | Adds                                                                    |
| ---------- | ----------------------------------------------------------------------- |
| `gfx`      | The neutral vocabulary only. No dependencies. `run` returns `NoBackend`. |
| `gfx-egui` | `gfx` plus the private egui backend (eframe, egui, egui-wgpu, wgpu, winit). |

Neither is default. Every backend crate is an optional `dep:` and lives in
`OWNED_IMPLEMENTATION_CRATES`, so the `kernal_api_boundary` Dylint rejects a
client that depends on or names one, and rejects this crate if it puts one in
a public type position or `pub use`s one.

## Public vocabulary

| Concern | Facade type | Decision |
| --- | --- | --- |
| App / event loop ownership | `gfx::run(WindowOptions, impl App) -> Result<(), GfxError>` | `run` owns the event loop on the calling thread (the main thread on macOS) until the window closes. One window per `run`; `run` may be called again on the same thread after it returns (a resident app opens a fresh window per show). No async: a GUI loop is not a Tokio task. |
| Window description | `WindowOptions` (builder): title, size, resizable, decorations, always-on-top, transparent, click-through, `Placement`, initial `Theme` | Fields are private; the builder is the contract, so fields can be added without breaking callers. |
| Frame lifecycle | `App::frame(&mut Frame)` once per frame; `App::close_requested() -> bool` | Immediate mode: the app owns all state; a frame is a pure function of state plus events. `Frame::close`, `Frame::request_repaint` drive the loop. |
| Widget subset | `label`, `heading`, `button`, `toggle`, `text_field`, `slider`, `separator`, `space`, `horizontal`, `scroll`, `list`, `selectable_row` | The intersection every candidate backend can render. Each interactive widget returns "acted on this frame" as `bool`; no widget handles or response objects. See "Provisional widget set". |
| Input | `Frame::events() -> &[Event]`: `Key`, `Text`, `PointerMoved`, `PointerButton`, `PointerLeft`, `Scroll` (pixels), `Focused`, `Screenshot` | Logical pixels, delivery order. `Frame::request_screenshot()` reads back the window's own rendered framebuffer (not the compositor's) and delivers it in a later frame as `Event::Screenshot { width, height, rgba }` (physical pixels, straight-alpha sRGB); it works wherever the renderer does, including llvmpipe, so CI can prove what was drawn. Backend-specific events (IME, touch, accessibility, clipboard) are dropped, not passed through. |
| Theme tokens | `Theme { mode, accent, background, text_size, spacing, corner_radius }`, `Color` | Tokens, not a style tree. A backend maps them onto its own style system. |
| Windowless render | `gfx::snapshot(&WindowOptions, &mut impl App) -> Result<Snapshot, GfxError>`; `Snapshot::{width, height, rgba}` | Lays the UI out at `options.size` (one pixel per point) over three input-less settling frames and paints it offscreen. No window, no compositor, so it works on a locked or display-less session where `request_screenshot` cannot. Same pixel layout as `Event::Screenshot`; fields are private. |
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
- **Window-manager behaviour.** Transparency, click-through, always-on-top and
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
