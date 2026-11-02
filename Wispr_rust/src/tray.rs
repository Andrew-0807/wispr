use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::Arc;
use parking_lot::Mutex;
use windows::core::w;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIM_ADD, NIM_DELETE, NIM_MODIFY, NIF_ICON, NIF_MESSAGE, NIF_TIP,
    NOTIFYICONDATAW,
};
use windows::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreateIconFromResourceEx, CreatePopupMenu, CreateWindowExW, DefWindowProcW,
    DestroyMenu, DestroyWindow, DispatchMessageW, GetCursorPos, GetMessageW, GetSystemMetrics,
    LoadIconW, LookupIconIdFromDirectoryEx, PostMessageW, PostQuitMessage, RegisterClassExW,
    SetForegroundWindow, TrackPopupMenu, HICON, IDI_APPLICATION, LR_DEFAULTCOLOR, MF_CHECKED,
    MF_DISABLED, MF_GRAYED, MF_POPUP, MF_SEPARATOR, MF_STRING, MF_UNCHECKED, MSG, SM_CXSMICON,
    SM_CYSMICON, TPM_BOTTOMALIGN, TPM_LEFTALIGN, TPM_RIGHTBUTTON, WM_APP, WM_COMMAND,
    WM_CONTEXTMENU, WM_DESTROY, WM_LBUTTONUP, WM_NULL, WM_RBUTTONUP, WNDCLASSEXW,
};
use crate::config::{AppConfig, CleanupMode, ProviderType};

const WM_TRAYICON: u32 = WM_APP + 1;

const IDM_TOGGLE_LLM: usize = 1001;
const IDM_PROVIDER_GEMINI: usize = 1002;
const IDM_PROVIDER_OPENROUTER: usize = 1003;
const IDM_PROVIDER_LMSTUDIO: usize = 1004;
const IDM_PROVIDER_OLLAMA: usize = 1005;
const IDM_UNLOAD_RO: usize = 1006;
const IDM_RELOAD_WORDS: usize = 1007;
const IDM_QUIT: usize = 1008;

static TRAY_IDLE_ICO: &[u8] = include_bytes!("../tray-idle.ico");
static TRAY_REC_ICO: &[u8] = include_bytes!("../tray-recording.ico");

// Set once the tray window/icons exist, so any thread can swap the icon.
static TRAY_HWND: AtomicIsize = AtomicIsize::new(0);
static ICON_IDLE: AtomicIsize = AtomicIsize::new(0);
static ICON_REC: AtomicIsize = AtomicIsize::new(0);

/// Swap the tray icon between recording (red mic) and idle (white mic).
pub fn set_recording_icon(active: bool) {
    let hwnd_raw = TRAY_HWND.load(Ordering::Relaxed);
    let icon_raw = if active { ICON_REC.load(Ordering::Relaxed) } else { ICON_IDLE.load(Ordering::Relaxed) };
    if hwnd_raw == 0 || icon_raw == 0 {
        return;
    }
    unsafe {
        let nid = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: HWND(hwnd_raw as *mut c_void),
            uID: 1,
            uFlags: NIF_ICON,
            hIcon: HICON(icon_raw as *mut c_void),
            ..Default::default()
        };
        let _ = Shell_NotifyIconW(NIM_MODIFY, &nid);
    }
}

pub struct TrayManager {
    _is_running: Arc<AtomicBool>,
}

pub struct TrayCallbacks {
    pub toggle_cleanup_mode: Arc<dyn Fn() -> CleanupMode + Send + Sync>,
    pub set_provider: Arc<dyn Fn(ProviderType) + Send + Sync>,
    pub unload_ro_model: Arc<dyn Fn() + Send + Sync>,
    pub reload_words: Arc<dyn Fn() + Send + Sync>,
    pub get_config: Arc<dyn Fn() -> AppConfig + Send + Sync>,
    pub has_ro_loaded: Arc<dyn Fn() -> bool + Send + Sync>,
    pub quit_app: Arc<dyn Fn() + Send + Sync>,
}

static TRAY_STATE: Mutex<Option<Arc<TrayCallbacks>>> = Mutex::new(None);

impl TrayManager {
    pub fn start(callbacks: TrayCallbacks) -> Self {
        let is_running = Arc::new(AtomicBool::new(true));
        let running_clone = is_running.clone();

        {
            let mut guard = TRAY_STATE.lock();
            *guard = Some(Arc::new(callbacks));
        }

        std::thread::spawn(move || {
            run_tray_loop(running_clone);
        });

        Self { _is_running: is_running }
    }
}

unsafe extern "system" fn tray_wnd_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_TRAYICON => {
            let event = lparam.0 as u32;
            if event == WM_RBUTTONUP || event == WM_LBUTTONUP || event == WM_CONTEXTMENU {
                show_tray_menu(hwnd);
            }
            LRESULT(0)
        }
        WM_COMMAND => {
            let id = (wparam.0 & 0xFFFF) as usize;
            handle_command(id, hwnd);
            LRESULT(0)
        }
        WM_DESTROY => {
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

fn hicon_from_ico(bytes: &[u8], cx: i32, cy: i32) -> HICON {
    unsafe {
        // Full .ico file: pick the best-matching image, then build the icon from its bits.
        let offset = LookupIconIdFromDirectoryEx(bytes.as_ptr(), true, cx, cy, LR_DEFAULTCOLOR);
        if offset > 0 && (offset as usize) < bytes.len() {
            if let Ok(icon) = CreateIconFromResourceEx(
                &bytes[offset as usize..],
                true,
                0x00030000,
                cx,
                cy,
                LR_DEFAULTCOLOR,
            ) {
                return icon;
            }
        }
        LoadIconW(None, IDI_APPLICATION).unwrap_or_default()
    }
}

fn run_tray_loop(is_running: Arc<AtomicBool>) {
    unsafe {
        let class_name = w!("WisprTrayClass");
        let hinstance = GetModuleHandleW(None).unwrap();

        let wnd_class = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            lpfnWndProc: Some(tray_wnd_proc),
            hInstance: hinstance.into(),
            lpszClassName: class_name,
            ..Default::default()
        };

        RegisterClassExW(&wnd_class);

        let hwnd = CreateWindowExW(
            Default::default(),
            class_name,
            w!("WisprTrayWindow"),
            Default::default(),
            0,
            0,
            0,
            0,
            HWND::default(),
            None,
            hinstance,
            None,
        ).unwrap();

        let cx = GetSystemMetrics(SM_CXSMICON);
        let cy = GetSystemMetrics(SM_CYSMICON);
        let icon_idle = hicon_from_ico(TRAY_IDLE_ICO, cx, cy);
        let icon_rec = hicon_from_ico(TRAY_REC_ICO, cx, cy);

        TRAY_HWND.store(hwnd.0 as isize, Ordering::Relaxed);
        ICON_IDLE.store(icon_idle.0 as isize, Ordering::Relaxed);
        ICON_REC.store(icon_rec.0 as isize, Ordering::Relaxed);

        let mut nid = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: hwnd,
            uID: 1,
            uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage: WM_TRAYICON,
            hIcon: icon_idle,
            ..Default::default()
        };

        let tip = "Wispr Dictation (Ctrl+Win / Shift+Win)\0";
        for (i, c) in tip.encode_utf16().enumerate() {
            if i < nid.szTip.len() {
                nid.szTip[i] = c;
            }
        }

        let _ = Shell_NotifyIconW(NIM_ADD, &nid);

        let mut msg = MSG::default();
        while is_running.load(Ordering::Relaxed) && GetMessageW(&mut msg, HWND::default(), 0, 0).as_bool() {
            DispatchMessageW(&msg);
        }

        let _ = Shell_NotifyIconW(NIM_DELETE, &nid);
        let _ = DestroyWindow(hwnd);
    }
}

unsafe fn show_tray_menu(hwnd: HWND) {
    let callbacks = {
        let guard = TRAY_STATE.lock();
        match guard.as_ref() {
            Some(cb) => cb.clone(),
            None => return,
        }
    };

    let mut cursor = POINT::default();
    let _ = GetCursorPos(&mut cursor);

    let hmenu = CreatePopupMenu().unwrap();
    let provider_menu = CreatePopupMenu().unwrap();

    let cfg = (callbacks.get_config)();
    let ro_loaded = (callbacks.has_ro_loaded)();

    // 1. LLM Toggle
    let llm_label = if cfg.cleanup_mode == CleanupMode::Llm {
        "LLM Correction: ON\0"
    } else {
        "LLM Correction: OFF\0"
    };
    let llm_utf16: Vec<u16> = llm_label.encode_utf16().collect();
    let llm_check = if cfg.cleanup_mode == CleanupMode::Llm {
        MF_CHECKED
    } else {
        MF_UNCHECKED
    };
    let _ = AppendMenuW(hmenu, MF_STRING | llm_check, IDM_TOGGLE_LLM, windows::core::PCWSTR(llm_utf16.as_ptr()));

    // 2. Providers Submenu
    let providers = [
        ("Gemini", ProviderType::Gemini, IDM_PROVIDER_GEMINI),
        ("OpenRouter", ProviderType::OpenRouter, IDM_PROVIDER_OPENROUTER),
        ("LM Studio", ProviderType::LmStudio, IDM_PROVIDER_LMSTUDIO),
        ("Ollama", ProviderType::Ollama, IDM_PROVIDER_OLLAMA),
    ];

    for (name, p_type, id) in providers {
        let check = if cfg.provider == p_type {
            MF_CHECKED
        } else {
            MF_UNCHECKED
        };
        let label: Vec<u16> = format!("{}\0", name).encode_utf16().collect();
        let _ = AppendMenuW(provider_menu, MF_STRING | check, id, windows::core::PCWSTR(label.as_ptr()));
    }

    let p_label: Vec<u16> = "Provider\0".encode_utf16().collect();
    let _ = AppendMenuW(hmenu, MF_POPUP, provider_menu.0 as usize, windows::core::PCWSTR(p_label.as_ptr()));

    let _ = AppendMenuW(hmenu, MF_SEPARATOR, 0, windows::core::PCWSTR::null());

    // 3. Romanian model status & unload
    let ro_label = if ro_loaded {
        "Romanian model: loaded (click to unload)\0"
    } else {
        "Romanian model: not loaded\0"
    };
    let ro_utf16: Vec<u16> = ro_label.encode_utf16().collect();
    let ro_flags = if ro_loaded {
        MF_STRING
    } else {
        MF_STRING | MF_DISABLED | MF_GRAYED
    };
    let _ = AppendMenuW(hmenu, ro_flags, IDM_UNLOAD_RO, windows::core::PCWSTR(ro_utf16.as_ptr()));

    // 4. Reload words.json
    let reload_utf16: Vec<u16> = "Reload words.json\0".encode_utf16().collect();
    let _ = AppendMenuW(hmenu, MF_STRING, IDM_RELOAD_WORDS, windows::core::PCWSTR(reload_utf16.as_ptr()));

    let _ = AppendMenuW(hmenu, MF_SEPARATOR, 0, windows::core::PCWSTR::null());

    // 5. Quit
    let quit_utf16: Vec<u16> = "Quit Wispr\0".encode_utf16().collect();
    let _ = AppendMenuW(hmenu, MF_STRING, IDM_QUIT, windows::core::PCWSTR(quit_utf16.as_ptr()));

    let _ = SetForegroundWindow(hwnd);
    let _ = TrackPopupMenu(
        hmenu,
        TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON,
        cursor.x,
        cursor.y,
        0,
        hwnd,
        None,
    );
    let _ = PostMessageW(hwnd, WM_NULL, WPARAM(0), LPARAM(0));

    let _ = DestroyMenu(provider_menu);
    let _ = DestroyMenu(hmenu);
}

unsafe fn handle_command(id: usize, hwnd: HWND) {
    let callbacks = {
        let guard = TRAY_STATE.lock();
        match guard.as_ref() {
            Some(cb) => cb.clone(),
            None => return,
        }
    };

    match id {
        IDM_TOGGLE_LLM => {
            let new_mode = (callbacks.toggle_cleanup_mode)();
            log::info!("Toggled cleanup mode: {:?}", new_mode);
        }
        IDM_PROVIDER_GEMINI => {
            (callbacks.set_provider)(ProviderType::Gemini);
            log::info!("Switched LLM provider to Gemini");
        }
        IDM_PROVIDER_OPENROUTER => {
            (callbacks.set_provider)(ProviderType::OpenRouter);
            log::info!("Switched LLM provider to OpenRouter");
        }
        IDM_PROVIDER_LMSTUDIO => {
            (callbacks.set_provider)(ProviderType::LmStudio);
            log::info!("Switched LLM provider to LM Studio");
        }
        IDM_PROVIDER_OLLAMA => {
            (callbacks.set_provider)(ProviderType::Ollama);
            log::info!("Switched LLM provider to Ollama");
        }
        IDM_UNLOAD_RO => {
            (callbacks.unload_ro_model)();
            log::info!("Unloaded Romanian model");
        }
        IDM_RELOAD_WORDS => {
            (callbacks.reload_words)();
            log::info!("Reloaded words.json");
        }
        IDM_QUIT => {
            log::info!("Quit requested from tray icon.");
            let nid = NOTIFYICONDATAW {
                cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
                hWnd: hwnd,
                uID: 1,
                ..Default::default()
            };
            let _ = Shell_NotifyIconW(NIM_DELETE, &nid);
            (callbacks.quit_app)();
        }
        _ => {}
    }
}
