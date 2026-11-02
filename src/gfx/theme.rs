//! Theme tokens.

/// An sRGB colour with straight (unpremultiplied) alpha.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Color {
    /// Red.
    pub r: u8,
    /// Green.
    pub g: u8,
    /// Blue.
    pub b: u8,
    /// Alpha; 0 is fully transparent.
    pub a: u8,
}

impl Color {
    /// An opaque colour.
    #[must_use]
    pub const fn rgb(r: u8, g: u8, b: u8) -> Self {
        Self { r, g, b, a: 255 }
    }

    /// A colour with alpha.
    #[must_use]
    pub const fn rgba(r: u8, g: u8, b: u8, a: u8) -> Self {
        Self { r, g, b, a }
    }

    /// Fully transparent.
    pub const TRANSPARENT: Self = Self::rgba(0, 0, 0, 0);
}

/// Light or dark base palette.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
#[non_exhaustive]
pub enum ThemeMode {
    /// Dark backgrounds, light text.
    #[default]
    Dark,
    /// Light backgrounds, dark text.
    Light,
}

/// The portable styling tokens. A backend maps each token onto its own
/// style system; anything finer-grained is deliberately not portable.
#[derive(Clone, Copy, Debug, PartialEq)]
#[non_exhaustive]
pub struct Theme {
    /// Base palette.
    pub mode: ThemeMode,
    /// Selection, focus and active-widget colour.
    pub accent: Color,
    /// Window background; `None` keeps the palette default. Use
    /// [`Color::TRANSPARENT`] with a transparent window.
    pub background: Option<Color>,
    /// Body text size in logical pixels.
    pub text_size: f32,
    /// Spacing between widgets in logical pixels.
    pub spacing: f32,
    /// Widget corner radius in logical pixels.
    pub corner_radius: f32,
}

impl Default for Theme {
    fn default() -> Self {
        Self {
            mode: ThemeMode::Dark,
            accent: Color::rgb(0x3d, 0x8b, 0xfd),
            background: None,
            text_size: 14.0,
            spacing: 8.0,
            corner_radius: 4.0,
        }
    }
}

impl Theme {
    /// The default tokens with `mode`.
    #[must_use]
    pub fn with_mode(mode: ThemeMode) -> Self {
        Self {
            mode,
            ..Self::default()
        }
    }
}
