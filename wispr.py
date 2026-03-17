#!/usr/bin/env python3
"""
Wispr - Windows voice dictation tool
Hold Win + Space to record, release to transcribe and paste.
"""

import os
import sys
import re
import json
import time
import threading
import queue
import numpy as np
import sounddevice as sd
import pyautogui
import pyperclip
import keyboard
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk
from pystray import Icon, MenuItem, Menu
from dotenv import load_dotenv
from faster_whisper import WhisperModel
import requests
try:
    import google.generativeai as genai
    _genai_available = True
except ImportError:
    _genai_available = False

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE     = 16_000
CHANNELS        = 1
WHISPER_MODEL   = "small.en"
WHISPER_DEVICE  = "cuda"
WHISPER_COMPUTE = "float16"
BEAM_SIZE       = 1

GEMINI_MODEL       = "gemini-2.0-flash"
OPENROUTER_DEFAULT = "nvidia/nemotron-3-super-120b-a12b:free"
LMSTUDIO_DEFAULT   = "qwen2.5-1.5b-instruct"   # fast, ~1 GB — ideal for grammar-only cleanup
OLLAMA_DEFAULT     = "qwen2.5:1.5b"             # pull with: ollama pull qwen2.5:1.5b

CLEANUP_SYSTEM = (
    "You are a voice transcription cleanup assistant. "
    "The user will give you raw speech-to-text output. "
    "Your job: remove filler words (um, uh, like, you know, etc.), "
    "fix grammar, fix punctuation, and keep the original tone and intent. "
    "Return ONLY the cleaned text. No explanations, no quotes, no preamble."
)

# ── Overlay geometry ──────────────────────────────────────────────────────────

OVERLAY_W     = 120           # total pill width
OVERLAY_H     = 34            # pill height  (radius = H/2 for true pill)
TASKBAR_CLEAR = 24            # px above screen bottom

# pill background
KEY_COLOR     = "#010002"     # unique near-black used as the transparent key
PILL_FILL     = (0, 0, 0, 230)   # RGBA – dark navy, ~90 % opaque

# listening dots
NUM_DOTS      = 16            # more dots = finer visualiser
DOT_R         = 2             # horizontal radius (fixed)
DOT_MAX_H     = 14            # max vertical radius when loud
DOT_GAP       = 1.5             # gap between dot edges

# spinner (processing)
SPINNER_DOTS  = 8
SPINNER_R     = 9             # radius of spinner circle
SPINNER_DOT_R = 2             # radius of each spinner dot

# rendering
RENDER_SCALE  = 4            # supersample factor — drawn at 4How are you doing?× then LANCZOS ↓

COLOR_REC     = "#818cf8"     # indigo-400  – recording
COLOR_PROC    = "#fb923c"    # orange      – processing

pyautogui.FAILSAFE = False
pyautogui.PAUSE    = 0

# ── Init ──────────────────────────────────────────────────────────────────────

load_dotenv()

LLM_PROVIDER     = os.getenv("LLM_PROVIDER", "gemini").lower()
LLM_FALLBACK     = os.getenv("LLM_FALLBACK", "").lower()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", OPENROUTER_DEFAULT)

gemini_client      = None
openrouter_api_key = ""
lmstudio_url       = ""
lmstudio_model     = ""
ollama_url         = ""
ollama_model       = ""

def _setup_provider(name: str, fatal: bool = True) -> bool:
    global gemini_client, openrouter_api_key, lmstudio_url, lmstudio_model, ollama_url, ollama_model
    label = "primary" if fatal else "fallback"

    def _fail(msg):
        if fatal:
            sys.exit(f"ERROR: {msg}")
        print(f"WARNING: fallback {name} skipped — {msg}")
        return False

    if name == "gemini":
        if not _genai_available:
            return _fail("google-generativeai not installed.")
        _key = os.getenv("GEMINI_API_KEY", "")
        if not _key or _key == "your_gemini_api_key_here":
            return _fail("GEMINI_API_KEY not set in .env.")
        genai.configure(api_key=_key)
        gemini_client = genai.GenerativeModel(model_name=GEMINI_MODEL, system_instruction=CLEANUP_SYSTEM)
        print(f"LLM {label}: Gemini ({GEMINI_MODEL})")
        return True

    if name == "openrouter":
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            return _fail("OPENROUTER_API_KEY not set in .env.")
        print(f"LLM {label}: OpenRouter ({OPENROUTER_MODEL})")
        return True

    if name == "lmstudio":
        lmstudio_url   = os.getenv("LMSTUDIO_URL",   "http://localhost:1234/v1")
        lmstudio_model = os.getenv("LMSTUDIO_MODEL", LMSTUDIO_DEFAULT)
        print(f"LLM {label}: LM Studio ({lmstudio_model} @ {lmstudio_url})")
        return True

    if name == "ollama":
        ollama_url   = os.getenv("OLLAMA_URL",   "http://localhost:11434/v1")
        ollama_model = os.getenv("OLLAMA_MODEL", OLLAMA_DEFAULT)
        print(f"LLM {label}: Ollama ({ollama_model} @ {ollama_url})")
        return True

    return _fail(f"unknown provider '{name}'. Use gemini, openrouter, lmstudio, or ollama.")

_setup_provider(LLM_PROVIDER, fatal=True)
if LLM_FALLBACK and LLM_FALLBACK != LLM_PROVIDER:
    _setup_provider(LLM_FALLBACK, fatal=False)

whisper = None   # loaded in background thread below

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
        text = re.sub(r"\b" + re.escape(key) + r"\b", wmap[key], text, flags=re.IGNORECASE)
    return text

WORD_MAP = _load_word_map()

# ── Global state ──────────────────────────────────────────────────────────────

recording          = False
audio_chunks: list = []
audio_level        = 0.0
tray_icon: Icon | None = None
_media_was_playing = False

status_queue: queue.Queue[str] = queue.Queue()   # "idle" | "recording" | "processing"

# ── Tray helpers ──────────────────────────────────────────────────────────────

def _make_dot(color: str) -> Image.Image:
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=color, outline="white", width=2)
    return img

ICON_IDLE       = _make_dot("#22c55e")
ICON_RECORDING  = _make_dot("#ef4444")
ICON_PROCESSING = _make_dot("#f59e0b")

def _update_tray(state: str):
    if tray_icon is None:
        return
    if state == "recording":
        tray_icon.icon  = ICON_RECORDING
        tray_icon.title = "Wispr – Recording…"
    elif state == "processing":
        tray_icon.icon  = ICON_PROCESSING
        tray_icon.title = "Wispr – Transcribing…"
    else:
        tray_icon.icon  = ICON_IDLE
        tray_icon.title = "Wispr – Idle (hold Win + Space to record)"

# ── Audio callback ────────────────────────────────────────────────────────────

def audio_callback(indata: np.ndarray, frames: int, time_info, status):
    global audio_level
    if recording:
        audio_chunks.append(indata.copy())
    audio_level = float(np.sqrt(np.mean(indata ** 2)))

# ── LLM cleanup ───────────────────────────────────────────────────────────────

_http = requests.Session()
_http.mount("http://",  requests.adapters.HTTPAdapter(max_retries=0))
_http.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

def _openai_compat_cleanup(base_url: str, model: str, raw: str, api_key: str = "") -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = _http.post(
        f"{base_url}/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": CLEANUP_SYSTEM},
                {"role": "user",   "content": raw},
            ],
        },
        timeout=(1, 30),   # (connect, read) — fail instantly if server is down
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

def _call_provider(name: str, raw: str) -> str:
    if name == "gemini":
        return gemini_client.generate_content(raw).text.strip()
    if name == "openrouter":
        return _openai_compat_cleanup("https://openrouter.ai/api/v1", OPENROUTER_MODEL, raw, openrouter_api_key)
    if name == "lmstudio":
        return _openai_compat_cleanup(lmstudio_url, lmstudio_model, raw)
    if name == "ollama":
        return _openai_compat_cleanup(ollama_url, ollama_model, raw)
    raise ValueError(f"Unknown provider: {name}")

def cleanup_text(raw: str) -> str:
    try:
        return _call_provider(LLM_PROVIDER, raw)
    except (requests.ConnectionError, requests.Timeout):
        if LLM_FALLBACK:
            print(f"  [{LLM_PROVIDER} unreachable → {LLM_FALLBACK}]")
            return _call_provider(LLM_FALLBACK, raw)
        raise

# ── Transcription + cleanup ────────────────────────────────────────────────────

def transcribe_and_paste(audio: np.ndarray):
    t_start = time.perf_counter()
    status_queue.put("processing")

    audio_f32 = audio.flatten().astype(np.float32)
    segments, _ = whisper.transcribe(audio_f32, beam_size=BEAM_SIZE, vad_filter=True)
    raw = " ".join(seg.text.strip() for seg in segments).strip()
    t_whisper = time.perf_counter()

    if not raw:
        print("Nothing detected.")
        status_queue.put("idle")
        return

    print(f"Raw:     {raw}")
    print(f"  [whisper]  {(t_whisper - t_start)*1000:.0f}ms")

    try:
        cleaned = cleanup_text(raw)
    except Exception as exc:
        print(f"LLM error ({LLM_PROVIDER}): {exc}  —  using raw transcript.")
        cleaned = raw
    t_llm = time.perf_counter()
    print(f"  [llm]      {(t_llm - t_whisper)*1000:.0f}ms")

    if WORD_MAP:
        cleaned = _apply_word_map(cleaned, WORD_MAP)

    print(f"Cleaned: {cleaned}")
    print(f"  [total]    {(time.perf_counter() - t_start)*1000:.0f}ms")

    pyperclip.copy(cleaned)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    if _media_was_playing:
        pyautogui.press("playpause")    # resume music (only if we paused it)

    status_queue.put("idle")

# ── Media state helpers ───────────────────────────────────────────────────────

def _is_media_playing() -> bool:
    """Query Windows GSMTC to check if any media is currently playing."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$mgr=[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,"
                "Windows.Media.Control,ContentType=WindowsRuntime]::RequestAsync().GetAwaiter().GetResult();"
                "$s=$mgr.GetCurrentSession();"
                "if($s){$s.GetPlaybackInfo().PlaybackStatus.ToString()}else{'None'}",
            ],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stdout.strip() == "Playing"
    except Exception:
        return False

def _pause_media_if_playing():
    global _media_was_playing
    _media_was_playing = _is_media_playing()
    if _media_was_playing:
        pyautogui.press("playpause")

# ── Keyboard logic ────────────────────────────────────────────────────────────

def _on_both_pressed():
    global recording, audio_chunks
    if recording or whisper is None:
        return
    recording    = True
    audio_chunks = []
    threading.Thread(target=_pause_media_if_playing, daemon=True).start()
    status_queue.put("recording")
    print("Recording…")

def _on_both_released():
    global recording
    if not recording:
        return
    recording = False
    print("Stopped.")
    if not audio_chunks:
        status_queue.put("idle")
        return
    audio = np.concatenate(audio_chunks, axis=0)
    threading.Thread(target=transcribe_and_paste, args=(audio,), daemon=True).start()

_win_down     = False
_space_down   = False
_combo_active = False

def _on_key_event(event: keyboard.KeyboardEvent):
    global _win_down, _space_down, _combo_active
    is_down = event.event_type == keyboard.KEY_DOWN
    name    = (event.name or "").lower()
    if "windows" in name:
        _win_down = is_down
    elif name == "space":
        _space_down = is_down
    both = _win_down and _space_down
    if both and not _combo_active:
        _combo_active = True
        _on_both_pressed()
    elif not both and _combo_active:
        _combo_active = False
        _on_both_released()

# ── Overlay ───────────────────────────────────────────────────────────────────

class Overlay:
    # Pre-parse color constants as RGBA tuples
    _KEY_RGBA  = tuple(int(KEY_COLOR[i:i+2], 16) for i in (1, 3, 5)) + (255,)
    _REC_RGBA  = tuple(int(COLOR_REC[i:i+2], 16) for i in (1, 3, 5)) + (255,)
    _PROC_RGBA = tuple(int(COLOR_PROC[i:i+2], 16) for i in (1, 3, 5)) + (255,)

    def __init__(self):
        self._rng           = np.random.default_rng()
        self._dot_h         = [float(DOT_R)] * NUM_DOTS
        # Cosine envelope: 1.0 at center, 0.25 at edges → pill-within-pill silhouette
        _pos = np.linspace(-np.pi / 2, np.pi / 2, NUM_DOTS)
        self._dot_env = 0.25 + 0.75 * np.cos(_pos) ** 2
        self._spinner_angle = 0.0
        self._mode          = "idle"
        self._t_frac        = 1.0    # cross-fade progress (0=start of processing, 1=done)
        self._frame_ref     = None   # prevent PhotoImage GC

        root = tk.Tk()
        root.title("wispr-overlay")
        root.withdraw()
        self.root = root

        ov = tk.Toplevel(root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.wm_attributes("-topmost",          True)
        ov.wm_attributes("-toolwindow",       True)
        ov.wm_attributes("-transparentcolor", KEY_COLOR)
        ov.configure(bg=KEY_COLOR)

        sw = ov.winfo_screenwidth()
        sh = ov.winfo_screenheight()
        x  = (sw - OVERLAY_W) // 2
        self._x        = x
        self._target_y = sh - OVERLAY_H - TASKBAR_CLEAR
        self._sh       = sh               # off-screen bottom anchor
        self._y        = float(sh)        # current animated y (starts off-screen)
        self._anim_token = 0
        self._visible    = False
        ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{x}+{sh}")

        c = tk.Canvas(ov, width=OVERLAY_W, height=OVERLAY_H,
                      bg=KEY_COLOR, highlightthickness=0)
        c.pack()

        # Single image item — the full frame is re-rendered each tick via PIL
        self._frame_item = c.create_image(0, 0, anchor="nw")

        # ── Pre-computed layers (drawn once) ──────────────────────────────────
        # Solid key-color background layer
        self._bg_layer = Image.new("RGBA", (OVERLAY_W, OVERLAY_H), self._KEY_RGBA)

        # Pill shape layer (transparent outside, PILL_FILL inside)
        self._pill_layer = Image.new("RGBA", (OVERLAY_W, OVERLAY_H), (0, 0, 0, 0))
        ImageDraw.Draw(self._pill_layer).rounded_rectangle(
            [0, 0, OVERLAY_W - 1, OVERLAY_H - 1],
            radius=OVERLAY_H // 2,
            fill=PILL_FILL,
        )

        # ── Logical dot positions (x centres) ────────────────────────────────
        total_dot_w    = NUM_DOTS * DOT_R * 2 + (NUM_DOTS - 1) * DOT_GAP
        dot_x0         = (OVERLAY_W - total_dot_w) // 2
        mid_y          = OVERLAY_H // 2
        self._dot_xs   = [dot_x0 + i * (DOT_R * 2 + DOT_GAP) + DOT_R for i in range(NUM_DOTS)]
        self._dot_mid  = mid_y
        self._spin_cx  = OVERLAY_W // 2
        self._spin_cy  = mid_y

        self._canvas = c
        self._ov     = ov

        self._tick()
        self._poll()

    # ── PIL frame renderer ────────────────────────────────────────────────────

    @staticmethod
    def _spinner_rgba(frac: float) -> tuple:
        """Fade bright orange → near-black across the spinner tail."""
        return (
            int(0xfb * (1 - frac) + 0x20 * frac),
            int(0x92 * (1 - frac) + 0x10 * frac),
            int(0x3c * (1 - frac) + 0x08 * frac),
            255,
        )

    def _render_frame(self) -> ImageTk.PhotoImage:
        S = RENDER_SCALE
        W3, H3 = OVERLAY_W * S, OVERLAY_H * S

        # Draw content at 3× resolution on a transparent canvas
        content = Image.new("RGBA", (W3, H3), (0, 0, 0, 0))
        draw    = ImageDraw.Draw(content)

        if self._mode == "recording":
            for i, cx in enumerate(self._dot_xs):
                rx = DOT_R * S
                ry = int(self._dot_h[i] * S)
                sx = cx * S
                sy = self._dot_mid * S
                draw.ellipse([sx - rx, sy - ry, sx + rx, sy + ry],
                             fill=self._REC_RGBA)

        elif self._mode == "processing":
            spin_a = int(255 * self._t_frac)
            # Spinner fades in
            for j in range(SPINNER_DOTS):
                a  = self._spinner_angle + j * (2 * np.pi / SPINNER_DOTS)
                px = self._spin_cx * S + np.cos(a) * SPINNER_R * S
                py = self._spin_cy * S + np.sin(a) * SPINNER_R * S
                r  = max(S, int((SPINNER_DOT_R - j * 0.2) * S))
                base = self._spinner_rgba(j / SPINNER_DOTS)
                draw.ellipse([px - r, py - r, px + r, py + r],
                             fill=base[:3] + (spin_a,))
            # Dots fade out during transition
            if self._t_frac < 1.0:
                dot_a = int(255 * (1.0 - self._t_frac))
                for i, cx in enumerate(self._dot_xs):
                    rx = DOT_R * S
                    ry = int(self._dot_h[i] * S)
                    sx = cx * S
                    sy = self._dot_mid * S
                    draw.ellipse([sx - rx, sy - ry, sx + rx, sy + ry],
                                 fill=self._REC_RGBA[:3] + (dot_a,))

        # Downscale 3× → 1× with LANCZOS (anti-aliased smooth circles)
        content = content.resize((OVERLAY_W, OVERLAY_H), Image.LANCZOS)

        # Composite: key-color bg → pill shape → animated content
        frame = Image.alpha_composite(self._bg_layer, self._pill_layer)
        frame = Image.alpha_composite(frame, content)
        return ImageTk.PhotoImage(frame.convert("RGB"))

    # ── animation tick ────────────────────────────────────────────────────────

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
                self._t_frac = min(1.0, self._t_frac + 0.14)   # ~350 ms
                for i in range(NUM_DOTS):
                    self._dot_h[i] += (DOT_R - self._dot_h[i]) * 0.45

        else:
            for i in range(NUM_DOTS):
                self._dot_h[i] += (DOT_R - self._dot_h[i]) * 0.3

        if self._mode != "idle":
            photo = self._render_frame()
            self._frame_ref = photo          # hold reference — prevents GC
            self._canvas.itemconfig(self._frame_item, image=photo)

        self.root.after(50, self._tick)

    # ── status polling ────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                self._apply(status_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ── slide animation ───────────────────────────────────────────────────────

    def _animate_to(self, target_y: int, token: int, on_done=None):
        if self._anim_token != token:
            return                            # superseded by a newer animation
        dist = target_y - self._y
        if abs(dist) < 0.8:
            self._y = float(target_y)
            self._ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{self._x}+{target_y}")
            if on_done:
                on_done()
            return
        self._y += dist * 0.28              # exponential ease-out
        self._ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{self._x}+{int(self._y)}")
        self.root.after(16, lambda: self._animate_to(target_y, token, on_done))

    def _apply(self, state: str):
        self._mode = state
        self._anim_token += 1
        token = self._anim_token

        if state in ("recording", "processing"):
            if state == "processing":
                self._t_frac = 0.0       # kick off cross-fade
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

def _quit(icon: Icon, item):
    icon.stop()
    os._exit(0)

def _load_whisper():
    global whisper
    print(f"Loading Whisper {WHISPER_MODEL} on {WHISPER_DEVICE}/{WHISPER_COMPUTE} …")
    whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    print("Whisper ready.  Hold Win + Space to record.")

def main():
    global tray_icon

    threading.Thread(target=_load_whisper, daemon=True).start()

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        callback=audio_callback,
        blocksize=1024,
    )
    stream.start()

    keyboard.hook(_on_key_event)

    menu      = Menu(MenuItem("Quit Wispr", _quit))
    tray_icon = Icon("Wispr", ICON_IDLE, "Wispr – Idle (hold Win + Space to record)", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()

    Overlay().run()

if __name__ == "__main__":
    main()
