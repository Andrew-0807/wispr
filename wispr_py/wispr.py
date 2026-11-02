#!/usr/bin/env python3
from __future__ import annotations

"""
Wispr - Windows voice dictation tool
Hold the Wispr hotkey to record, release to transcribe and paste.
"""

import os
import sys
import tempfile
import subprocess
import numpy as _np


def _check_single_instance() -> bool:
    """Returns True if another instance is already running."""
    lock_dir = tempfile.gettempdir()
    lock_file = os.path.join(lock_dir, "wispr.lock")

    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                return True
            except OSError:
                pass
        except (ValueError, OSError):
            pass

    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))
    return False


_BENCH_MODE = "--bench" in sys.argv

if not _BENCH_MODE and _check_single_instance():
    sys.exit("Wispr is already running. Exiting.")

# ── Stdlib-only imports at module level (fast) ────────────────────────────────
import re
import json
import time
import threading
import queue
import math
import atexit
import urllib.request
import urllib.error

# Heavy third-party imports are deferred to main() below.
# Module-level names used in type hints / global state use forward references.

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16_000
CHANNELS = 1
WHISPER_MODEL = "small.en"
WHISPER_MODEL_RO = "large-v3-turbo"  # multilingual model for Romanian
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE = "float16"
BEAM_SIZE = 1

GEMINI_MODEL = "gemini-2.0-flash"
OPENROUTER_DEFAULT = "nvidia/nemotron-3-super-120b-a12b:free"
LMSTUDIO_DEFAULT = (
    "qwen2.5-1.5b-instruct"  # fast, ~1 GB — ideal for grammar-only cleanup
)
OLLAMA_DEFAULT = "qwen2.5:1.5b"  # pull with: ollama pull qwen2.5:1.5b

CLEANUP_SYSTEM = (
    "You are a voice transcription cleanup assistant. "
    "The user will give you raw speech-to-text output. "
    "Your job: remove filler words (um, uh, like, you know, etc.) and fix punctuation/capitalization. "
    "CRITICAL: preserve the user's exact words, phrasing, and meaning. "
    "Do NOT rephrase, reword, restructure, or 'improve' the text. "
    "Only remove filler words and add punctuation — change nothing else. "
    "Return ONLY the cleaned text. No explanations, no quotes, no preamble."
)

# ── Overlay geometry ──────────────────────────────────────────────────────────

OVERLAY_W = 120  # total pill width
OVERLAY_H = 34  # pill height  (radius = H/2 for true pill)
TASKBAR_CLEAR = 24  # px above screen bottom

# pill background
KEY_COLOR = "#010002"  # unique near-black used as the transparent key
PILL_FILL = (0, 0, 0, 230)  # RGBA – dark navy, ~90 % opaque

# listening dots
NUM_DOTS = 16  # more dots = finer visualiser
DOT_R = 2  # horizontal radius (fixed)
DOT_MAX_H = 14  # max vertical radius when loud
DOT_GAP = 1.5  # gap between dot edges

# spinner (processing)
SPINNER_DOTS = 8
SPINNER_R = 9  # radius of spinner circle
SPINNER_DOT_R = 2  # radius of each spinner dot

# rendering
RENDER_SCALE = 4  # supersample factor — drawn at 4× then LANCZOS ↓

COLOR_REC = "#818cf8"  # indigo-400  – recording
COLOR_PROC = "#fb923c"  # orange      – processing

# ── Global state (populated after deferred imports in main()) ─────────────────

recording = False
audio_chunks: list = []
audio_level = 0.0
tray_icon = None  # pystray.Icon — set in main()
_media_was_paused = False
_audio_stream = None
_rec_lock = threading.Lock()
_media_paused_event = threading.Event()
_pasting = False
_keyboard_hook_ref = None

status_queue: queue.Queue = queue.Queue()  # "idle" | "recording" | "processing"

# Deferred import handles (set in main())
np = None  # numpy
sd = None  # sounddevice
pyautogui = None
pyperclip = None
keyboard = None
_PIL_Image = None
_PIL_ImageDraw = None
_PIL_ImageTk = None
_pystray_Icon = None
_pystray_MenuItem = None
_pystray_Menu = None

# ── Env / provider state ──────────────────────────────────────────────────────

LLM_PROVIDER = "gemini"  # overwritten after load_dotenv() in main()
LLM_FALLBACK = ""
CLEANUP_MODE = "punct"
OPENROUTER_MODEL = OPENROUTER_DEFAULT

gemini_client = None
openrouter_api_key = ""
lmstudio_url = ""
lmstudio_model = ""
lmstudio_api_key = ""
ollama_url = ""
ollama_model = ""


def _release_single_instance():
    lock_file = os.path.join(tempfile.gettempdir(), "wispr.lock")
    try:
        os.remove(lock_file)
    except OSError:
        pass


atexit.register(_release_single_instance)


def _setup_provider(name: str, fatal: bool = True) -> bool:
    global \
        gemini_client, \
        openrouter_api_key, \
        lmstudio_url, \
        lmstudio_model, \
        lmstudio_api_key, \
        ollama_url, \
        ollama_model
    label = "primary" if fatal else "fallback"

    def _fail(msg):
        if fatal:
            sys.exit(f"ERROR: {msg}")
        print(f"WARNING: fallback {name} skipped — {msg}")
        return False

    if name == "gemini":
        try:
            import google.generativeai as genai
        except ImportError:
            return _fail("google-generativeai not installed.")
        _key = os.getenv("GEMINI_API_KEY", "")
        if not _key or _key == "your_gemini_api_key_here":
            return _fail("GEMINI_API_KEY not set in .env.")
        genai.configure(api_key=_key)
        gemini_client = genai.GenerativeModel(
            model_name=GEMINI_MODEL, system_instruction=CLEANUP_SYSTEM
        )
        return True

    if name == "openrouter":
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            return _fail("OPENROUTER_API_KEY not set in .env.")
        return True

    if name == "lmstudio":
        lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1")
        lmstudio_model = os.getenv("LMSTUDIO_MODEL", LMSTUDIO_DEFAULT)
        lmstudio_api_key = os.getenv("LMSTUDIO_API_KEY", "")
        return True

    if name == "ollama":
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
        ollama_model = os.getenv("OLLAMA_MODEL", OLLAMA_DEFAULT)
        print(f"LLM {label}: Ollama ({ollama_model} @ {ollama_url})")
        return True

    return _fail(
        f"unknown provider '{name}'. Use gemini, openrouter, lmstudio, or ollama."
    )


whisper = None  # loaded in background thread
whisper_ready = threading.Event()
whisper_error = None

whisper_ro = None  # Romanian model — loaded on first Shift+Win press
whisper_ro_lock = threading.Lock()
whisper_ro_loading = False


def _cuda_gpu():
    """Check for CUDA GPU via nvidia-smi. Returns GPU name or None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _ensure_whisper_ro():
    """Load Romanian whisper model on first use. Returns model or None."""
    global whisper_ro, whisper_ro_loading
    if whisper_ro is not None:
        return whisper_ro
    with whisper_ro_lock:
        if whisper_ro is not None:
            return whisper_ro
        if whisper_ro_loading:
            return None
        whisper_ro_loading = True
    print("Loading Romanian model…")
    from faster_whisper import WhisperModel

    attempts = []
    if _cuda_gpu():
        attempts.append((WHISPER_DEVICE, WHISPER_COMPUTE))
    attempts.append(("cpu", "int8"))

    for device, compute_type in attempts:
        try:
            whisper_ro = WhisperModel(
                WHISPER_MODEL_RO, device=device, compute_type=compute_type
            )
            print(f"Romanian model loaded on {device}")
            whisper_ro_loading = False
            return whisper_ro
        except Exception as exc:
            last_error = exc

    print(f"ERROR: Romanian model failed to load: {last_error}")
    whisper_ro_loading = False
    return None


def _unload_whisper_ro(icon=None, item=None):
    """Unload Romanian model to free VRAM."""
    global whisper_ro
    if whisper_ro is None:
        print("Romanian model not loaded.")
        return
    whisper_ro = None
    print("Romanian model unloaded.")
    if icon:
        icon.update_menu()


# ── Word map ──────────────────────────────────────────────────────────────────


def _load_word_map() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "words.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _apply_word_map(text: str, wmap: dict) -> str:
    # longest phrases first so "Claude Code" matches before "Claude"
    for key in sorted(wmap, key=len, reverse=True):
        text = re.sub(
            r"\b" + re.escape(key) + r"\b", wmap[key], text, flags=re.IGNORECASE
        )
    return text


WORD_MAP = _load_word_map()

# ── Tray helpers ──────────────────────────────────────────────────────────────

ICON_IDLE = None
ICON_RECORDING = None
ICON_PROCESSING = None


_icon_cache: dict = {}


def _make_mic_icon(color: str):
    if color in _icon_cache:
        return _icon_cache[color]

    # Parse hex colour
    hex_c = color.lstrip("#")
    r = int(hex_c[0:2], 16)
    g = int(hex_c[2:4], 16)
    b = int(hex_c[4:6], 16)

    # Draw a high-quality microphone icon programmatically
    S = 64  # icon size
    img = _PIL_Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = _PIL_ImageDraw.Draw(img)

    # ── Capsule body ──────────────────────────────────────────────────────────
    cap_w = 20
    cap_h = 36
    cap_x0 = (S - cap_w) // 2
    cap_y0 = 6
    draw.rounded_rectangle(
        [cap_x0, cap_y0, cap_x0 + cap_w, cap_y0 + cap_h],
        radius=cap_w // 2,
        fill=(r, g, b),
    )

    # ── Grid lines on capsule ────────────────────────────────────────────────
    grid_col = (max(0, r - 40), max(0, g - 40), max(0, b - 40), 200)
    for dy in [10, 18, 26]:
        y = cap_y0 + dy
        draw.arc(
            [cap_x0, y - 1, cap_x0 + cap_w, y + 1],
            0, 180,
            fill=grid_col,
            width=1,
        )

    # ── Collar band ───────────────────────────────────────────────────────────
    collar_y0 = cap_y0 + cap_h - 1
    collar_y1 = collar_y0 + 4
    draw.rectangle(
        [cap_x0, collar_y0, cap_x0 + cap_w, collar_y1],
        fill=(r, g, b),
    )

    # ── U-shaped yoke (arc) ───────────────────────────────────────────────────
    yoke_y0 = cap_y0 + cap_h + 4
    yoke_y1 = yoke_y0 + 10
    draw.arc(
        [cap_x0 - 10, yoke_y0, cap_x0 + cap_w + 10, yoke_y1],
        0, 180,
        fill=(r, g, b),
        width=3,
    )

    # ── Stem (vertical line) ───────────────────────────────────────────────────
    stem_x = S // 2
    stem_y0 = yoke_y0 + 5
    stem_y1 = stem_y0 + 6
    draw.line(
        [stem_x, stem_y0, stem_x, stem_y1],
        fill=(r, g, b),
        width=3,
    )

    # ── Base (horizontal foot) ─────────────────────────────────────────────────
    base_y0 = stem_y1 - 1
    base_y1 = base_y0 + 3
    draw.arc(
        [stem_x - 8, base_y0, stem_x + 8, base_y1],
        190, 350,
        fill=(r, g, b),
        width=3,
    )

    _icon_cache[color] = img
    return img


def _update_tray(state: str):
    if tray_icon is None:
        return
    if state == "recording":
        tray_icon.icon = ICON_RECORDING
        tray_icon.title = "Wispr – Recording…"
    elif state == "processing":
        tray_icon.icon = ICON_PROCESSING
        tray_icon.title = "Wispr – Transcribing…"
    else:
        tray_icon.icon = ICON_IDLE
        tray_icon.title = "Wispr – Idle (hold the Wispr hotkey to record)"


# ── Audio callback ────────────────────────────────────────────────────────────


def audio_callback(indata, frames: int, time_info, status):
    global audio_level
    rms = math.sqrt(np.dot(indata.ravel(), indata.ravel()) / len(indata))
    with _rec_lock:
        if recording:
            # Cap at 30 minutes of recording (~28,125 chunks at 1024 blocksize) to prevent memory issues
            if len(audio_chunks) < 30000:
                audio_chunks.append(indata.copy())
        audio_level = rms


# ── LLM cleanup ───────────────────────────────────────────────────────────────


def _openai_compat_cleanup(
    base_url: str, model: str, raw: str, api_key: str = ""
) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CLEANUP_SYSTEM},
            {"role": "user", "content": raw},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=data, headers=headers
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    body = resp.read().decode("utf-8")
    return json.loads(body)["choices"][0]["message"]["content"].strip()


def _call_provider(name: str, raw: str) -> str:
    delays = [0.5, 1.0, 2.0]
    for attempt in range(3):
        try:
            if name == "gemini":
                return gemini_client.generate_content(raw).text.strip()
            if name == "openrouter":
                return _openai_compat_cleanup(
                    "https://openrouter.ai/api/v1", OPENROUTER_MODEL, raw, openrouter_api_key
                )
            if name == "lmstudio":
                return _openai_compat_cleanup(lmstudio_url, lmstudio_model, raw, lmstudio_api_key)
            if name == "ollama":
                return _openai_compat_cleanup(ollama_url, ollama_model, raw)
            raise ValueError(f"Unknown provider: {name}")
        except Exception as exc:
            if attempt < 2 and (
                isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 500, 502, 503, 504)
                or isinstance(exc, urllib.error.URLError)
                or "Connection" in type(exc).__name__
            ):
                time.sleep(delays[attempt])
                continue
            raise


def _local_punct(text: str) -> str:
    """Capitalize first letter and ensure ending punctuation. Nothing else."""
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?,":
        text += "."
    return text


def cleanup_text(raw: str) -> str:
    if CLEANUP_MODE == "punct":
        return _local_punct(raw)
    try:
        return _call_provider(LLM_PROVIDER, raw)
    except Exception:
        if LLM_FALLBACK:
            return _call_provider(LLM_FALLBACK, raw)
        raise


# ── Transcription + cleanup ────────────────────────────────────────────────────


def transcribe_and_paste(audio, lang="en"):
    t_start = time.perf_counter()
    status_queue.put("processing")

    if lang == "ro":
        model = _ensure_whisper_ro()
        if model is None:
            print("Romanian model failed to load; skipping.")
            status_queue.put("idle")
            return
    else:
        model = whisper
        if model is None:
            print("Whisper is not ready; skipping transcription.")
            status_queue.put("idle")
            return

    audio_f32 = audio.flatten().astype(np.float32)
    segments, _ = model.transcribe(
        audio_f32, beam_size=BEAM_SIZE, vad_filter=False,
        language=lang if lang == "ro" else None,
    )
    raw = " ".join(seg.text.strip() for seg in segments).strip()
    t_whisper = time.perf_counter()

    if not raw:
        print("Nothing detected.")
        status_queue.put("idle")
        return

    print(f"Raw [{lang}]: {raw}")
    print(f"  [whisper]  {(t_whisper - t_start) * 1000:.0f}ms")

    if lang == "en":
        try:
            cleaned = cleanup_text(raw)
        except Exception as exc:
            print(f"LLM error ({LLM_PROVIDER}): {exc}  —  using raw transcript.")
            cleaned = raw
        t_llm = time.perf_counter()
        print(f"  [llm]      {(t_llm - t_whisper) * 1000:.0f}ms")

        if WORD_MAP:
            cleaned = _apply_word_map(cleaned, WORD_MAP)
    else:
        cleaned = _local_punct(raw)

    print(f"Cleaned: {cleaned}")
    print(f"  [total]    {(time.perf_counter() - t_start) * 1000:.0f}ms")

    global _pasting
    pyperclip.copy(cleaned)
    _pasting = True
    pyautogui.hotkey("ctrl", "shift", "v")
    _pasting = False
    _resume_media()

    status_queue.put("idle")


# ── Media state helpers ───────────────────────────────────────────────────────


# ── Media state helpers (PowerShell-based) ─────────────────────────────────

_PS_PAUSE_PLAYING = r"""
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
"""

_PS_RESUME_PAUSED = r"""
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
"""

_POWERSHELL_AWAIT = r"""
function Await($WinRtTask, $ResultType) {
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
"""

def _run_ps_media(action: str = "pause") -> str:
    """Run PowerShell media control, return result."""
    body = _PS_PAUSE_PLAYING if action == "pause" else _PS_RESUME_PAUSED
    script = _POWERSHELL_AWAIT + body
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"Media control error: {e}")
        return ""


def _pause_media_if_playing():
    global _media_was_paused
    _media_paused_event.clear()
    _media_was_paused = False
    result = _run_ps_media("pause")
    _media_was_paused = (result == "PAUSED")
    _media_paused_event.set()


def _resume_media():
    global _media_was_paused
    _media_paused_event.wait(timeout=5)
    if not _media_was_paused:
        return
    result = _run_ps_media("resume")
    if result == "PLAYING":
        print("Media resumed")
    _media_was_paused = False


# ── Keyboard logic ────────────────────────────────────────────────────────────


def _on_both_pressed():
    global recording, audio_chunks
    if recording:
        return
    if whisper is None:
        print("Whisper is still loading. Try again in a moment.")
        return
    with _rec_lock:
        recording = True
        audio_chunks = []
    threading.Thread(target=_pause_media_if_playing, daemon=True).start()
    status_queue.put("recording")
    print("Recording…")


def _on_both_released():
    global recording
    if not recording:
        return
    lang = _combo_lang
    with _rec_lock:
        recording = False
        chunks = list(audio_chunks)
    print("Stopped.")
    if not chunks:
        status_queue.put("idle")
        return
    audio = np.concatenate(chunks, axis=0)
    min_samples = int(SAMPLE_RATE * 0.3)
    if len(audio) < min_samples:
        print("Recording too short, ignoring.")
        status_queue.put("idle")
        return
    threading.Thread(target=transcribe_and_paste, args=(audio, lang), daemon=True).start()


_win_down = False
_ctrl_down = False
_shift_down = False
_combo_active = False
_combo_lang = "en"  # which language combo is active
_last_key_time = 0.0
_KEY_TIMEOUT = 60.0  # seconds — reset stuck keys after this


def _on_key_event(event):
    global _win_down, _ctrl_down, _shift_down, _combo_active, _combo_lang, _last_key_time
    if _pasting:
        return
    now = time.time()
    if now - _last_key_time > _KEY_TIMEOUT:
        _win_down = False
        _ctrl_down = False
        _shift_down = False
    _last_key_time = now
    is_down = event.event_type == keyboard.KEY_DOWN
    name = (event.name or "").lower()
    if "windows" in name:
        _win_down = is_down
    elif "ctrl" in name:
        _ctrl_down = is_down
    elif "shift" in name:
        _shift_down = is_down

    combo_en = _win_down and _ctrl_down
    combo_ro = _win_down and _shift_down and not _ctrl_down

    if (combo_en or combo_ro) and not _combo_active:
        _combo_active = True
        _combo_lang = "ro" if combo_ro else "en"
        _on_both_pressed()
    elif not (combo_en or combo_ro) and _combo_active:
        _combo_active = False
        _on_both_released()


# ── Overlay ───────────────────────────────────────────────────────────────────


class Overlay:
    # Pre-parse color constants as RGBA tuples
    _KEY_RGBA = tuple(int(KEY_COLOR[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    _REC_RGBA = tuple(int(COLOR_REC[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    _PROC_RGBA = tuple(int(COLOR_PROC[i : i + 2], 16) for i in (1, 3, 5)) + (255,)

    def __init__(self):
        self._rng = np.random.default_rng()
        self._dot_h = [float(DOT_R)] * NUM_DOTS
        _pos = np.linspace(-np.pi / 2, np.pi / 2, NUM_DOTS)
        self._dot_env = 0.25 + 0.75 * np.cos(_pos) ** 2
        self._spinner_angle = 0.0
        self._mode = "idle"
        self._t_frac = 1.0
        self._frame_ref = None

        root = _tk.Tk()
        root.title("wispr-overlay")
        root.withdraw()
        self.root = root

        ov = _tk.Toplevel(root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.wm_attributes("-topmost", True)
        ov.wm_attributes("-toolwindow", True)
        ov.wm_attributes("-transparentcolor", KEY_COLOR)
        ov.configure(bg=KEY_COLOR)

        sw = ov.winfo_screenwidth()
        sh = ov.winfo_screenheight()
        x = (sw - OVERLAY_W) // 2
        self._x = x
        self._target_y = sh - OVERLAY_H - TASKBAR_CLEAR
        self._sh = sh
        self._y = float(sh)
        self._anim_token = 0
        self._visible = False
        ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{x}+{sh}")

        c = _tk.Canvas(
            ov, width=OVERLAY_W, height=OVERLAY_H, bg=KEY_COLOR, highlightthickness=0
        )
        c.pack()

        self._frame_item = c.create_image(0, 0, anchor="nw")

        self._bg_layer = _PIL_Image.new("RGBA", (OVERLAY_W, OVERLAY_H), self._KEY_RGBA)
        self._pill_layer = _PIL_Image.new("RGBA", (OVERLAY_W, OVERLAY_H), (0, 0, 0, 0))
        _PIL_ImageDraw.Draw(self._pill_layer).rounded_rectangle(
            [0, 0, OVERLAY_W - 1, OVERLAY_H - 1],
            radius=OVERLAY_H // 2,
            fill=PILL_FILL,
        )

        total_dot_w = NUM_DOTS * DOT_R * 2 + (NUM_DOTS - 1) * DOT_GAP
        dot_x0 = (OVERLAY_W - total_dot_w) // 2
        mid_y = OVERLAY_H // 2
        self._dot_xs = [
            dot_x0 + i * (DOT_R * 2 + DOT_GAP) + DOT_R for i in range(NUM_DOTS)
        ]
        self._dot_mid = mid_y
        self._spin_cx = OVERLAY_W // 2
        self._spin_cy = mid_y

        self._canvas = c
        self._ov = ov

        self._tick()
        self._poll()

    @staticmethod
    def _spinner_rgba(frac: float) -> tuple:
        return (
            int(0xFB * (1 - frac) + 0x20 * frac),
            int(0x92 * (1 - frac) + 0x10 * frac),
            int(0x3C * (1 - frac) + 0x08 * frac),
            255,
        )

    def _render_frame(self):
        S = RENDER_SCALE
        W3, H3 = OVERLAY_W * S, OVERLAY_H * S

        content = _PIL_Image.new("RGBA", (W3, H3), (0, 0, 0, 0))
        draw = _PIL_ImageDraw.Draw(content)

        if self._mode == "recording":
            for i, cx in enumerate(self._dot_xs):
                rx = DOT_R * S
                ry = int(self._dot_h[i] * S)
                sx = cx * S
                sy = self._dot_mid * S
                draw.ellipse([sx - rx, sy - ry, sx + rx, sy + ry], fill=self._REC_RGBA)

        elif self._mode == "processing":
            spin_a = int(255 * self._t_frac)
            for j in range(SPINNER_DOTS):
                a = self._spinner_angle + j * (2 * np.pi / SPINNER_DOTS)
                px = self._spin_cx * S + np.cos(a) * SPINNER_R * S
                py = self._spin_cy * S + np.sin(a) * SPINNER_R * S
                r = max(S, int((SPINNER_DOT_R - j * 0.2) * S))
                base = self._spinner_rgba(j / SPINNER_DOTS)
                draw.ellipse(
                    [px - r, py - r, px + r, py + r], fill=base[:3] + (spin_a,)
                )
            if self._t_frac < 1.0:
                dot_a = int(255 * (1.0 - self._t_frac))
                for i, cx in enumerate(self._dot_xs):
                    rx = DOT_R * S
                    ry = int(self._dot_h[i] * S)
                    sx = cx * S
                    sy = self._dot_mid * S
                    draw.ellipse(
                        [sx - rx, sy - ry, sx + rx, sy + ry],
                        fill=self._REC_RGBA[:3] + (dot_a,),
                    )

        content = content.resize((OVERLAY_W, OVERLAY_H), _PIL_Image.LANCZOS)
        frame = _PIL_Image.alpha_composite(self._bg_layer, self._pill_layer)
        frame = _PIL_Image.alpha_composite(frame, content)
        return _PIL_ImageTk.PhotoImage(frame.convert("RGB"))

    def _tick(self):
        if self._mode == "recording":
            level = min(audio_level * 14.0, 1.0)
            for i in range(NUM_DOTS):
                target = DOT_R + (DOT_MAX_H - DOT_R) * self._dot_env[i] * level * float(
                    self._rng.uniform(0.4, 1.0)
                )
                self._dot_h[i] += (target - self._dot_h[i]) * 0.55

        elif self._mode == "processing":
            self._spinner_angle -= 0.30
            if self._t_frac < 1.0:
                self._t_frac = min(1.0, self._t_frac + 0.14)
                for i in range(NUM_DOTS):
                    self._dot_h[i] += (DOT_R - self._dot_h[i]) * 0.45

        else:
            for i in range(NUM_DOTS):
                self._dot_h[i] += (DOT_R - self._dot_h[i]) * 0.3

        if self._mode != "idle":
            photo = self._render_frame()
            self._frame_ref = photo
            self._canvas.itemconfig(self._frame_item, image=photo)

        self.root.after(50, self._tick)

    def _poll(self):
        try:
            while True:
                self._apply(status_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _animate_to(self, target_y: int, token: int, on_done=None):
        if self._anim_token != token:
            return
        dist = target_y - self._y
        if abs(dist) < 0.8:
            self._y = float(target_y)
            self._ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{self._x}+{target_y}")
            if on_done:
                on_done()
            return
        self._y += dist * 0.28
        self._ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{self._x}+{int(self._y)}")
        self.root.after(16, lambda: self._animate_to(target_y, token, on_done))

    def _apply(self, state: str):
        self._mode = state
        self._anim_token += 1
        token = self._anim_token

        if state in ("recording", "processing"):
            if state == "processing":
                self._t_frac = 0.0
            if not self._visible:
                self._visible = True
                self._y = float(self._sh)
                self._ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{self._x}+{self._sh}")
                self._ov.deiconify()
                self._ov.lift()
            self._animate_to(self._target_y, token)
        else:
            self._animate_to(self._sh, token, on_done=self._hide)

        _update_tray(state)

    def _hide(self):
        self._visible = False
        self._ov.withdraw()

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────


def _quit(icon, item):
    icon.stop()
    if _audio_stream is not None:
        _audio_stream.stop()
        _audio_stream.close()
    if keyboard is not None and _keyboard_hook_ref is not None:
        keyboard.unhook(_keyboard_hook_ref)
    _release_single_instance()
    os._exit(0)


_PROVIDERS = ["gemini", "openrouter", "ollama", "lmstudio"]


def _toggle_llm(icon, item):
    global CLEANUP_MODE
    CLEANUP_MODE = "llm" if CLEANUP_MODE == "punct" else "punct"
    icon.update_menu()


def _set_provider(name: str):
    def _handler(icon, item):
        global LLM_PROVIDER
        ok = _setup_provider(name, fatal=False)
        LLM_PROVIDER = name if ok else LLM_PROVIDER
        icon.update_menu()

    return _handler


def _build_menu():
    provider_items = [
        _pystray_MenuItem(
            p,
            _set_provider(p),
            checked=lambda item, p=p: LLM_PROVIDER == p,
            radio=True,
        )
        for p in _PROVIDERS
    ]
    return _pystray_Menu(
        _pystray_MenuItem(
            lambda item: f"LLM Correction: {'ON' if CLEANUP_MODE == 'llm' else 'OFF'}",
            _toggle_llm,
            checked=lambda item: CLEANUP_MODE == "llm",
        ),
        _pystray_MenuItem("Provider", _pystray_Menu(*provider_items)),
        _pystray_Menu.SEPARATOR,
        _pystray_MenuItem(
            lambda item: f"Romanian model: {'loaded' if whisper_ro else 'not loaded'}",
            _unload_whisper_ro,
            enabled=lambda item: whisper_ro is not None,
        ),
        _pystray_Menu.SEPARATOR,
        _pystray_MenuItem("Quit Wispr", _quit),
    )


def _load_whisper():
    global whisper, whisper_error
    from faster_whisper import WhisperModel

    attempts = []
    gpu = _cuda_gpu()
    if gpu:
        print(f"[whisper] GPU detected: {gpu[0:60]}")
        attempts.append((WHISPER_DEVICE, WHISPER_COMPUTE))
    attempts.append(("cpu", "int8"))

    last_error = None
    for device, compute_type in attempts:
        try:
            whisper = WhisperModel(
                WHISPER_MODEL, device=device, compute_type=compute_type
            )
            print(f"Whisper loaded on {device}")
            whisper_error = None
            whisper_ready.set()
            return
        except Exception as exc:
            last_error = exc

    whisper_error = last_error
    whisper_ready.set()  # unblock any waiters — they'll check whisper is None
    print(f"ERROR: Whisper failed to load: {last_error}")
    print("Voice transcription is unavailable. Restart Wispr to retry.")


def main():
    global tray_icon, np, sd, pyautogui, pyperclip, keyboard, _audio_stream
    global _PIL_Image, _PIL_ImageDraw, _PIL_ImageTk
    global _pystray_Icon, _pystray_MenuItem, _pystray_Menu
    global LLM_PROVIDER, LLM_FALLBACK, CLEANUP_MODE, OPENROUTER_MODEL
    global ICON_IDLE, ICON_RECORDING, ICON_PROCESSING
    global _keyboard_hook_ref, _tk

    _t_main_start = time.perf_counter()
    print(f"[t+{(_t_main_start - _t_main_start) * 1000:.0f}ms] main() started")

    # When started by Wispr.exe launcher, exit if it dies (stdin pipe -> EOF).
    if os.getenv("WISPR_LAUNCHER") == "1":
        def _watch_launcher():
            try:
                while os.read(0, 1):
                    pass
            except OSError:
                pass
            os._exit(0)
        threading.Thread(target=_watch_launcher, daemon=True).start()

    # ── Start LM Studio server if lms CLI is available ───────────────────────
    import shutil
    if shutil.which("lms"):
        subprocess.Popen(
            ["lms", "server", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ── Deferred heavy imports ────────────────────────────────────────────────
    # numpy must be imported single-threaded (circular import risk in threads).
    # All other packages are independent and safe to import in parallel.
    np = _np

    import concurrent.futures as _cf

    def _import_sounddevice():
        import sounddevice as _sd

        return _sd

    def _import_pyautogui():
        import pyautogui as _pag

        return _pag

    def _import_pyperclip():
        import pyperclip as _pc

        return _pc

    def _import_keyboard():
        import keyboard as _kb

        return _kb

    def _import_pil():
        from PIL import Image as _Im, ImageDraw as _ID, ImageTk as _ITk

        return _Im, _ID, _ITk

    def _import_pystray():
        from pystray import Icon as _Ic, MenuItem as _MI, Menu as _Me

        return _Ic, _MI, _Me

    def _import_dotenv():
        from dotenv import load_dotenv as _ld

        return _ld

    def _import_tk():
        import tkinter as _tk

        return _tk

    with _cf.ThreadPoolExecutor(max_workers=9) as _pool:
        _f_sd = _pool.submit(_import_sounddevice)
        _f_pag = _pool.submit(_import_pyautogui)
        _f_pc = _pool.submit(_import_pyperclip)
        _f_kb = _pool.submit(_import_keyboard)
        _f_pil = _pool.submit(_import_pil)
        _f_pst = _pool.submit(_import_pystray)
        _f_dn = _pool.submit(_import_dotenv)
        _f_tk = _pool.submit(_import_tk)

        sd = _f_sd.result()
        pyautogui = _f_pag.result()
        pyperclip = _f_pc.result()
        keyboard = _f_kb.result()
        _PIL_Image, _PIL_ImageDraw, _PIL_ImageTk = _f_pil.result()
        _pystray_Icon, _pystray_MenuItem, _pystray_Menu = _f_pst.result()
        load_dotenv = _f_dn.result()
        _tk = _f_tk.result()

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0

    load_dotenv()

    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
    LLM_FALLBACK = os.getenv("LLM_FALLBACK", "").lower()
    CLEANUP_MODE = os.getenv("CLEANUP_MODE", "punct").lower()
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", OPENROUTER_DEFAULT)

    # Build tray icons
    ICON_IDLE = _make_mic_icon("#22c55e")
    ICON_RECORDING = _make_mic_icon("#ef4444")
    ICON_PROCESSING = _make_mic_icon("#f59e0b")

    # LLM provider setup (skip in bench mode)
    if not _BENCH_MODE:
        if LLM_PROVIDER != "punct":
            _setup_provider(LLM_PROVIDER, fatal=True)
        if LLM_FALLBACK and LLM_FALLBACK != LLM_PROVIDER:
            _setup_provider(LLM_FALLBACK, fatal=False)

    # Start whisper loading in background
    threading.Thread(target=_load_whisper, daemon=True).start()

    # Start audio stream in background
    _stream_ready = threading.Event()

    def _init_stream():
        s = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=audio_callback,
            blocksize=1024,
        )
        s.start()
        global _audio_stream
        _audio_stream = s
        _stream_ready.set()

    threading.Thread(target=_init_stream, daemon=True).start()

    # Setup keyboard hook
    _keyboard_hook_ref = keyboard.hook(_on_key_event)
    print("Wispr ready! Win+Ctrl = English, Win+Shift = Romanian")

    # Start tray icon
    tray_icon = _pystray_Icon(
        "Wispr",
        ICON_IDLE,
        "Wispr – Idle (hold the Wispr hotkey to record)",
        _build_menu(),
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()

    # Wait for stream to be ready
    _stream_ready.wait(timeout=10)

    if _BENCH_MODE:
        _t_interactive = time.perf_counter()
        _elapsed_ms = (_t_interactive - _t_main_start) * 1000
        _bench_file = os.path.join(tempfile.gettempdir(), "wispr_bench.txt")
        with open(_bench_file, "w") as _f:
            _f.write(f"{_elapsed_ms:.1f}")
        print(f"[BENCH] interactive in {_elapsed_ms:.1f}ms")
        tray_icon.stop()
        _audio_stream.stop()
        _audio_stream.close()
        keyboard.unhook_all()
        _release_single_instance()
        os._exit(0)

    Overlay().run()


if __name__ == "__main__":
    main()
