"""Wispr launcher exe.

Runs the existing wispr.py with the already-installed interpreter (no bundled
torch/models) and is the app's single manageable PID:

  * kill this exe  -> its stdin pipe to wispr closes -> wispr sees EOF and exits
  * quit wispr     -> wait() returns -> this exe exits

so your process-manager can treat Wispr.exe as the whole app.
"""
import os
import sys
import subprocess

CREATE_NO_WINDOW = 0x08000000

# Base dir = where this exe lives (next to wispr.py). Frozen -> sys.executable.
BASE = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))
if not os.path.exists(os.path.join(BASE, "wispr.py")):
    BASE = r"E:\Programming\wispr"  # fallback if exe is moved

# Interpreter: prefer a local .venv (matches run.bat), else the miniconda one.
VENV_PY = os.path.join(BASE, ".venv", "Scripts", "pythonw.exe")
MINICONDA_PY = r"E:\Apps\scoop\apps\miniconda3\current\pythonw.exe"
PYTHON = VENV_PY if os.path.exists(VENV_PY) else MINICONDA_PY

SCRIPT = os.path.join(BASE, "wispr.py")
LOG = os.path.join(BASE, "wispr.log")


def main():
    logf = open(LOG, "w", encoding="utf-8", errors="replace")
    if not os.path.exists(PYTHON):
        logf.write(f"Interpreter not found: {PYTHON}\n")
        logf.flush()
        sys.exit(1)

    env = dict(os.environ, WISPR_LAUNCHER="1")
    proc = subprocess.Popen(
        [PYTHON, "-u", SCRIPT],  # -u: unbuffered so wispr.log is live
        cwd=BASE,
        stdin=subprocess.PIPE,   # held open; closes when this exe dies -> wispr exits
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
        env=env,
    )
    try:
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()
