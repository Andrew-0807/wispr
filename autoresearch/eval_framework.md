# Eval Framework — Wispr Startup Time

## Primary Metric
Wall-clock milliseconds from `python wispr.py --bench` invocation until the process exits cleanly.
Parsed from the PowerShell `$elapsed.TotalMilliseconds` output.

## Baseline
To be established on first run (experiment #0 / pre-loop measurement).

## Scoring
- Score = negative elapsed ms (higher = better, i.e., faster startup)
- Keep if score >= baseline score (faster or equal)
- Discard if score < baseline score (slower)
- Simplicity criterion: if score == baseline, keep if the change also removes code

## Success Target
Reduce startup time by >= 30% from baseline.

## Correctness Guard
After each kept change, verify:
1. `--bench` exits cleanly with exit code 0
2. No ImportError or AttributeError in output
3. The timing output is parseable (contains "Startup:")

## What counts as "interactive"
- Tray icon visible (pystray Icon.run() called)
- Keyboard hook active (keyboard.hook() called)
- Audio stream running (sd.InputStream.start() called)
- These must all happen BEFORE the bench timer records completion
