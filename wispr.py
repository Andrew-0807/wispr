#!/usr/bin/env python3
"""
Wispr - Windows voice dictation tool
Hold both Ctrl keys to record, release to transcribe and paste.
"""

import os
import sys
import time
import threading
import queue
import numpy as np
import sounddevice as sd
import pyautogui
import pyperclip
import keyboard
import tkinter as tk
from PIL import Image, ImageDraw
from pystray import Icon, MenuItem, Menu
from dotenv import load_dotenv
from faster_whisper import WhisperModel
import google.generativeai as genai

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE     = 16_000
CHANNELS        = 1
WHISPER_MODEL   = "large-v3-turbo"
WHISPER_DEVICE  = "cuda"
WHISPER_COMPUTE = "float16"
BEAM_SIZE       = 1

GEMINI_MODEL  = "gemini-2.0-flash"
GEMINI_SYSTEM = (
    "You are a voice transcription cleanup assistant. "
    "The user will give you raw speech-to-text output. "
    "Your job: remove filler words (um, uh, like, you know, etc.), "
    "fix grammar, fix punctuation, and keep the original tone and intent. "
    "Return ONLY the cleaned text. No explanations, no quotes, no preamble."
)

# Overlay
NUM_BARS       = 14
BAR_W          = 7
BAR_GAP        = 4
BAR_MIN_H      = 3
BAR_MAX_H      = 46
OVERLAY_W      = NUM_BARS * (BAR_W + BAR_GAP) + 48
OVERLAY_H      = 76
TASKBAR_CLEAR  = 54       # px above bottom of screen
COLOR_BG       = "#0f172a"
COLOR_BAR_REC  = "#38bdf8"   # sky-400
COLOR_BAR_PROC = "#fb923c"   # orange
COLOR_REC_DOT  = "#f43f5e"   # rose-500
COLOR_PROC_DOT = "#fb923c"

pyautogui.FAILSAFE = False
pyautogui.PAUSE    = 0

# ── Init ──────────────────────────────────────────────────────────────────────

load_dotenv()
gemini_api_key = os.getenv("GEMINI_API_KEY", "")
if not gemini_api_key or gemini_api_key == "your_gemini_api_key_here":
    sys.exit("ERROR: Set GEMINI_API_KEY in .env before running.")

genai.configure(api_key=gemini_api_key)
gemini = genai.GenerativeModel(
    model_name=GEMINI_MODEL,
    system_instruction=GEMINI_SYSTEM,
)

print(f"Loading Whisper {WHISPER_MODEL} on {WHISPER_DEVICE}/{WHISPER_COMPUTE} …")
whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
print("Whisper ready.  Hold Win + Space to record, release to transcribe.")

# ── Global state ──────────────────────────────────────────────────────────────

recording            = False
audio_chunks: list   = []
audio_level          = 0.0
tray_icon: Icon | None = None

# "idle" | "recording" | "processing"
status_queue: queue.Queue[str] = queue.Queue()

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

# ── Transcription + Gemini cleanup ────────────────────────────────────────────

def transcribe_and_paste(audio: np.ndarray):
    status_queue.put("processing")

    audio_f32 = audio.flatten().astype(np.float32)
    segments, _ = whisper.transcribe(
        audio_f32,
        beam_size=BEAM_SIZE,
        vad_filter=True,
    )
    raw = " ".join(seg.text.strip() for seg in segments).strip()

    if not raw:
        print("Nothing detected.")
        status_queue.put("idle")
        return

    print(f"Raw:     {raw}")

    try:
        response = gemini.generate_content(raw)
        cleaned  = response.text.strip()
    except Exception as exc:
        print(f"Gemini error: {exc}  —  using raw transcript.")
        cleaned = raw

    print(f"Cleaned: {cleaned}")

    pyperclip.copy(cleaned)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.15)
    pyautogui.press("playpause")    # resume music

    status_queue.put("idle")

# ── Keyboard logic ────────────────────────────────────────────────────────────

def _on_both_ctrl_pressed():
    global recording, audio_chunks
    if recording:
        return
    recording    = True
    audio_chunks = []
    pyautogui.press("playpause")    # pause music
    status_queue.put("recording")
    print("Recording…")

def _on_both_ctrl_released():
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
    name = (event.name or "").lower()
    if "windows" in name:
        _win_down = is_down
    elif name == "space":
        _space_down = is_down
    both = _win_down and _space_down
    if both and not _combo_active:
        _combo_active = True
        _on_both_ctrl_pressed()
    elif not both and _combo_active:
        _combo_active = False
        _on_both_ctrl_released()

# ── Overlay (tkinter) ─────────────────────────────────────────────────────────

class Overlay:
    def __init__(self):
        self._rng   = np.random.default_rng()
        self._bar_h = [float(BAR_MIN_H)] * NUM_BARS

        root = tk.Tk()
        root.title("wispr-overlay")
        root.withdraw()
        self.root = root

        ov = tk.Toplevel(root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.wm_attributes("-topmost",    True)
        ov.wm_attributes("-alpha",      0.90)
        ov.wm_attributes("-toolwindow", True)   # exclude from Alt-Tab
        ov.configure(bg=COLOR_BG)

        sw = ov.winfo_screenwidth()
        sh = ov.winfo_screenheight()
        x  = (sw - OVERLAY_W) // 2
        y  =  sh - OVERLAY_H - TASKBAR_CLEAR
        ov.geometry(f"{OVERLAY_W}x{OVERLAY_H}+{x}+{y}")

        c = tk.Canvas(ov, width=OVERLAY_W, height=OVERLAY_H,
                      bg=COLOR_BG, highlightthickness=0)
        c.pack()

        self._lbl = c.create_text(
            OVERLAY_W // 2, 15,
            text="● Recording",
            fill=COLOR_REC_DOT,
            font=("Segoe UI", 9, "bold"),
        )

        self._bars = []
        total_bar_w = NUM_BARS * BAR_W + (NUM_BARS - 1) * BAR_GAP
        bx     = (OVERLAY_W - total_bar_w) // 2
        base_y = OVERLAY_H - 12
        for i in range(NUM_BARS):
            x0  = bx + i * (BAR_W + BAR_GAP)
            bar = c.create_rectangle(
                x0, base_y - BAR_MIN_H, x0 + BAR_W, base_y,
                fill=COLOR_BAR_REC, outline="",
            )
            self._bars.append(bar)

        self._canvas = c
        self._ov     = ov
        self._base_y = base_y

        self._tick()
        self._poll()

    # ── animation ────────────────────────────────────────────────────────────

    def _tick(self):
        """Animate bars at 20 fps."""
        level = min(audio_level * 4.0, 1.0) if recording else 0.0
        for i, bar in enumerate(self._bars):
            target = BAR_MIN_H + (BAR_MAX_H - BAR_MIN_H) * level * float(
                self._rng.uniform(0.25, 1.0)
            )
            h = self._bar_h[i] + (target - self._bar_h[i]) * 0.35
            self._bar_h[i] = h
            x0, _, x1, _ = self._canvas.coords(bar)
            self._canvas.coords(bar, x0, self._base_y - h, x1, self._base_y)
        self.root.after(50, self._tick)

    # ── status polling ────────────────────────────────────────────────────────

    def _poll(self):
        """Drain the status queue on the tkinter thread."""
        try:
            while True:
                state = status_queue.get_nowait()
                self._apply(state)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _apply(self, state: str):
        if state == "recording":
            self._canvas.itemconfig(self._lbl, text="● Recording",     fill=COLOR_REC_DOT)
            for b in self._bars:
                self._canvas.itemconfig(b, fill=COLOR_BAR_REC)
            self._ov.deiconify()
            self._ov.lift()
        elif state == "processing":
            self._canvas.itemconfig(self._lbl, text="⟳ Transcribing…", fill=COLOR_PROC_DOT)
            for b in self._bars:
                self._canvas.itemconfig(b, fill=COLOR_BAR_PROC)
            self._ov.deiconify()
            self._ov.lift()
        else:
            self._ov.withdraw()
        _update_tray(state)

    def run(self):
        self.root.mainloop()

# ── Entry point ───────────────────────────────────────────────────────────────

def _quit(icon: Icon, item):
    icon.stop()
    os._exit(0)

def main():
    global tray_icon

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

    Overlay().run()   # tkinter mainloop – blocks until quit

if __name__ == "__main__":
    main()
