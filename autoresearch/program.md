# Autoresearch Operating Manual

## Purpose
Optimize wispr.py startup time — from `python wispr.py` to interactive (tray icon visible + keyboard hook active + audio stream running). Whisper model loading is backgrounded and NOT counted.

## Metric
`--bench` flag: start app, write timing to temp file, exit after 3 seconds.
Measurement command (PowerShell):
```
$start = Get-Date; E:\Programming\wispr\.venv312\Scripts\python.exe E:\Programming\wispr\wispr.py --bench; $elapsed = (Get-Date) - $start; Write-Output "Startup: $($elapsed.TotalMilliseconds)ms"
```
Primary metric: wall-clock ms from process start to interactive.

## Eval Framework
See eval_framework.md

## Hypothesis Bank
See hypothesis_bank.md

## Results
See results.tsv (not committed)
