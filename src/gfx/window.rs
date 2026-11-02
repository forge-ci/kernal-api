//! Window description.

use super::{Capabilities, GfxError, GfxErrorKind, Support, Theme};

/// Where the window first appears.
#[derive(Clone, Copy, Debug, PartialEq, Default)]
#[non_exhaustive]
pub enum Placement {
    /// Wherever the windowing system puts a new window.
    #[default]
    Default,
    /// Centred on the primary monitor.
    Centered,
    /// Outer top-left corner at this position, in logical pixels of the
    /// virtual desktop.
    At {
        /// Horizontal position.
        x: f32,
        /// Vertical position.
        y: f32,
    },
    /// On the monitor with this enumeration index.
    Monitor {
        /// Zero-based monitor index.
        index: u32,
    },
}

/// Everything [`run`](super::run) needs to create the window.
#[derive(Clone, Debug, PartialEq)]
#[must_use]
pub struct WindowOptions {
    pub(crate) title: String,
    pub(crate) size: (f32, f32),
    pub(crate) resizable: bool,
    pub(crate) decorations: bool,
    pub(crate) always_on_top: bool,
    pub(crate) transparent: bool,
    pub(crate) click_through: bool,
    pub(crate) placement: Placement,
    pub(crate) theme: Theme,
}

impl WindowOptions {
    /// A decorated, resizable 640x480 window with the default theme.
    pub fn new(title: impl Into<String>) -> Self {
        Self {
            title: title.into(),
            size: (640.0, 480.0),
            resizable: true,
            decorations: true,
            always_on_top: false,
            transparent: false,
            click_through: false,
            placement: Placement::Default,
            theme: Theme::default(),
        }
    }

    /// Inner size in logical pixels.
    pub fn size(mut self, width: f32, height: f32) -> Self {
        self.size = (width, height);
        self
    }

    /// Whether the user may resize the window.
    pub fn resizable(mut self, resizable: bool) -> Self {
        self.resizable = resizable;
        self
    }

    /// Whether the window has a title bar and border.
    pub fn decorations(mut self, decorations: bool) -> Self {
        self.decorations = decorations;
        self
    }

    /// Keep the window above ordinary windows.
    pub fn always_on_top(mut self, always_on_top: bool) -> Self {
        self.always_on_top = always_on_top;
        self
    }

    /// Composite the window with a transparent background.
    pub fn transparent(mut self, transparent: bool) -> Self {
        self.transparent = transparent;
        self
    }

    /// Let pointer input pass through the window to whatever is below.
    pub fn click_through(mut self, click_through: bool) -> Self {
        self.click_through = click_through;
        self
    }

    /// Initial placement.
    pub fn placement(mut self, placement: Placement) -> Self {
        self.placement = placement;
        self
    }

    /// Initial theme; [`Frame::set_theme`](super::Frame::set_theme) changes it later.
    pub fn theme(mut self, theme: Theme) -> Self {
        self.theme = theme;
        self
    }

    pub(crate) fn validate(&self, caps: &Capabilities) -> Result<(), GfxError> {
        let (width, height) = self.size;
        if !(width.is_finite() && height.is_finite() && width > 0.0 && height > 0.0) {
            return Err(GfxError::new(
                GfxErrorKind::InvalidOptions,
                format!("window size must be positive and finite, got {width}x{height}"),
            ));
        }
        let requested = [
            (self.transparent, caps.transparent, "transparent"),
            (self.click_through, caps.click_through, "click_through"),
            (self.always_on_top, caps.always_on_top, "always_on_top"),
            (!self.decorations, caps.undecorated, "decorations(false)"),
            (
                matches!(self.placement, Placement::At { .. }),
                caps.absolute_placement,
                "Placement::At",
            ),
            (
                matches!(self.placement, Placement::Monitor { .. }),
                caps.monitor_placement,
                "Placement::Monitor",
            ),
        ];
        for (wanted, support, name) in requested {
            if wanted && support == Support::Unsupported {
                return Err(GfxError::new(
                    GfxErrorKind::Unsupported,
                    format!("{name} is not supported by the {:?} gfx backend", caps.backend),
                ));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gfx::BackendKind;

    fn all_best_effort() -> Capabilities {
        Capabilities {
            backend: BackendKind::Egui,
            transparent: Support::BestEffort,
            click_through: Support::BestEffort,
            always_on_top: Support::BestEffort,
            undecorated: Support::Supported,
            absolute_placement: Support::BestEffort,
            monitor_placement: Support::Unsupported,
        }
    }

    #[test]
    fn rejects_a_degenerate_size() {
        let err = WindowOptions::new("t")
            .size(0.0, 10.0)
            .validate(&all_best_effort())
            .expect_err("zero width must be rejected");
        assert_eq!(err.kind(), GfxErrorKind::InvalidOptions);
        let err = WindowOptions::new("t")
            .size(f32::NAN, 10.0)
            .validate(&all_best_effort())
            .expect_err("NaN width must be rejected");
        assert_eq!(err.kind(), GfxErrorKind::InvalidOptions);
    }

    #[test]
    fn rejects_an_unsupported_capability_and_accepts_best_effort() {
        let caps = all_best_effort();
        let ok = WindowOptions::new("t")
            .transparent(true)
            .click_through(true)
            .always_on_top(true)
            .decorations(false)
            .placement(Placement::At { x: 1.0, y: 2.0 });
        assert!(ok.validate(&caps).is_ok());
        let err = WindowOptions::new("t")
            .placement(Placement::Monitor { index: 1 })
            .validate(&caps)
            .expect_err("monitor placement is unsupported here");
        assert_eq!(err.kind(), GfxErrorKind::Unsupported);
    }
}
