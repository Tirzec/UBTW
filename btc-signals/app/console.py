"""Console output that can't crash on Windows.

A Windows console or a redirected log file (start-bot.ps1 writes data\\console.log) may use a
legacy code page (cp1252/cp437) that can't encode characters like the sigma or <= signs in
signal explanations. Redirected output is written as UTF-8; an interactive console keeps its
encoding but replaces anything it can't show instead of raising UnicodeEncodeError."""
from __future__ import annotations

import sys


def no_quick_edit() -> None:
    """Windows: turn off the console's QuickEdit mode for this window. With it on, one click inside the
    bot window starts a text selection ("Select" in the title) that PAUSES the whole bot until Esc."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)                      # console input
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return                                     # not a console (redirected / background)
        ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
        k32.SetConsoleMode(h, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except Exception:  # noqa: BLE001 - cosmetic; never block startup
        pass


def safe_console() -> None:
    no_quick_edit()
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
