use std::os::windows::process::CommandExt;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

// Prevent the child powershell process from flashing a console window (which also
// steals foreground focus and breaks the Ctrl+V text injection).
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const PS_AWAIT: &str = r#"
function Await($WinRtTask, $ResultType) {
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
"#;

const PS_PAUSE: &str = r#"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$async = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
$manager = Await $async ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
if (-not $manager) { exit 1 }
$sessions = $manager.GetSessions()
foreach ($s in $sessions) {
    $info = $s.GetPlaybackInfo()
    if ($info.PlaybackStatus -eq 'Playing') { $s.TryPauseAsync() | Out-Null; Write-Host 'PAUSED' }
}
"#;

const PS_RESUME: &str = r#"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$async = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
$manager = Await $async ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
if (-not $manager) { exit 1 }
$sessions = $manager.GetSessions()
foreach ($s in $sessions) {
    $info = $s.GetPlaybackInfo()
    if ($info.PlaybackStatus -eq 'Paused') { $s.TryPlayAsync() | Out-Null; Write-Host 'PLAYING' }
}
"#;

#[derive(Clone)]
pub struct MediaManager {
    media_was_paused: Arc<AtomicBool>,
}

impl MediaManager {
    pub fn new() -> Self {
        Self {
            media_was_paused: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn pause_if_playing_async(&self) {
        let flag = self.media_was_paused.clone();
        std::thread::spawn(move || {
            let script = format!("{}{}", PS_AWAIT, PS_PAUSE);
            if let Ok(output) = Command::new("powershell")
                .args(["-NoProfile", "-NonInteractive", "-Command", &script])
                .creation_flags(CREATE_NO_WINDOW)
                .output()
            {
                let text = String::from_utf8_lossy(&output.stdout);
                if text.contains("PAUSED") {
                    log::info!("Active media playback detected and paused.");
                    flag.store(true, Ordering::SeqCst);
                } else {
                    flag.store(false, Ordering::SeqCst);
                }
            }
        });
    }

    pub fn resume_if_paused_async(&self) {
        let flag = self.media_was_paused.clone();
        std::thread::spawn(move || {
            if !flag.load(Ordering::SeqCst) {
                return;
            }
            flag.store(false, Ordering::SeqCst);

            let script = format!("{}{}", PS_AWAIT, PS_RESUME);
            if let Ok(output) = Command::new("powershell")
                .args(["-NoProfile", "-NonInteractive", "-Command", &script])
                .creation_flags(CREATE_NO_WINDOW)
                .output()
            {
                let text = String::from_utf8_lossy(&output.stdout);
                if text.contains("PLAYING") {
                    log::info!("Media playback resumed.");
                }
            }
        });
    }
}
