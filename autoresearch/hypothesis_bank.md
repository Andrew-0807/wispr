# Hypothesis Bank

Priority order (highest impact first):

## H1 [HIGH] Add --bench flag + lazy imports for sounddevice, keyboard, pystray, PIL
Move heavy imports inside main() so they only load when main() is called, not at module level.
Also add the --bench flag infrastructure.
Expected gain: 30-50% (PIL + sounddevice are the heaviest imports)

## H2 [HIGH] Lazy icon creation
Move ICON_IDLE/RECORDING/PROCESSING from module-level to inside main() or _build_menu().
Currently drawn at import time via 3x _make_mic_icon() calls.
Expected gain: 5-15%

## H3 [HIGH] Defer sounddevice stream init to background thread
sd.InputStream() + start() blocks; move to a background thread that starts immediately.
Expected gain: 100-300ms depending on audio driver init

## H4 [MEDIUM] Defer winrt import with importlib
Use importlib.import_module() inside a try block instead of top-level import.
Expected gain: 5-20ms (winrt COM init)

## H5 [MEDIUM] Defer requests.Session() creation
The _http Session and adapter mounts happen at module level. Move inside a function.
Expected gain: 5-10ms

## H6 [MEDIUM] Parallelize icon creation + stream init with ThreadPoolExecutor
Both can run concurrently — icon creation (PIL) and sd.InputStream() init.
Expected gain: 20-50ms

## H7 [MEDIUM] Defer load_dotenv() + env var reads to inside main()
load_dotenv() does disk I/O. Move it inside main().
Expected gain: 5-15ms

## H8 [LOW] Use importlib lazy loader for rarely-used modules
Wrap tkinter, pyautogui, pyperclip in lazy import proxies.
Expected gain: 10-30ms

## H9 [LOW] Pre-compile regex patterns at module level
_apply_word_map re.sub calls compile regex each time — move to cached patterns.
Expected gain: negligible at startup (only matters at transcription time)

## H10 [LOW] Skip _setup_provider entirely in bench mode
In --bench mode, skip LLM provider setup since it's not needed.
Expected gain: 5-10ms

## Completed
(none yet)

## Discarded
(none yet)
