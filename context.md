# Wispr Architecture & Implementation Context

This repository contains two parallel implementations of the Wispr voice dictation assistant: **`wispr_py`** (Python) and **`Wispr_rust`** (Rust).

---

## Why Two Implementations?

Wispr is an ultra-low-friction system-wide voice-to-text assistant with AI post-processing (filler word removal, punctuation fixing, custom word expansion).

As the application evolved, memory overhead became the primary bottleneck:

```
+──────────────────────────────────────────────────────────────────────────+
|  Implementation Comparison                                               |
+───────────────────────────+──────────────────────+───────────────────────+
| Metric                    | wispr_py (Python)    | Wispr_rust (Rust)     |
+───────────────────────────+──────────────────────+───────────────────────+
| Active RAM Usage          | ~700 MB – 885 MB     | ~150 MB – 250 MB      |
| Host/App Runtime Overhead | ~250 MB – 350 MB     | ~5 MB – 15 MB         |
| Cold Startup Time         | ~2.0s – 4.0s         | < 100ms               |
| Inference Engine          | faster-whisper       | whisper.cpp / candle  |
| Quantization Support      | INT8 / FP16          | Q4_0, Q5_0, Q8_0 GGUF |
| Binary Packaging          | PyInstaller bundle   | Single native .exe    |
+───────────────────────────+──────────────────────+───────────────────────+
```

---

## 1. `wispr_py/` — Python Reference Implementation

The original, feature-complete implementation.

* **Stack:** Python 3.10+, `faster-whisper` (CTranslate2), `sounddevice`, `pystray`, `tkinter`, `pyautogui`, `pyperclip`, `keyboard`.
* **Strengths:** Rapid iteration, native access to rich Python ML ecosystems, easily configurable LLM providers (Gemini, OpenRouter, LM Studio, Ollama).
* **Limitations:** High memory footprint (Python runtime + CUDA/OpenMP DLL mappings consume ~800+ MB), higher CPU/RAM overhead at idle.
* **Role:** Serves as the golden reference implementation for features, UX flow, prompt formats, and behavior.

---

## 2. `Wispr_rust/` — Rust High-Performance Implementation

The next-generation, lightweight native rewrite.

* **Stack:** Rust, `whisper-rs` (`whisper.cpp` bindings) or `candle`, `cpal` (audio capture), `windows-rs` / `tray-icon` / `global-hotkey`, `reqwest` (LLM API clients).
* **Objectives:**
  1. **Slash RAM footprint:** Reduce idle and running RAM to ~150 MB (or <80 MB using quantized `base`/`tiny` GGUF models).
  2. **Zero-cost host application:** Replace Python/Tkinter overhead with native Windows Win32 API calls consuming <15 MB.
  3. **Instant activation:** Fast launch with static memory arenas and memory-mapped (`mmap`) weights.
  4. **Single self-contained binary:** No Python runtime extraction or DLL bundling issues.

---

## Repository Layout

```
wispr/
├── context.md          # Architecture rationale and project overview (this file)
├── AGENTS.md           # Agent routing rules and development guidelines
├── README.md           # General project documentation
├── autoresearch/       # Evaluation frameworks, benchmarks, and research logs
├── wispr_py/           # Python reference implementation
│   ├── wispr.py        # Core Python engine and UI
│   ├── launcher.py     # Windows launcher script
│   ├── requirements.txt# Python dependencies
│   ├── words.json      # Word substitution dictionary
│   └── run.bat         # Execution script
└── Wispr_rust/         # Native Rust rewrite
```
