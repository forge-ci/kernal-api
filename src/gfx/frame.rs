//! The per-frame context.

use std::ops::RangeInclusive;

use super::backend::FrameImp;
use super::{Event, Theme};

/// One frame of UI. Widgets are laid out top to bottom unless nested in
/// [`Frame::horizontal`]. Each widget returns whether the user acted on it
/// this frame.
pub struct Frame<'a> {
    pub(crate) imp: FrameImp<'a>,
}

impl Frame<'_> {
    /// Input events received since the previous frame.
    #[must_use]
    pub fn events(&self) -> &[Event] {
        self.imp.events()
    }

    /// Show text.
    pub fn label(&mut self, text: &str) {
        self.imp.label(text);
    }

    /// Show heading-sized text.
    pub fn heading(&mut self, text: &str) {
        self.imp.heading(text);
    }

    /// A push button; `true` when clicked this frame.
    pub fn button(&mut self, text: &str) -> bool {
        self.imp.button(text)
    }

    /// A labelled on/off toggle bound to `value`; `true` when changed.
    pub fn toggle(&mut self, value: &mut bool, text: &str) -> bool {
        self.imp.toggle(value, text)
    }

    /// A single-line text field bound to `value`; `true` when edited.
    pub fn text_field(&mut self, value: &mut String) -> bool {
        self.imp.text_field(value)
    }

    /// A labelled slider bound to `value` within `range`; `true` when moved.
    pub fn slider(&mut self, value: &mut f64, range: RangeInclusive<f64>, text: &str) -> bool {
        self.imp.slider(value, range, text)
    }

    /// A horizontal rule.
    pub fn separator(&mut self) {
        self.imp.separator();
    }

    /// Empty space of `amount` logical pixels along the layout direction.
    pub fn space(&mut self, amount: f32) {
        self.imp.space(amount);
    }

    /// Lay out the widgets added by `contents` left to right.
    pub fn horizontal<R>(&mut self, contents: impl FnOnce(&mut Frame<'_>) -> R) -> R {
        let mut contents = Some(contents);
        let mut result = None;
        self.imp.horizontal(&mut |imp| {
            if let Some(contents) = contents.take() {
                result = Some(contents(&mut Frame { imp }));
            }
        });
        match result {
            Some(result) => result,
            None => unreachable!("the backend runs the horizontal body exactly once"),
        }
    }

    /// Replace the theme from the next frame on.
    pub fn set_theme(&mut self, theme: Theme) {
        self.imp.set_theme(theme);
    }

    /// Close the window after this frame (does not consult
    /// [`App::close_requested`](super::App::close_requested)).
    pub fn close(&mut self) {
        self.imp.close();
    }

    /// Schedule another frame even without input (for animation).
    pub fn request_repaint(&mut self) {
        self.imp.request_repaint();
    }
}
