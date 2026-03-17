# Wispr

Windows voice dictation tool. Hold **Win + Space** to record, release to transcribe and paste into whatever window is focused.

## How it works

1. **Record** — hold Win + Space; a pill-shaped overlay slides up from the taskbar showing a live audio visualizer
2. **Transcribe** — on release, [faster-whisper](https://github.com/SYSTRAN/faster-whisper) runs locally on your GPU
3. **Clean up** — raw transcript is sent to an LLM to remove fillers, fix grammar, and fix punctuation
4. **Paste** — cleaned text is copied to clipboard and pasted automatically

Media playback is paused while you speak and resumed after pasting.

## Features

- Push-to-talk with **Win + Space** — works globally, across all apps
- Local speech recognition via faster-whisper (no audio leaves your machine)
- LLM cleanup via Gemini, OpenRouter, LM Studio, or Ollama — with automatic fallback
- Floating overlay with audio-level visualizer and processing spinner
- System tray icon (green = idle, red = recording, amber = transcribing)
- Word correction dictionary for recurring misrecognitions

## Requirements

- Windows 10 / 11
- Python 3.11+
- CUDA-capable GPU (tested on RTX 3060 12 GB)
- An LLM provider (see [Configuration](#configuration))

## Installation

```bat
setup.bat
```

Or manually:

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and configure your LLM provider:

```env
LLM_PROVIDER=gemini
```

### LLM providers

| Provider | `LLM_PROVIDER` value | Required env vars |
| --- | --- | --- |
| Google Gemini | `gemini` | `GEMINI_API_KEY` |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY`, optionally `OPENROUTER_MODEL` |
| LM Studio (local) | `lmstudio` | optionally `LMSTUDIO_URL`, `LMSTUDIO_MODEL` |
| Ollama (local) | `ollama` | optionally `OLLAMA_URL`, `OLLAMA_MODEL` |

Get a free Gemini key at [aistudio.google.com](https://aistudio.google.com/app/apikey).

**Fallback:** set `LLM_FALLBACK` to a second provider. If the primary is unreachable, it falls back automatically and recovers when the primary comes back.

```env
LLM_PROVIDER=gemini
LLM_FALLBACK=lmstudio
```

**Local LLM recommendations** (fast, low RAM):

- LM Studio: `qwen2.5-1.5b-instruct` (~1 GB)
- Ollama: `qwen2.5:1.5b` — install with `ollama pull qwen2.5:1.5b`

## Running

```bat
run.bat
```

Right-click → **Run as administrator** if the hotkey doesn't respond. Administrator rights are required for global keyboard hooks.

First launch downloads the Whisper model (~800 MB for `small.en`) automatically.

### Changing the Whisper model

Edit the `WHISPER_MODEL` constant at the top of [wispr.py](wispr.py):

| Model | Size | Speed | Accuracy |
| --- | --- | --- | --- |
| `tiny.en` | ~75 MB | fastest | lowest |
| `small.en` | ~460 MB | fast | good |
| `medium.en` | ~1.5 GB | moderate | better |
| `large-v3-turbo` | ~1.5 GB | moderate | best |

### Python path

`run.bat` auto-detects a `.venv` in the project folder. If you use a conda env or system Python, edit the `PYTHON=` line in `run.bat` to point to the right interpreter.

## Word corrections

Edit `words.json` to fix recurring misrecognitions. Keys are case-insensitive; longest match wins:

```json
{
  "cloud code": "Claude Code",
  "cloud": "Claude"
}
```

Replacements run after LLM cleanup using whole-word matching.

## komorebi

Exclude the overlay window from tiling:

```bat
komorebic ignore-rule title "wispr-overlay"
```

## Files

| File | Purpose |
| --- | --- |
| `wispr.py` | entire app |
| `words.json` | word correction dictionary |
| `.env` | API keys and provider config (gitignored) |
| `.env.example` | config template |
| `requirements.txt` | Python dependencies |
| `run.bat` | launcher |
| `setup.bat` | one-time dependency installer |
