use std::thread;
use std::time::Duration;
use windows::Win32::System::DataExchange::{
    CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData,
};
use windows::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, VK_CONTROL, VK_LWIN,
    VK_RWIN, VK_SHIFT, VK_V,
};

pub fn inject_text(text: &str) -> anyhow::Result<()> {
    if text.is_empty() {
        return Ok(());
    }

    log::info!("Injecting text: \"{}\"", text);

    // 1. Set transcribed text as the current clipboard entry (stays at top of
    //    clipboard history — we intentionally do NOT restore the previous entry,
    //    otherwise the transcribed text lands in 2nd position).
    set_clipboard_text(text)?;

    // 2. Ensure any held modifier keys from the hotkey release are cleared
    release_modifier_keys();

    // 3. Send Ctrl+Shift+V keystroke (works in terminals too; pastes plain text elsewhere)
    send_ctrl_shift_v();

    Ok(())
}

fn release_modifier_keys() {
    unsafe {
        let inputs = [
            make_key_input(VK_LWIN.0, true),
            make_key_input(VK_RWIN.0, true),
            make_key_input(VK_SHIFT.0, true),
            make_key_input(VK_CONTROL.0, true),
        ];
        SendInput(&inputs, std::mem::size_of::<INPUT>() as i32);
        thread::sleep(Duration::from_millis(15));
    }
}

fn send_ctrl_shift_v() {
    unsafe {
        let inputs = [
            // Ctrl down
            make_key_input(VK_CONTROL.0, false),
            // Shift down
            make_key_input(VK_SHIFT.0, false),
            // V down
            make_key_input(VK_V.0, false),
            // V up
            make_key_input(VK_V.0, true),
            // Shift up
            make_key_input(VK_SHIFT.0, true),
            // Ctrl up
            make_key_input(VK_CONTROL.0, true),
        ];

        SendInput(&inputs, std::mem::size_of::<INPUT>() as i32);
    }
}

fn make_key_input(vk: u16, is_up: bool) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: windows::Win32::UI::Input::KeyboardAndMouse::VIRTUAL_KEY(vk),
                wScan: 0,
                dwFlags: if is_up {
                    KEYEVENTF_KEYUP
                } else {
                    windows::Win32::UI::Input::KeyboardAndMouse::KEYBD_EVENT_FLAGS(0)
                },
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

const CF_UNICODETEXT: u32 = 13;

pub fn set_clipboard_text(text: &str) -> anyhow::Result<()> {
    let utf16: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
    let byte_len = utf16.len() * std::mem::size_of::<u16>();

    unsafe {
        let mut retries = 0;
        while OpenClipboard(None).is_err() {
            retries += 1;
            if retries > 10 {
                return Err(anyhow::anyhow!("Failed to open clipboard for writing"));
            }
            thread::sleep(Duration::from_millis(10));
        }

        let _ = EmptyClipboard();

        let h_global = GlobalAlloc(GMEM_MOVEABLE, byte_len)?;
        let ptr = GlobalLock(h_global) as *mut u16;
        if ptr.is_null() {
            let _ = CloseClipboard();
            return Err(anyhow::anyhow!("Failed to allocate clipboard memory"));
        }

        std::ptr::copy_nonoverlapping(utf16.as_ptr(), ptr, utf16.len());
        let _ = GlobalUnlock(h_global);

        let result = SetClipboardData(CF_UNICODETEXT, windows::Win32::Foundation::HANDLE(h_global.0));
        let _ = CloseClipboard();

        if result.is_err() {
            return Err(anyhow::anyhow!("Failed to set clipboard data"));
        }

        Ok(())
    }
}
