#!/usr/bin/env python3
"""
bench.py — Wispr Whisper latency benchmark (AutoResearch-style)

Iterates every config, measures latency, compares quality, reports winner.
Optionally writes the best config back to wispr.py.

Usage:
  python bench.py              # use existing bench_audio.npy or prompt to record
  python bench.py --record     # force re-record a fresh clip
  python bench.py --apply      # write winning config to wispr.py after benchmarking
  python bench.py --record --apply
"""

import os
import re
import sys
import time
import json
import wave
import tempfile
import subprocess
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

# ── Benchmark settings ────────────────────────────────────────────────────────

SAMPLE_RATE    = 16_000
RECORD_SECONDS = 6        # speak naturally for this many seconds
RUNS_PER_MODEL = 3        # 1 warmup + 2 timed; increase for more accuracy
BEAM_SIZE      = 1        # keep fixed — this is already optimal
AUDIO_PATH     = os.path.join(os.path.dirname(__file__), "bench_audio.npy")
RESULTS_PATH   = os.path.join(os.path.dirname(__file__), "bench_results.json")
WISPR_PATH     = os.path.join(os.path.dirname(__file__), "wispr.py")

# ── Config search space ───────────────────────────────────────────────────────
# (model, compute_type)  — device is always cuda; skips automatically on error

CONFIGS = [
    ("large-v3-turbo",  "float16"),       # current baseline
    ("large-v3-turbo",  "int8_float16"),  # same model, lower precision
    ("distil-large-v3", "float16"),       # distilled large, ~2x faster
    ("distil-large-v3", "int8_float16"),  # distilled + int8
    ("medium",          "float16"),
    ("medium",          "int8_float16"),
    ("medium.en",       "float16"),       # English-only, slightly faster
    ("medium.en",       "int8_float16"),
    ("small",           "float16"),
    ("small.en",        "float16"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

TTS_TEXT = (
    "Hello, this is a benchmark test for the Wispr voice transcription system. "
    "I'm speaking at a natural pace to compare different Whisper models. "
    "The quick brown fox jumps over the lazy dog. "
    "Testing one, two, three — can you hear me clearly? "
    "Artificial intelligence is transforming how we interact with computers every day."
)

def generate_tts_audio() -> np.ndarray:
    """Generate test audio using Windows TTS — no microphone needed."""
    wav_path = os.path.join(tempfile.gettempdir(), "wispr_bench_tts.wav")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav_path}'); "
        "$s.Rate = 0; "
        f"$s.Speak(\"{TTS_TEXT}\"); "
        "$s.SetOutputToDefaultAudioDevice(); "
        "$s.Dispose()"
    )
    print("Generating TTS audio via Windows Speech...", flush=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    with wave.open(wav_path, "rb") as wf:
        frames   = wf.readframes(wf.getnframes())
        rate     = wf.getframerate()
        channels = wf.getnchannels()
        width    = wf.getsampwidth()

    dtype = np.int16 if width == 2 else np.int32
    scale = 32768.0 if width == 2 else 2147483648.0
    audio = np.frombuffer(frames, dtype=dtype).astype(np.float32) / scale

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    if rate != SAMPLE_RATE:
        ratio  = SAMPLE_RATE / rate
        n_out  = int(len(audio) * ratio)
        audio  = np.interp(
            np.linspace(0, len(audio) - 1, n_out),
            np.arange(len(audio)), audio
        ).astype(np.float32)
    print(f"TTS clip: {len(audio)/SAMPLE_RATE:.1f}s\n")
    return audio


def record_audio() -> np.ndarray:
    print(f"\nSpeak for {RECORD_SECONDS}s — use natural sentences, not single words.")
    print("Recording in 1s...", flush=True)
    time.sleep(1.0)
    print("GO", flush=True)
    raw = sd.rec(int(RECORD_SECONDS * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                 channels=1, dtype="float32")
    sd.wait()
    print("Done.\n")
    return raw.flatten()


def run_config(model_name: str, compute: str, audio: np.ndarray) -> dict | None:
    label = f"{model_name}/{compute}"
    print(f"  {label:<40}", end="", flush=True)

    try:
        load_t0 = time.perf_counter()
        m = WhisperModel(model_name, device="cuda", compute_type=compute)
        load_s = time.perf_counter() - load_t0
    except Exception as e:
        print(f"LOAD ERROR: {e}")
        return None

    latencies = []
    output    = ""

    audio_f32 = audio.astype(np.float32)
    for i in range(RUNS_PER_MODEL):
        t0       = time.perf_counter()
        segments, _ = m.transcribe(audio_f32, beam_size=BEAM_SIZE, vad_filter=True)
        text     = " ".join(s.text.strip() for s in segments).strip()
        elapsed  = time.perf_counter() - t0

        if i == 0:
            output = text          # warmup — captures output but not latency
        else:
            latencies.append(elapsed * 1000)

    mean_ms = int(np.mean(latencies))
    min_ms  = int(np.min(latencies))
    print(f"  mean {mean_ms:>5}ms   min {min_ms:>5}ms   (load {load_s:.1f}s)")

    del m   # free VRAM before next model loads

    return {
        "model":   model_name,
        "compute": compute,
        "label":   label,
        "mean_ms": mean_ms,
        "min_ms":  min_ms,
        "load_s":  round(load_s, 1),
        "output":  output,
    }


def apply_to_wispr(result: dict):
    """Patch WHISPER_MODEL and WHISPER_COMPUTE in wispr.py."""
    with open(WISPR_PATH, "r", encoding="utf-8") as f:
        src = f.read()

    src = re.sub(r'(WHISPER_MODEL\s*=\s*")[^"]*(")',
                 rf'\g<1>{result["model"]}\2', src)
    src = re.sub(r'(WHISPER_COMPUTE\s*=\s*")[^"]*(")',
                 rf'\g<1>{result["compute"]}\2', src)

    with open(WISPR_PATH, "w", encoding="utf-8") as f:
        f.write(src)

    print(f"\n  wispr.py updated → WHISPER_MODEL={result['model']}  "
          f"WHISPER_COMPUTE={result['compute']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    force_record = "--record" in sys.argv
    auto_apply   = "--apply"  in sys.argv
    use_tts      = "--tts"    in sys.argv or (not force_record and not os.path.exists(AUDIO_PATH))

    # ── Audio ─────────────────────────────────────────────────────────────────
    if force_record:
        audio = record_audio()
        np.save(AUDIO_PATH, audio)
        print(f"Saved clip to bench_audio.npy ({RECORD_SECONDS}s)\n")
    elif use_tts:
        audio = generate_tts_audio()
        np.save(AUDIO_PATH, audio)
        print("Saved TTS clip to bench_audio.npy  —  pass --record to use your voice instead\n")
    else:
        audio = np.load(AUDIO_PATH)
        dur   = len(audio) / SAMPLE_RATE
        print(f"Using existing bench_audio.npy ({dur:.1f}s)  —  pass --record to refresh\n")

    # ── Iterate configs ───────────────────────────────────────────────────────
    print(f"Running {len(CONFIGS)} configs × {RUNS_PER_MODEL - 1} timed runs each:\n")
    print(f"  {'Config':<40}  {'Mean':>8}   {'Min':>8}   (load)")
    print("  " + "-" * 62)

    results = []
    for model_name, compute in CONFIGS:
        r = run_config(model_name, compute, audio)
        if r:
            results.append(r)
            with open(RESULTS_PATH, "w") as f:
                json.dump(results, f, indent=2)

    if not results:
        print("No successful runs.")
        return

    # ── Report ────────────────────────────────────────────────────────────────
    results.sort(key=lambda r: r["mean_ms"])
    baseline = next((r for r in results if r["model"] == "large-v3-turbo"
                     and r["compute"] == "float16"), results[-1])

    print("\n" + "=" * 68)
    print(f"  {'Rank':<5} {'Config':<40} {'Mean':>7} {'Speedup':>9}")
    print("=" * 68)
    for rank, r in enumerate(results, 1):
        speedup = f"{baseline['mean_ms'] / r['mean_ms']:.2f}x"
        marker  = "  ← WINNER" if rank == 1 else ""
        flag    = "  [baseline]" if r["label"] == baseline["label"] else ""
        print(f"  {rank:<5} {r['label']:<40} {r['mean_ms']:>5}ms {speedup:>9}{marker}{flag}")
    print("=" * 68)

    winner = results[0]
    print(f"\nWinner : {winner['label']}")
    print(f"Speedup: {baseline['mean_ms'] / winner['mean_ms']:.2f}x faster than baseline")
    print(f"Output : {winner['output'][:100]}")

    # Quality warning — if output diverges significantly from baseline
    baseline_out = baseline.get("output", "")
    if baseline_out and winner["output"]:
        common = sum(w in winner["output"].lower() for w in baseline_out.lower().split())
        total  = len(baseline_out.split())
        if total > 0 and common / total < 0.7:
            print("\n  WARNING: winner output differs >30% from baseline — check quality manually.")

    if auto_apply:
        apply_to_wispr(winner)
    else:
        print(f"\nTo apply winner to wispr.py, re-run with --apply")
        print(f"  WHISPER_MODEL   = \"{winner['model']}\"")
        print(f"  WHISPER_COMPUTE = \"{winner['compute']}\"")


if __name__ == "__main__":
    main()
