# Local STT Model Benchmarks and Architectural Evaluation for Wispr Rust

## 1. Executive Summary

Wispr's goal is an ultra-lightweight (< 150 MB active RAM), push-to-talk voice dictation assistant on Windows with sub-200ms response latency. While OpenAI's Whisper models (`small.en` and `large-v3-turbo`) provide strong accuracy, they suffer from two fundamental architectural constraints in interactive desktop dictation:
1. Fixed 30-Second Window Zero-Padding: Whisper requires every utterance (even a 2-second snippet like "cancel appointment") to be padded to 30 seconds of audio features, wasting encoder compute.
2. Autoregressive Decoder Bottleneck and Hallucination Loops: Sequential autoregressive decoding introduces latency and risks repetitive loops or phantom phrases when users pause or release push-to-talk in noisy environments.

### Key Findings
* Fastest Non-Autoregressive Model: SenseVoiceSmall (FunAudioLLM) executes a single-pass decode, transcribing 10 seconds of speech in ~70 ms on CPU (RTF 0.007 to 0.03, > 150x real-time), with built-in Inverse Text Normalization (ITN).
* Most Efficient Variable-Length Model: Moonshine Tiny / Base (Useful Sensors) eliminates Whisper 30-second padding using Rotary Position Embeddings (RoPE), cutting compute by ~5x on short phrases (< 5s) with a tiny footprint (26 MB to 57 MB INT8).
* Highest English Accuracy: NVIDIA Parakeet-TDT 0.6B beats Whisper Large on the OpenASR benchmark while running 3x faster than standard Transducers, but requires 500+ MB RAM.
* Drop-In Compatibility with Current Stack: Distil-Whisper (`distil-medium.en`) and Whisper Large-v3-Turbo drop directly into Wispr Rust existing `whisper-rs` GGUF engine without adding new runtime dependencies.

---

## 2. Comparative Benchmark Matrix

| Model | Parameters | Disk Footprint (Quantized) | Active RAM Footprint | Inference RTF (x86_64 CPU) | Short Utterance Latency (3s audio) | LibriSpeech Clean WER | Multilingual (Romanian) | Rust Runtime Path |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Whisper small.en (Baseline) | 244M | ~150 MB (Q5_0 GGUF) | ~200 - 260 MB | ~0.08 - 0.14 | ~250 - 400 ms | ~3.4% | No (English only) | `whisper-rs` (current) |
| Whisper large-v3-turbo (Baseline) | 809M | ~550 MB (Q4_0 GGUF) | ~600 - 750 MB | ~0.10 - 0.20 | ~350 - 550 ms | ~2.5% | Yes (100+ langs) | `whisper-rs` (current) |
| Moonshine Tiny | 27M | ~26 MB (INT8 ONNX) | ~45 - 70 MB | ~0.02 - 0.04 | ~80 - 120 ms | ~4.8% | No (English only) | `ort` / `sherpa-onnx` |
| Moonshine Base | 58M | ~57 MB (INT8 ONNX) | ~85 - 120 MB | ~0.04 - 0.07 | ~130 - 180 ms | ~3.2% | No (English only) | `ort` / `sherpa-onnx` |
| SenseVoiceSmall | 234M | ~125 MB (INT8 ONNX) | ~140 - 210 MB | ~0.007 - 0.02 | ~60 - 90 ms | ~3.1% | Partial (EN, ZH, JA, KO, YUE) | `sherpa-onnx` / `ort` |
| Sherpa-onnx Zipformer | 65M | ~45 MB (INT8 ONNX) | ~60 - 95 MB | ~0.01 - 0.03 | < 50 ms (Streaming) | ~3.8% | English + select multi | `sherpa-onnx` crate |
| Distil-Whisper medium.en | 394M | ~250 MB (Q5_0 GGUF) | ~280 - 350 MB | ~0.05 - 0.09 | ~180 - 260 ms | ~3.0% | No (English only) | `whisper-rs` / `candle` |
| Distil-Whisper large-v3 | 756M | ~500 MB (Q4_0 GGUF) | ~550 - 700 MB | ~0.08 - 0.15 | ~240 - 350 ms | ~2.6% | English primary | `whisper-rs` / `candle` |
| NVIDIA Parakeet-TDT 0.6B | 600M | ~380 MB (INT8 ONNX) | ~500 - 650 MB | ~0.03 - 0.06 | ~110 - 160 ms | ~2.1% (SOTA) | No (English only) | `sherpa-onnx` |
| Phonon-2 (Fermion Research) | ~600M (distilled) | **164 MB** (2.1-bit QAT) | **~180 - 240 MB** | **~0.02 - 0.05** | **~80 - 130 ms** | **~5.2% (OpenASR)** | No (English only) | Custom C++/Wasm (`phonon-cpu`) |
| NVIDIA Canary-1B | 1000M | ~700 MB (INT8 ONNX) | ~900 - 1200 MB | ~0.15 - 0.28 | ~450 - 700 ms | ~2.5% | Yes (EN, DE, ES, FR + 20 EU) | `sherpa-onnx` |

Note: RTF = Real-Time Factor (Duration to process / Duration of audio). Values < 1.0 are faster than real time; 0.01 = 100x real-time.

---

## 3. Candidate Architectural Breakdown

### 1. Moonshine (Useful Sensors / Moonshine AI)
* Architecture: Variable-length encoder-decoder using Rotary Position Embeddings (RoPE).
* Core Edge Advantage: Traditional Whisper models convert audio into fixed 30-second Mel spectrogram frames. Moonshine encodes only the actual recorded length. For a typical push-to-talk dictation utterance of 2 to 4 seconds, Moonshine performs ~5x fewer computations than Whisper Tiny or Base.
* Accuracy: Moonshine Base (~58M params) matches `whisper-small.en` on LibriSpeech clean (~3.2% vs ~3.4% WER), while consuming less than half the RAM.
* Dictation Suitability: Ideal for quick voice commands and short sentences. Generates standard punctuation and truecased letters.

### 2. SenseVoiceSmall (FunAudioLLM / Alibaba)
* Architecture: Non-autoregressive SAN-M (Self-Attention Memory) with CTC.
* Core Edge Advantage: Generates all transcript tokens in a single forward pass rather than generating token-by-token.
  * Zero hallucination loops on silence or background noise.
  * CPU processing time: 10s of audio is transcribed in 60 to 80 ms.
  * Built-in Inverse Text Normalization (ITN): automatically outputs formatted numbers, currencies, dates, and punctuation without requiring an external LLM step.
  * Detects emotional tags and acoustic events.
* Limitation: Does not support Romanian (supports English, Mandarin, Cantonese, Japanese, Korean).

### 3. Sherpa-onnx Zipformer (Next-gen Kaldi / k2)
* Architecture: Multi-rate Zipformer (U-Net style downsampling in the temporal dimension).
* Core Edge Advantage: Designed explicitly for streaming. As the user speaks, tokens are emitted incrementally every 160ms to 320ms. When the user releases the push-to-talk hotkey, the entire transcription is virtually already completed.
* Footprint: Models are compact (~45 MB INT8) and consume < 80 MB RAM.
* Limitation: Raw Zipformer output lacks truecasing and nuanced punctuation, requiring downstream capitalization and formatting.

### 4. NVIDIA Parakeet (CTC / TDT) and Canary
* Architecture: FastConformer encoder paired with Token-and-Duration Transducer (TDT).
* Core Edge Advantage: TDT predicts both the token and the number of audio frames to advance, allowing the decoder to skip multiple frames at once. It runs ~3x faster than standard conformer transducers and holds top ranking on OpenASR for English accuracy.
* Limitation: Parakeet 0.6B and Canary 1B are heavy for an ultra-lightweight desktop background utility. They exceed Wispr < 150 MB RAM budget (Canary-1B requires ~1 GB RAM) and have complex multi-graph ONNX dependencies.

### 5. Phonon-2 (Fermion Research, often referenced as "Photon 2")
* Architecture: Quantization-aware trained distillation of NVIDIA Parakeet TDT 0.6B v3.
* Core Edge Advantage:
  * Extreme Compression: 164 MB download (~177 MB on disk), compressing the 2.5 GB Parakeet teacher by ~15x.
  * 2.1-Bit Learned Quantization: Uses 5-level learned weights with 6-bit lookup tables for fast CPU and Apple Silicon execution.
  * Direct Punctuated Output: Directly outputs punctuation, truecasing, and word-level timestamps.
  * Accuracy: Sits at ~5.21% average WER on OpenASR benchmarks, matching the accuracy of the full teacher model and rivaling Whisper Large on English.
* Limitation for Wispr Rust:
  * English only (no Romanian or multilingual support).
  * Lacks an official native Rust crate. Currently distributed as Python (`fermion-research`), C++ Docker images (`phonon-cpu`), and community WebAssembly/WebGPU ports. Direct integration in Wispr Rust would require either wrapping `phonon-cpu` via FFI or running an internal localhost inference server.

### 6. Distil-Whisper (`distil-medium.en` / `distil-large-v3`)
* Architecture: Knowledge distillation with decoder pruned to 2 layers (from 24 or 32).
* Core Edge Advantage: Preserves the Whisper vocabulary, prompt conditioning, and GGUF quantization format. Runs ~6x faster than Whisper large-v3 with negligible accuracy drop (< 1% WER difference).
* Limitation: Still requires 30-second Mel spectrogram zero-padding. Superseded for multilingual use by OpenAI official `large-v3-turbo`.

---

## 4. Rust Runtime and Ecosystem Feasibility

| Runtime | Crates | Backends | Model Support | Pros | Cons |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `whisper-rs` | `whisper-rs` | CPU (AVX2/AVX-512), Vulkan, CUDA | Whisper, Distil-Whisper, Large-v3-Turbo | Zero extra DLLs, mature GGUF quantization, already integrated in Wispr Rust | Locked to Whisper-style encoder-decoder; cannot run Moonshine or SenseVoice |
| `sherpa-onnx` | `sherpa-onnx`, `sherpa-onnx-sys` | CPU, DirectML, CUDA, Vulkan | SenseVoice, Moonshine, Zipformer, Parakeet, Whisper | Official safe Rust API, prebuilt binaries available, supports both streaming and offline | Requires linking `sherpa-onnx` C library (~15 MB native DLL bundle) |
| `ort` (ONNX Runtime) | `ort` (v2.0) | DirectML, CPU, CUDA | Any ONNX model (Moonshine, SenseVoice, Silero) | Native DirectML works across AMD, Intel, and NVIDIA on Windows; standard Microsoft stack | Requires implementing custom audio STFT/Mel feature extraction in Rust |
| `candle` | `candle-core`, `candle-transformers` | CPU, CUDA, Metal | Whisper, Distil-Whisper | 100% pure Rust, zero C/C++ compilation | Slower CPU inference than `whisper.cpp` or ONNX Runtime; lacks heavily optimized INT8 GEMM kernels |

---

## 5. Concrete Recommendations for Wispr Rust

Wispr Rust technical specification requires:
* Hotkey 1 (`Ctrl + Win`): Fast English dictation.
* Hotkey 2 (`Shift + Win`): Multilingual dictation (specifically Romanian).
* Active RAM budget: < 150 MB.

### Strategy A: Immediate Win (Zero Code Changes)
* English Dictation (`Ctrl + Win`): Switch from `small.en` (244M) to `distil-medium.en` (Q5_0 GGUF) or keep `small.en` quantized at Q5_0.
* Romanian / Multilingual (`Shift + Win`): Use `whisper-large-v3-turbo` (Q4_0 GGUF). At 550 MB, it gives strong Romanian transcription at 8x the speed of full `large-v3`.
* Verdict: Requires zero code changes to Wispr Rust engine, only swapping the GGUF weights loaded by `whisper-rs`.

### Strategy B: Next-Gen Sub-100MB Architecture (Recommended Evolution)
Adopt the `sherpa-onnx` Rust crate alongside or replacing `whisper-rs`:
1. Tier 1 (English Dictation): Run SenseVoiceSmall (INT8) or Moonshine Base (INT8).
   * SenseVoiceSmall yields instantaneous transcription (~70ms) with zero hallucination risk, and built-in ITN means basic punctuation and numbers format correctly even if the downstream LLM times out.
   * Moonshine Base drops memory consumption to ~80 MB RAM with identical WER to `small.en`.
2. Tier 2 (Multilingual / Romanian): Keep `whisper-rs` with `large-v3-turbo-q4_0.gguf` mapped via `mmap` only when `Shift + Win` is invoked, or load `sherpa-onnx-whisper`.
3. Hardware Acceleration: Leverage DirectML via ONNX Runtime on Windows, giving GPU acceleration across Intel Arc, AMD Radeon, and NVIDIA RTX cards without shipping 800 MB of CUDA DLLs.
