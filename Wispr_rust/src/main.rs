#![windows_subsystem = "windows"]

mod audio;
mod config;
mod hook;
mod inject;
mod llm;
mod media;
mod overlay;
mod stt;
mod tray;

use std::fs::OpenOptions;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Instant;
use parking_lot::RwLock;
use windows::core::w;
use windows::Win32::Foundation::{CloseHandle, ERROR_ALREADY_EXISTS, GetLastError, HANDLE};
use windows::Win32::System::Threading::CreateMutexW;

use crate::audio::AudioEngine;
use crate::config::{AppConfig, CleanupMode, CustomDictionary, ProviderType};
use crate::hook::{set_pasting_state, HookEvent, KeyboardHookManager, Language};
use crate::inject::inject_text;
use crate::llm::LlmEngine;
use crate::media::MediaManager;
use crate::overlay::{OverlayManager, OverlayMode};
use crate::stt::WhisperEngine;
use crate::tray::{TrayCallbacks, TrayManager};

struct SingleInstanceGuard {
    handle: HANDLE,
}

impl SingleInstanceGuard {
    fn acquire() -> Option<Self> {
        unsafe {
            let handle = CreateMutexW(None, true, w!("Global\\WisprRustSingleInstanceMutex"));
            if handle.is_err() {
                return None;
            }
            let h = handle.unwrap();
            if GetLastError() == ERROR_ALREADY_EXISTS {
                let _ = CloseHandle(h);
                return None;
            }
            Some(Self { handle: h })
        }
    }
}

impl Drop for SingleInstanceGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.handle);
        }
    }
}

fn init_logging() {
    let mut log_path = std::path::PathBuf::from("wispr.log");
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            log_path = dir.join("wispr.log");
        }
    }

    if let Ok(file) = OpenOptions::new().create(true).append(true).open(&log_path) {
        let target = Box::new(file);
        env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
            .target(env_logger::Target::Pipe(target))
            .init();
    } else {
        env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    init_logging();
    log::info!("=========================================");
    log::info!("Starting Wispr Rust Dictation...");
    log::info!("=========================================");

    let _single_instance = match SingleInstanceGuard::acquire() {
        Some(guard) => guard,
        None => {
            log::warn!("Wispr is already running. Exiting duplicate instance.");
            std::process::exit(0);
        }
    };

    // 1. Load configuration and custom dictionary
    let config = Arc::new(RwLock::new(AppConfig::load()));
    let dictionary = Arc::new(CustomDictionary::new());

    // 2. Initialize Audio Engine
    let audio_engine = match AudioEngine::new() {
        Ok(engine) => Arc::new(engine),
        Err(e) => {
            log::error!("Failed to initialize audio engine: {}", e);
            return Err(e);
        }
    };

    // 3. Initialize Speech-to-Text Engine (SenseVoiceSmall for English, Whisper for Romanian)
    let initial_cfg = config.read().clone();
    let whisper_engine = Arc::new(WhisperEngine::new(
        initial_cfg.model_en_path.clone(),
        initial_cfg.tokens_en_path.clone(),
        initial_cfg.model_ro_path.clone(),
    ));

    // 4. Initialize LLM Post-Processing Engine
    let llm_engine = Arc::new(tokio::sync::RwLock::new(LlmEngine::new(initial_cfg)));

    // 5. Initialize Media Playback Manager
    let media_manager = Arc::new(MediaManager::new());

    // 6. Initialize Overlay Manager (60 FPS Win32 Layered Pill)
    let overlay_manager = Arc::new(OverlayManager::new(audio_engine.rms_meter()));

    // 7. Initialize System Tray Menu
    let config_tray = config.clone();
    let llm_tray = llm_engine.clone();
    let whisper_tray = whisper_engine.clone();
    let dict_tray = dictionary.clone();

    let is_running = Arc::new(AtomicBool::new(true));
    let running_quit = is_running.clone();
    let overlay_quit = overlay_manager.clone();

    let tray_callbacks = TrayCallbacks {
        toggle_cleanup_mode: Arc::new({
            let config = config_tray.clone();
            let llm = llm_tray.clone();
            move || {
                let mut cfg = config.write();
                cfg.cleanup_mode = match cfg.cleanup_mode {
                    CleanupMode::Llm => CleanupMode::Punct,
                    CleanupMode::Punct => CleanupMode::Llm,
                };
                let updated = cfg.clone();
                let llm_clone = llm.clone();
                tokio::spawn(async move {
                    let mut guard = llm_clone.write().await;
                    guard.update_config(updated);
                });
                cfg.cleanup_mode
            }
        }),
        set_provider: Arc::new({
            let config = config_tray.clone();
            let llm = llm_tray.clone();
            move |provider: ProviderType| {
                let mut cfg = config.write();
                cfg.provider = provider;
                let updated = cfg.clone();
                let llm_clone = llm.clone();
                tokio::spawn(async move {
                    let mut guard = llm_clone.write().await;
                    guard.update_config(updated);
                });
            }
        }),
        unload_ro_model: Arc::new({
            let whisper = whisper_tray.clone();
            move || {
                whisper.unload_ro();
            }
        }),
        reload_words: Arc::new({
            let dict = dict_tray.clone();
            move || {
                dict.reload();
            }
        }),
        get_config: Arc::new({
            let config = config_tray.clone();
            move || config.read().clone()
        }),
        has_ro_loaded: Arc::new({
            let whisper = whisper_tray.clone();
            move || whisper.has_ro_loaded()
        }),
        quit_app: Arc::new(move || {
            log::info!("Exiting Wispr application.");
            running_quit.store(false, Ordering::SeqCst);
            overlay_quit.stop();
            std::process::exit(0);
        }),
    };

    let _tray_manager = TrayManager::start(tray_callbacks);

    // 8. Start Global Keyboard Hook
    let (event_tx, event_rx) = channel::<HookEvent>();
    let _hook_manager = KeyboardHookManager::start(event_tx)?;

    log::info!("Wispr Rust is running. Hold [Ctrl + Win] for English or [Shift + Win] for Romanian dictation.");

    // 9. Main Event Handling Loop
    while is_running.load(Ordering::Relaxed) {
        if let Ok(event) = event_rx.recv() {
            match event {
                HookEvent::StartRecording(lang) => {
                    log::info!("--- Recording started for {:?} ---", lang);
                    overlay_manager.set_mode(OverlayMode::Recording);
                    crate::tray::set_recording_icon(true);
                    media_manager.pause_if_playing_async();
                    audio_engine.start_recording();
                }
                HookEvent::StopRecording(lang) => {
                    log::info!("--- Recording stopped for {:?} ---", lang);
                    crate::tray::set_recording_icon(false);
                    let audio_samples = audio_engine.stop_recording();

                    // Minimum 300ms of audio required (4800 samples at 16kHz)
                    if audio_samples.len() < (config::SAMPLE_RATE as f32 * 0.3) as usize {
                        log::info!("Recording too short ({} samples), ignoring.", audio_samples.len());
                        overlay_manager.set_mode(OverlayMode::Idle);
                        media_manager.resume_if_paused_async();
                        continue;
                    }

                    overlay_manager.set_mode(OverlayMode::Processing);

                    let whisper = whisper_engine.clone();
                    let llm = llm_engine.clone();
                    let dict = dictionary.clone();
                    let media = media_manager.clone();
                    let overlay = overlay_manager.clone();

                    tokio::spawn(async move {
                        let t_start = Instant::now();

                        // 1. Speech to text
                        let transcription_res = tokio::task::spawn_blocking(move || {
                            whisper.transcribe(&audio_samples, lang)
                        })
                        .await;

                        let raw_text = match transcription_res {
                            Ok(Ok(text)) => text,
                            Ok(Err(e)) => {
                                log::error!("Transcription error: {}", e);
                                overlay.set_mode(OverlayMode::Idle);
                                media.resume_if_paused_async();
                                return;
                            }
                            Err(e) => {
                                log::error!("Task join error: {}", e);
                                overlay.set_mode(OverlayMode::Idle);
                                media.resume_if_paused_async();
                                return;
                            }
                        };

                        if raw_text.trim().is_empty() {
                            log::info!("No speech detected in audio.");
                            overlay.set_mode(OverlayMode::Idle);
                            media.resume_if_paused_async();
                            return;
                        }

                        let t_stt = Instant::now();
                        log::info!("Raw [{:?}]: \"{}\" (STT latency: {}ms)", lang, raw_text, (t_stt - t_start).as_millis());

                        // 2. Dictionary substitution & AI cleanup
                        let cleaned_text = if lang == Language::English {
                            let text_after_dict = dict.apply(&raw_text);
                            let llm_guard = llm.read().await;
                            let llm_cleaned = llm_guard.cleanup_text(&text_after_dict).await;
                            dict.apply(&llm_cleaned)
                        } else {
                            dict.apply(&crate::llm::local_punct(&raw_text))
                        };

                        let t_clean = Instant::now();
                        log::info!(
                            "Cleaned: \"{}\" (Cleanup: {}ms, Total: {}ms)",
                            cleaned_text,
                            (t_clean - t_stt).as_millis(),
                            (t_clean - t_start).as_millis()
                        );

                        // 3. Inject text via simulated Ctrl+V
                        set_pasting_state(true);
                        if let Err(e) = inject_text(&cleaned_text) {
                            log::error!("Failed to inject text: {}", e);
                        }
                        set_pasting_state(false);

                        // 4. Resume media and reset overlay to idle
                        media.resume_if_paused_async();
                        overlay.set_mode(OverlayMode::Idle);
                    });
                }
            }
        }
    }

    Ok(())
}
