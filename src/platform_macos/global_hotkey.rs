//! macOS backend pending (Carbon `RegisterEventHotKey`, forge-ci/kernal-api#533).
use crate::global_hotkey::{HotkeyError, HotkeyEvent, HotkeyOptions};
use tokio::sync::mpsc::Sender;

pub(crate) struct Handle;

impl Handle {
    pub(crate) fn is_active(&self) -> bool {
        false
    }
}

pub(crate) async fn register(
    options: HotkeyOptions,
    _events: Sender<HotkeyEvent>,
) -> Result<Handle, HotkeyError> {
    let _ = (options.id, options.description, options.chord);
    Err(HotkeyError::Unsupported)
}
