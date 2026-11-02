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
    ///
    /// When no widget holds keyboard focus, the first text field drawn in a
    /// frame takes it, so a window whose UI is one field accepts typing
    /// immediately.
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

    /// A vertically scrolling region `height` logical pixels tall and as wide
    /// as the layout. Content taller than the region is clipped and scrolls
    /// with the wheel, drag or scroll bar.
    pub fn scroll<R>(&mut self, height: f32, contents: impl FnOnce(&mut Frame<'_>) -> R) -> R {
        let mut contents = Some(contents);
        let mut result = None;
        self.imp.scroll(height, &mut |imp| {
            if let Some(contents) = contents.take() {
                result = Some(contents(&mut Frame { imp }));
            }
        });
        match result {
            Some(result) => result,
            None => unreachable!("the backend runs the scroll body exactly once"),
        }
    }

    /// Lay out a list: rows added by `contents` are stacked with half the
    /// theme spacing between them. Use [`Frame::separator`] between groups.
    pub fn list<R>(&mut self, contents: impl FnOnce(&mut Frame<'_>) -> R) -> R {
        let mut contents = Some(contents);
        let mut result = None;
        self.imp.list(&mut |imp| {
            if let Some(contents) = contents.take() {
                result = Some(contents(&mut Frame { imp }));
            }
        });
        match result {
            Some(result) => result,
            None => unreachable!("the backend runs the list body exactly once"),
        }
    }

    /// A full-width row showing `text`, highlighted with the theme accent
    /// when `selected`, with hover and press feedback; `true` when clicked.
    ///
    /// Inside [`Frame::scroll`], a row scrolls itself into view on the frame
    /// it becomes selected, so keyboard navigation keeps the selection
    /// visible without fighting the user's own scrolling.
    pub fn selectable_row(&mut self, text: &str, selected: bool) -> bool {
        self.imp.selectable_row(text, selected)
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
