use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::time::Instant;
use parking_lot::Mutex;
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    VK_CONTROL, VK_LCONTROL, VK_LSHIFT, VK_LWIN, VK_RCONTROL, VK_RSHIFT, VK_RWIN, VK_SHIFT,
    VK_SPACE,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, PeekMessageW, SetWindowsHookExW,
    UnhookWindowsHookEx, HHOOK, KBDLLHOOKSTRUCT, MSG, PM_NOREMOVE, WH_KEYBOARD_LL, WM_KEYDOWN,
    WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Language {
    English,
    Romanian,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookEvent {
    StartRecording(Language),
    StopRecording(Language),
}

struct HookState {
    win_down: bool,
    ctrl_down: bool,
    shift_down: bool,
    space_down: bool,
    active_combo: Option<Language>,
    last_key_time: Option<Instant>,
    sender: Option<Sender<HookEvent>>,
}

static HOOK_STATE: Mutex<HookState> = Mutex::new(HookState {
    win_down: false,
    ctrl_down: false,
    shift_down: false,
    space_down: false,
    active_combo: None,
    last_key_time: None,
    sender: None,
});

static IS_PASTING: AtomicBool = AtomicBool::new(false);

pub fn set_pasting_state(pasting: bool) {
    IS_PASTING.store(pasting, Ordering::SeqCst);
}

pub struct KeyboardHookManager {
    _thread: std::thread::JoinHandle<()>,
}

impl KeyboardHookManager {
    pub fn start(sender: Sender<HookEvent>) -> anyhow::Result<Self> {
        {
            let mut state = HOOK_STATE.lock();
            state.sender = Some(sender);
            state.win_down = false;
            state.ctrl_down = false;
            state.shift_down = false;
            state.space_down = false;
            state.active_combo = None;
            state.last_key_time = Some(Instant::now());
        }

        let thread = std::thread::spawn(move || {
            unsafe {
                let mut msg = MSG::default();
                let _ = PeekMessageW(&mut msg, None, 0, 0, PM_NOREMOVE);

                let h_mod = GetModuleHandleW(None).unwrap_or_default();
                let hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(low_level_keyboard_proc), h_mod, 0);
                if hook.is_err() {
                    log::error!("Failed to install low level keyboard hook");
                    return;
                }
                let hook_handle = hook.unwrap();
                log::info!("Low-level keyboard hook installed successfully.");

                while GetMessageW(&mut msg, None, 0, 0).as_bool() {
                    DispatchMessageW(&msg);
                }

                let _ = UnhookWindowsHookEx(hook_handle);
            }
        });

        Ok(Self { _thread: thread })
    }
}

unsafe extern "system" fn low_level_keyboard_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    let mut suppress_key = false;

    if code >= 0 && !IS_PASTING.load(Ordering::Relaxed) {
        let kbd_struct = *(lparam.0 as *const KBDLLHOOKSTRUCT);
        let vk = kbd_struct.vkCode as u16;
        let is_down = wparam.0 == WM_KEYDOWN as usize || wparam.0 == WM_SYSKEYDOWN as usize;
        let is_up = wparam.0 == WM_KEYUP as usize || wparam.0 == WM_SYSKEYUP as usize;

        if is_down || is_up {
            let mut state = HOOK_STATE.lock();
            let now = Instant::now();

            if let Some(last_time) = state.last_key_time {
                if now.duration_since(last_time).as_secs() > 60 {
                    state.win_down = false;
                    state.ctrl_down = false;
                    state.shift_down = false;
                    state.space_down = false;
                }
            }
            state.last_key_time = Some(now);

            match vk {
                x if x == VK_LWIN.0 || x == VK_RWIN.0 => {
                    state.win_down = is_down;
                }
                x if x == VK_CONTROL.0 || x == VK_LCONTROL.0 || x == VK_RCONTROL.0 => {
                    state.ctrl_down = is_down;
                }
                x if x == VK_SHIFT.0 || x == VK_LSHIFT.0 || x == VK_RSHIFT.0 => {
                    state.shift_down = is_down;
                }
                x if x == VK_SPACE.0 => {
                    state.space_down = is_down;
                    if state.win_down {
                        suppress_key = true;
                    }
                }
                _ => {}
            }

            let combo_en = state.win_down && (state.ctrl_down || state.space_down);
            let combo_ro = state.win_down && state.shift_down && !state.ctrl_down && !state.space_down;

            let new_active = if combo_ro {
                Some(Language::Romanian)
            } else if combo_en {
                Some(Language::English)
            } else {
                None
            };

            if let (Some(lang), true) = (new_active, state.active_combo.is_none()) {
                state.active_combo = Some(lang);
                log::info!("Hotkey combo pressed: {:?} (win={}, ctrl={}, space={}, shift={})", lang, state.win_down, state.ctrl_down, state.space_down, state.shift_down);
                if let Some(ref sender) = state.sender {
                    let _ = sender.send(HookEvent::StartRecording(lang));
                }
            } else if let (true, Some(lang)) = (new_active.is_none(), state.active_combo) {
                state.active_combo = None;
                log::info!("Hotkey combo released: {:?} (win={}, ctrl={}, space={}, shift={})", lang, state.win_down, state.ctrl_down, state.space_down, state.shift_down);
                if let Some(ref sender) = state.sender {
                    let _ = sender.send(HookEvent::StopRecording(lang));
                }
            }
        }
    }

    if suppress_key {
        LRESULT(1)
    } else {
        CallNextHookEx(HHOOK::default(), code, wparam, lparam)
    }
}
