//! `gfx` docks are not implemented on this host yet.
//!
//! The native shape is known (macOS: an `NSPanel` at status-bar window level
//! with the visible frame adjusted; Windows: an `SHAppBarMessage` AppBar);
//! neither is wired to the egui backend, so the capability is reported
//! honestly instead of approximated with an always-on-top window.
use crate::gfx::{App, Edge, GfxError, GfxErrorKind, Support, WindowOptions};

pub(crate) fn support() -> Support {
    Support::Unsupported
}

pub(crate) fn run(
    _options: &WindowOptions,
    _edge: Edge,
    _thickness: f32,
    _app: Box<dyn App>,
) -> Result<(), GfxError> {
    Err(GfxError::new(
        GfxErrorKind::Unsupported,
        "Placement::Dock is not implemented on this host yet",
    ))
}
