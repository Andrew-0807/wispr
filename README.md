# Wispr

Ultra-lightweight system-wide voice dictation tool for Windows in native Rust.

Hold **Ctrl + Win** (English) or **Shift + Win** (Multilingual / Romanian) to record, release to transcribe and paste into the active window.

> [!NOTE]
> The primary implementation is the native Rust engine located in `Wispr_rust/`.
> The original Python implementation has been moved to `wispr_py/` as an archived reference.

---

## Architecture and Implementations

```
wispr/
├── Wispr_rust/         # Active: Native Rust desktop assistant (<150 MB RAM, instant launch)
├── wispr_py/           # Archived: Python reference implementation
├── docs/               # Technical specs and STT model benchmark reports
└── autoresearch/       # Evaluation frameworks and startup experiments
```

| Metric | Wispr Rust (`Wispr_rust/`) | Wispr Python (`wispr_py/` - Archived) |
| :--- | :--- | :--- |
| Runtime Overhead | < 15 MB Win32 | ~250 - 350 MB Python/Tkinter |
| Active RAM | ~150 - 250 MB | ~700 - 885 MB |
| Startup Time | < 100 ms | ~1.1 - 4.0 s |
| Binary Packaging | Single native executable | PyInstaller bundle or Python venv |

---

## How It Works

1. **Record**: Hold `Ctrl + Win` (or `Shift + Win`). A floating pill overlay slides up with an animated audio visualizer. Active media playback pauses automatically.
2. **Transcribe**: On release, local Whisper inference runs with Vulkan GPU acceleration.
3. **Clean up**: Raw transcription runs through custom dictionary replacements (`words.json`) and optional LLM formatting (filler removal, punctuation).
4. **Paste**: Cleaned text injects into the active window, previous clipboard contents restore, and media playback resumes.

---

## Getting Started (Wispr Rust)

### Requirements

* Windows 10 or 11 (x64)
* Visual Studio Build Tools (C++ MSVC)
* LLVM (`scoop install llvm`)
* Vulkan SDK or supported GPU driver

### Setup and Build

1. Copy `.env.example` to `.env` in `Wispr_rust/`:
   ```bat
   cd Wispr_rust
   copy .env.example .env
   ```
2. Configure your LLM provider in `.env` (Gemini, OpenRouter, LM Studio, Ollama).
3. Place GGML / GGUF model weights into `Wispr_rust/models/`:
   * `ggml-small.en.bin` for English (`Ctrl + Win`)
   * `ggml-large-v3-turbo.bin` for Multilingual (`Shift + Win`)
4. Build release binary:
   ```bat
   build.bat
   ```
5. Run the assistant:
   ```bat
   run.bat
   ```

---

## Word Corrections

Edit `words.json` in `Wispr_rust/` to substitute recurring misrecognitions. Keys are case-insensitive; longest match takes precedence:

```json
{
  "cloud code": "Claude Code",
  "cloud": "Claude"
}
```

---

## Archived Python Version

The Python implementation using `faster-whisper` is preserved in `wispr_py/`.
See `wispr_py/README.md` for historical running instructions.
