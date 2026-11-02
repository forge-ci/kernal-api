//! Input events delivered to [`Frame::events`](super::Frame::events).

/// Modifier keys held during an event.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
#[allow(clippy::struct_excessive_bools, reason = "one flag per physical modifier")]
pub struct Modifiers {
    /// Shift.
    pub shift: bool,
    /// Control.
    pub ctrl: bool,
    /// Alt / Option.
    pub alt: bool,
    /// The platform command key: Command on macOS, Control elsewhere.
    pub command: bool,
}

/// A logical key. Text entry arrives separately as [`Event::Text`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum Key {
    /// A printable key, as its unshifted character (`'a'`, `'1'`, `'/'`).
    Char(char),
    /// Enter / Return.
    Enter,
    /// Escape.
    Escape,
    /// Tab.
    Tab,
    /// Backspace.
    Backspace,
    /// Delete.
    Delete,
    /// Space bar.
    Space,
    /// Up arrow.
    ArrowUp,
    /// Down arrow.
    ArrowDown,
    /// Left arrow.
    ArrowLeft,
    /// Right arrow.
    ArrowRight,
    /// Home.
    Home,
    /// End.
    End,
    /// Page Up.
    PageUp,
    /// Page Down.
    PageDown,
    /// Function key F1..=F35.
    F(u8),
    /// A key with no portable name.
    Other,
}

/// A pointer button.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum PointerButton {
    /// Usually the left button.
    Primary,
    /// Usually the right button.
    Secondary,
    /// Usually the wheel button.
    Middle,
    /// Any other button.
    Other,
}

/// One input event, in delivery order. Positions are logical pixels from the
/// window's inner top-left corner.
#[derive(Clone, Debug, PartialEq)]
#[non_exhaustive]
pub enum Event {
    /// A key changed state.
    Key {
        /// Which key.
        key: Key,
        /// `true` on press (including auto-repeat), `false` on release.
        pressed: bool,
        /// Whether this press is an auto-repeat.
        repeat: bool,
        /// Modifiers held.
        modifiers: Modifiers,
    },
    /// Committed text input.
    Text(String),
    /// The pointer moved.
    PointerMoved {
        /// Horizontal position.
        x: f32,
        /// Vertical position.
        y: f32,
    },
    /// A pointer button changed state.
    PointerButton {
        /// Which button.
        button: PointerButton,
        /// `true` on press.
        pressed: bool,
        /// Horizontal position.
        x: f32,
        /// Vertical position.
        y: f32,
        /// Modifiers held.
        modifiers: Modifiers,
    },
    /// The pointer left the window.
    PointerLeft,
    /// Scroll, in logical pixels (line-based wheels are converted by the backend).
    Scroll {
        /// Horizontal delta.
        dx: f32,
        /// Vertical delta.
        dy: f32,
    },
    /// The window gained (`true`) or lost focus.
    Focused(bool),
}
