# Wispr

Hold **Win + Space** to dictate. Release to transcribe and paste into the focused window.

- Transcription: faster-whisper `large-v3-turbo`, CUDA float16
- Cleanup: Gemini 2.0 Flash (removes fillers, fixes grammar)
- Overlay: floating audio-level visualizer at bottom-center, hides when idle
- Tray icon shows state (green = idle, red = recording, amber = transcribing)
- Pauses / resumes media playback around each dictation

## Requirements

- Windows 10/11
- Python 3.11+
- CUDA-capable GPU (tested on RTX 3060 12 GB)
- [Gemini API key](https://aistudio.google.com/app/apikey)

## Setup

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your key:

```
GEMINI_API_KEY=your_key_here
```

## Run

```
run.bat
```

Right-click → **Run as administrator** if the hotkey doesn't respond (required for global keyboard hooks).

First run downloads the Whisper model (~1.5 GB) automatically.

## komorebi

```
komorebic ignore-rule title "wispr-overlay"
```

## Files

| File | Purpose |
|---|---|
| `wispr.py` | entire app |
| `.env` | API key (gitignored) |
| `requirements.txt` | Python dependencies |
| `run.bat` | launcher |
