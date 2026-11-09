"""Stop the computer from going to sleep on its own while the bot runs (all platforms).

  Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) - the documented way an
           app tells Windows "don't idle-sleep now". The display may still turn off (that's fine).
           Cleared automatically when the process exits.
  macOS:   runs `caffeinate -i -w <our pid>` (replaces starting the app with `caffeinate`).
  Linux:   systemd-inhibit if available, otherwise nothing.

This blocks IDLE sleep only. Closing a laptop lid, pressing the power button, or a forced
Windows Update restart still interrupt the bot: see README "Running on Windows".
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("app")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
_proc: subprocess.Popen | None = None


def enable() -> str:
    """Returns a short description of what was done (for the startup banner)."""
    global _proc
    try:
        if sys.platform == "win32":
            import ctypes

            prev = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            return "on (Windows will not idle-sleep while the bot runs)" if prev else "FAILED (SetThreadExecutionState)"
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            _proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            return "on (caffeinate)"
        if shutil.which("systemd-inhibit"):
            _proc = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--why=btc-signals bot",
                                      "--mode=block", "sleep", "infinity"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "on (systemd-inhibit)"
    except Exception as e:  # noqa: BLE001 - never stop the app over this
        log.warning("keep-awake failed: %s", e)
        return f"FAILED ({e})"
    return "not available on this system"


def disable() -> None:
    global _proc
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:  # noqa: BLE001
            pass
    if _proc is not None:
        _proc.terminate()
        _proc = None
