# Hypothesis Bank

Priority order (highest impact first):

## Remaining / Not Yet Tried

## H-NEW-1 [HIGH] Replace `keyboard` library with `pynput` for hook
`keyboard` library hook takes 167ms on main thread. `pynput` uses a different
Windows API path and may be faster. Would need to rewrite _on_key_event logic.
Expected gain: 50-150ms

## H-NEW-2 [HIGH] Pre-warm Python bytecache via subprocess
Launch a short warm-up script that pre-imports all heavy packages and exits,
so .pyc files are in OS file cache on next run. Only helps on cold start.
Expected gain: 100-300ms on cold start, 0 on warm start

## H-NEW-3 [MEDIUM] Reduce sounddevice blocksize to speed up stream init
Larger blocksize = longer first-buffer wait. Try blocksize=256 instead of 1024.
Expected gain: 10-30ms

## H-NEW-4 [MEDIUM] Combine keyboard.hook + stream init in a single thread sequence
Since keyboard.hook takes 167ms and stream takes ~165ms (both sequential now),
try: start stream thread first, then do keyboard.hook on main, then wait for both.
This is the current exp4 approach. Already done — verify nothing further to overlap.

## H-NEW-5 [LOW] Use importlib.import_module with __import__ cache check
Before spawning thread for each package, check sys.modules first to skip
already-imported packages on hot restart.
Expected gain: negligible in cold start

## Completed (kept)
- H1: lazy imports → exp1 (keep, -21.7%)
- H3: parallel imports → exp3 (keep, -23.8%)
- H4: background stream init → exp4 (keep, -27.2%)

## Discarded
- H2 (exp2): parallel numpy → circular import crash
- H5 (exp5): background tray icon → threading overhead > savings
- H6/exp6: skip load_dotenv in bench mode → branching overhead > savings
- H7/exp7: skip icon render in bench mode → PIL already loaded
- H8/exp8: skip 3 parallel imports in bench mode → bottleneck is slowest, not count
- H9/exp9: background keyboard.hook → Windows hook must be on main thread (+196ms)
- H10/exp10: defer WORD_MAP to parallel pool → disk I/O negligible
