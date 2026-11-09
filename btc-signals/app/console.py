"""Console output that can't crash on Windows.

A Windows console or a redirected log file (start-bot.ps1 writes data\\console.log) may use a
legacy code page (cp1252/cp437) that can't encode characters like the sigma or <= signs in
signal explanations. Redirected output is written as UTF-8; an interactive console keeps its
encoding but replaces anything it can't show instead of raising UnicodeEncodeError."""
from __future__ import annotations

import sys


def safe_console() -> None:
    for s in (sys.stdout, sys.stderr):
        if s is None or not hasattr(s, "reconfigure"):
            continue
        try:
            if s.isatty():
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
