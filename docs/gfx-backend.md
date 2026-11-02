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
| App / event loop ownership | `gfx::run(WindowOptions, impl App) -> Result<(), GfxError>` | `run` owns the event loop on the calling thread (the main thread on macOS) until the window closes. One window per `run`. No async: a GUI loop is not a Tokio task. |
| Window description | `WindowOptions` (builder): title, size, resizable, decorations, always-on-top, transparent, click-through, `Placement`, initial `Theme` | Fields are private; the builder is the contract, so fields can be added without breaking callers. |
| Frame lifecycle | `App::frame(&mut Frame)` once per frame; `App::close_requested() -> bool` | Immediate mode: the app owns all state; a frame is a pure function of state plus events. `Frame::close`, `Frame::request_repaint` drive the loop. |
| Widget subset | `label`, `heading`, `button`, `toggle`, `text_field`, `slider`, `separator`, `space`, `horizontal` | The intersection every candidate backend can render. Each interactive widget returns "acted on this frame" as `bool`; no widget handles or response objects. |
| Input | `Frame::events() -> &[Event]`: `Key`, `Text`, `PointerMoved`, `PointerButton`, `PointerLeft`, `Scroll` (pixels), `Focused` | Logical pixels, delivery order. Backend-specific events (IME, touch, accessibility, clipboard) are dropped, not passed through. |
| Theme tokens | `Theme { mode, accent, background, text_size, spacing, corner_radius }`, `Color` | Tokens, not a style tree. A backend maps them onto its own style system. |
| Capability query | `gfx::capabilities() -> Capabilities` of `Support::{Supported, BestEffort, Unsupported}` | `run` validates options against it first and fails with `GfxErrorKind::Unsupported`; it never silently degrades. |

Every public enum and `Capabilities` is `#[non_exhaustive]`. The frame
context is a facade struct with a private, feature-selected implementation
field; there is no public backend trait and no `impl` of a backend trait for
a facade type.

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
