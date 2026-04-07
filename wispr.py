#!/usr/bin/env python3
from __future__ import annotations

"""
Wispr - Windows voice dictation tool
Hold the Wispr hotkey to record, release to transcribe and paste.
"""

import os
import sys
import tempfile


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
import asyncio

# Heavy third-party imports are deferred to main() below.
# Module-level names used in type hints / global state use forward references.

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 16_000
CHANNELS = 1
WHISPER_MODEL = "small.en"
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
_paused_media_sessions: list = []

status_queue: queue.Queue = queue.Queue()  # "idle" | "recording" | "processing"

# Deferred import handles (set in main())
np = None          # numpy
sd = None          # sounddevice
pyautogui = None
pyperclip = None
keyboard = None
_PIL_Image = None
_PIL_ImageDraw = None
_PIL_ImageTk = None
_pystray_Icon = None
_pystray_MenuItem = None
_pystray_Menu = None
_http = None       # requests.Session

MediaManager = None
_media_controls_available = False

# ── Env / provider state ──────────────────────────────────────────────────────

LLM_PROVIDER = "gemini"     # overwritten after load_dotenv() in main()
LLM_FALLBACK = ""
CLEANUP_MODE = "punct"
OPENROUTER_MODEL = OPENROUTER_DEFAULT

gemini_client = None
openrouter_api_key = ""
lmstudio_url = ""
lmstudio_model = ""
ollama_url = ""
ollama_model = ""


def _release_single_instance():
    lock_file = os.path.join(tempfile.gettempdir(), "wispr.lock")
    try:
        os.remove(lock_file)
    except OSError:
        pass


def _setup_provider(name: str, fatal: bool = True) -> bool:
    global \
        gemini_client, \
        openrouter_api_key, \
        lmstudio_url, \
        lmstudio_model, \
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
        print(f"LLM {label}: Gemini ({GEMINI_MODEL})")
        return True

    if name == "openrouter":
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not openrouter_api_key:
            return _fail("OPENROUTER_API_KEY not set in .env.")
        print(f"LLM {label}: OpenRouter ({OPENROUTER_MODEL})")
        return True

    if name == "lmstudio":
        lmstudio_url = os.getenv("LMSTUDIO_URL", "http://localhost:1234/v1")
        lmstudio_model = os.getenv("LMSTUDIO_MODEL", LMSTUDIO_DEFAULT)
        print(f"LLM {label}: LM Studio ({lmstudio_model} @ {lmstudio_url})")
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


def _make_mic_icon(color: str):
    img = _PIL_Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = _PIL_ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [24, 8, 40, 36], radius=8, fill=color, outline="white", width=2
    )
    for y in [16, 20, 24, 28]:
        draw.line([(28, y), (36, y)], fill="white", width=2)
    draw.arc([18, 32, 46, 52], 180, 0, fill="white", width=2)
    draw.line([(32, 44), (32, 50)], fill="white", width=2)
    draw.line([(24, 50), (40, 50)], fill="white", width=2)
    draw.arc([10, 18, 22, 36], 270, 90, fill="white", width=2)
    draw.arc([4, 12, 22, 42], 270, 90, fill="white", width=2)
    draw.arc([42, 18, 54, 36], 90, 270, fill="white", width=2)
    draw.arc([42, 12, 60, 42], 90, 270, fill="white", width=2)
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
    if recording:
        audio_chunks.append(indata.copy())
    audio_level = float(np.sqrt(np.mean(indata**2)))


# ── LLM cleanup ───────────────────────────────────────────────────────────────


def _openai_compat_cleanup(
    base_url: str, model: str, raw: str, api_key: str = ""
) -> str:
    import requests as _req
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
                {"role": "user", "content": raw},
            ],
        },
        timeout=(1, 30),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _call_provider(name: str, raw: str) -> str:
    if name == "gemini":
        return gemini_client.generate_content(raw).text.strip()
    if name == "openrouter":
        return _openai_compat_cleanup(
            "https://openrouter.ai/api/v1", OPENROUTER_MODEL, raw, openrouter_api_key
        )
    if name == "lmstudio":
        return _openai_compat_cleanup(lmstudio_url, lmstudio_model, raw)
    if name == "ollama":
        return _openai_compat_cleanup(ollama_url, ollama_model, raw)
    raise ValueError(f"Unknown provider: {name}")


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
    import requests as _req
    if CLEANUP_MODE == "punct":
        return _local_punct(raw)
    try:
        return _call_provider(LLM_PROVIDER, raw)
    except (_req.ConnectionError, _req.Timeout):
        if LLM_FALLBACK:
            print(f"  [{LLM_PROVIDER} unreachable → {LLM_FALLBACK}]")
            return _call_provider(LLM_FALLBACK, raw)
        raise


# ── Transcription + cleanup ────────────────────────────────────────────────────


def transcribe_and_paste(audio):
    t_start = time.perf_counter()
    status_queue.put("processing")

    if whisper is None:
        print("Whisper is not ready; skipping transcription.")
        status_queue.put("idle")
        return

    audio_f32 = audio.flatten().astype(np.float32)
    segments, _ = whisper.transcribe(audio_f32, beam_size=BEAM_SIZE, vad_filter=False)
    raw = " ".join(seg.text.strip() for seg in segments).strip()
    t_whisper = time.perf_counter()

    if not raw:
        print("Nothing detected.")
        status_queue.put("idle")
        return

    print(f"Raw:     {raw}")
    print(f"  [whisper]  {(t_whisper - t_start) * 1000:.0f}ms")

    try:
        cleaned = cleanup_text(raw)
    except Exception as exc:
        print(f"LLM error ({LLM_PROVIDER}): {exc}  —  using raw transcript.")
        cleaned = raw
    t_llm = time.perf_counter()
    print(f"  [llm]      {(t_llm - t_whisper) * 1000:.0f}ms")

    if WORD_MAP:
        cleaned = _apply_word_map(cleaned, WORD_MAP)

    print(f"Cleaned: {cleaned}")
    print(f"  [total]    {(time.perf_counter() - t_start) * 1000:.0f}ms")

    pyperclip.copy(cleaned)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    _resume_media()

    status_queue.put("idle")


# ── Media state helpers ───────────────────────────────────────────────────────


def _pause_media_if_playing():
    global _paused_media_sessions

    if not _media_controls_available:
        _paused_media_sessions = []
        return

    async def do_pause():
        paused = []
        try:
            mgr = await MediaManager.request_async()
            sessions = mgr.get_sessions()
            for s in sessions:
                info = s.get_playback_info()
                if info and info.playback_status == 4:  # 4 = Playing
                    await s.try_pause_async()
                    paused.append(s)
        except Exception as e:
            print(f"Media pause error: {e}")
        return paused

    try:
        _paused_media_sessions = asyncio.run(do_pause())
    except Exception as e:
        print(f"Asyncio run error: {e}")
        _paused_media_sessions = []


def _resume_media():
    global _paused_media_sessions
    if not _paused_media_sessions:
        return

    async def do_resume(sessions):
        for s in sessions:
            try:
                await s.try_play_async()
            except Exception as e:
                print(f"Media resume error: {e}")

    try:
        asyncio.run(do_resume(_paused_media_sessions))
    except Exception as e:
        print(f"Asyncio run resume error: {e}")

    _paused_media_sessions = []


# ── Keyboard logic ────────────────────────────────────────────────────────────


def _on_both_pressed():
    global recording, audio_chunks
    if recording:
        return
    if whisper is None:
        print("Whisper is still loading. Try again in a moment.")
        return
    recording = True
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


_win_down = False
_ctrl_down = False
_combo_active = False


def _on_key_event(event):
    global _win_down, _ctrl_down, _combo_active
    is_down = event.event_type == keyboard.KEY_DOWN
    name = (event.name or "").lower()
    if "windows" in name:
        _win_down = is_down
    elif "ctrl" in name:
        _ctrl_down = is_down
    both = _win_down and _ctrl_down
    if both and not _combo_active:
        _combo_active = True
        _on_both_pressed()
    elif not both and _combo_active:
        _combo_active = False
        _on_both_released()


# ── Overlay ───────────────────────────────────────────────────────────────────


class Overlay:
    # Pre-parse color constants as RGBA tuples
    _KEY_RGBA = tuple(int(KEY_COLOR[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    _REC_RGBA = tuple(int(COLOR_REC[i : i + 2], 16) for i in (1, 3, 5)) + (255,)
    _PROC_RGBA = tuple(int(COLOR_PROC[i : i + 2], 16) for i in (1, 3, 5)) + (255,)

    def __init__(self):
        import tkinter as tk
        self._rng = np.random.default_rng()
        self._dot_h = [float(DOT_R)] * NUM_DOTS
        _pos = np.linspace(-np.pi / 2, np.pi / 2, NUM_DOTS)
        self._dot_env = 0.25 + 0.75 * np.cos(_pos) ** 2
        self._spinner_angle = 0.0
        self._mode = "idle"
        self._t_frac = 1.0
        self._frame_ref = None

        root = tk.Tk()
        root.title("wispr-overlay")
        root.withdraw()
        self.root = root

        ov = tk.Toplevel(root)
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

        c = tk.Canvas(
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
        if ok:
            LLM_PROVIDER = name
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
        _pystray_MenuItem("Quit Wispr", _quit),
    )


def _load_whisper():
    global whisper, whisper_error
    print(f"Loading Whisper {WHISPER_MODEL} on {WHISPER_DEVICE}/{WHISPER_COMPUTE} …")
    from faster_whisper import WhisperModel

    attempts = [
        (WHISPER_DEVICE, WHISPER_COMPUTE),
        ("cpu", "int8"),
    ]

    last_error = None
    for device, compute_type in attempts:
        try:
            print(f"Trying Whisper on {device}/{compute_type} …")
            whisper = WhisperModel(
                WHISPER_MODEL, device=device, compute_type=compute_type
            )
            whisper_error = None
            whisper_ready.set()
            print(
                f"Whisper ready on {device}/{compute_type}. Hold the Wispr hotkey to record."
            )
            return
        except Exception as exc:
            last_error = exc
            print(f"Whisper load failed on {device}/{compute_type}: {exc}")

    whisper_error = last_error
    whisper_ready.clear()
    print("Whisper could not be loaded. Transcription is disabled until restart.")


def main():
    global tray_icon, np, sd, pyautogui, pyperclip, keyboard
    global _PIL_Image, _PIL_ImageDraw, _PIL_ImageTk
    global _pystray_Icon, _pystray_MenuItem, _pystray_Menu
    global _http, MediaManager, _media_controls_available
    global LLM_PROVIDER, LLM_FALLBACK, CLEANUP_MODE, OPENROUTER_MODEL
    global ICON_IDLE, ICON_RECORDING, ICON_PROCESSING

    _t_main_start = time.perf_counter()

    # ── Deferred heavy imports ────────────────────────────────────────────────
    # numpy must be imported single-threaded (circular import risk in threads).
    # All other packages are independent and safe to import in parallel.
    import numpy as _np
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

    def _import_requests_dotenv():
        import requests as _req
        from dotenv import load_dotenv as _ld
        return _req, _ld

    def _import_winrt():
        try:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as _MM,
            )
            return _MM, True
        except Exception:
            return None, False

    with _cf.ThreadPoolExecutor(max_workers=8) as _pool:
        _f_sd  = _pool.submit(_import_sounddevice)
        _f_pag = _pool.submit(_import_pyautogui)
        _f_pc  = _pool.submit(_import_pyperclip)
        _f_kb  = _pool.submit(_import_keyboard)
        _f_pil = _pool.submit(_import_pil)
        _f_pst = _pool.submit(_import_pystray)
        _f_rd  = _pool.submit(_import_requests_dotenv)
        _f_wrt = _pool.submit(_import_winrt)

        sd                        = _f_sd.result()
        pyautogui                 = _f_pag.result()
        pyperclip                 = _f_pc.result()
        keyboard                  = _f_kb.result()
        _PIL_Image, _PIL_ImageDraw, _PIL_ImageTk = _f_pil.result()
        _pystray_Icon, _pystray_MenuItem, _pystray_Menu = _f_pst.result()
        _requests, load_dotenv    = _f_rd.result()
        MediaManager, _media_controls_available = _f_wrt.result()

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0

    load_dotenv()
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
    LLM_FALLBACK = os.getenv("LLM_FALLBACK", "").lower()
    CLEANUP_MODE = os.getenv("CLEANUP_MODE", "punct").lower()
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", OPENROUTER_DEFAULT)

    _http = _requests.Session()
    _http.mount("http://", _requests.adapters.HTTPAdapter(max_retries=0))
    _http.mount("https://", _requests.adapters.HTTPAdapter(max_retries=0))

    # ── Build tray icons (deferred from module level) ─────────────────────────
    ICON_IDLE = _make_mic_icon("#22c55e")
    ICON_RECORDING = _make_mic_icon("#ef4444")
    ICON_PROCESSING = _make_mic_icon("#f59e0b")

    # ── LLM provider setup ────────────────────────────────────────────────────
    if not _BENCH_MODE:
        if LLM_PROVIDER != "punct":
            _setup_provider(LLM_PROVIDER, fatal=True)
        if LLM_FALLBACK and LLM_FALLBACK != LLM_PROVIDER:
            _setup_provider(LLM_FALLBACK, fatal=False)

    threading.Thread(target=_load_whisper, daemon=True).start()

    # Start stream init in background — overlaps with tray/keyboard setup
    _stream_container = [None]
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
        _stream_container[0] = s
        _stream_ready.set()

    threading.Thread(target=_init_stream, daemon=True).start()

    keyboard.hook(_on_key_event)

    tray_icon = _pystray_Icon(
        "Wispr",
        ICON_IDLE,
        "Wispr – Idle (hold the Wispr hotkey to record)",
        _build_menu(),
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()

    # Ensure stream is ready before proceeding
    _stream_ready.wait(timeout=10)
    stream = _stream_container[0]

    if _BENCH_MODE:
        _t_interactive = time.perf_counter()
        _elapsed_ms = (_t_interactive - _t_main_start) * 1000
        _bench_file = os.path.join(tempfile.gettempdir(), "wispr_bench.txt")
        with open(_bench_file, "w") as _f:
            _f.write(f"{_elapsed_ms:.1f}")
        print(f"[BENCH] interactive in {_elapsed_ms:.1f}ms (main() only)")
        print(f"[BENCH] result written to {_bench_file}")
        time.sleep(0.5)
        tray_icon.stop()
        stream.stop()
        stream.close()
        keyboard.unhook_all()
        _release_single_instance()
        os._exit(0)

    Overlay().run()


if __name__ == "__main__":
    main()
