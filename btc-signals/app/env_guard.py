"""Startup check: refuse to run if a secrets file is tracked by git.

`.env` holds the Polymarket US API secret. If git tracks it, the next `git commit` / `git push`
would publish the key. The app refuses to start until it is untracked:

    git rm --cached .env          (keeps your local file; only stops git tracking it)
    git commit -m "Stop tracking .env"

If git isn't installed, the repository's index file is read directly, so the check still works.
Also warns about Windows' classic mistake: Notepad saving the file as `.env.txt`.
"""
from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

from .config import ROOT

# tracked files matching these (relative to the app folder) stop the app
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key")
ALLOWED = (".env.example",)


class SecretsTracked(SystemExit):
    pass


def _repo_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return None


def _tracked_paths(repo: Path) -> list[str] | None:
    """All tracked paths (repo-relative, forward slashes); None if they can't be read."""
    if shutil.which("git"):
        try:
            r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
        except (OSError, subprocess.SubprocessError):
            pass
    return _paths_from_index(repo / ".git" / "index")


def _paths_from_index(index: Path) -> list[str] | None:
    """Minimal reader for git's index file (versions 2/3) so the check works without git."""
    import struct

    try:
        data = index.read_bytes()
    except OSError:
        return None
    if data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        return None
    pos, out = 12, []
    for _ in range(count):
        flags = struct.unpack(">H", data[pos + 60:pos + 62])[0]
        extended = version == 3 and flags & 0x4000
        name_start = pos + 62 + (2 if extended else 0)
        end = data.index(b"\0", name_start)
        out.append(data[name_start:end].decode("utf-8", "replace"))
        entry_len = end - pos + 1
        pos += (entry_len + 7) // 8 * 8                 # entries are NUL-padded to 8 bytes
    return out


def tracked_secret_files(app_dir: Path = ROOT) -> list[str]:
    repo = _repo_root(app_dir.resolve())
    if repo is None:
        return []                                       # not a git checkout: nothing can be committed
    paths = _tracked_paths(repo)
    if paths is None:
        return []
    prefix = app_dir.resolve().relative_to(repo).as_posix()
    prefix = "" if prefix == "." else prefix + "/"
    bad = []
    for p in paths:
        if prefix and not p.startswith(prefix):
            continue
        name = p.rsplit("/", 1)[-1]
        if name in ALLOWED:
            continue
        if any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS):
            bad.append(p[len(prefix):])               # relative to the app folder (where you run commands)
    return bad


def check_env_safety(app_dir: Path = ROOT) -> list[str]:
    """Raise SecretsTracked if a secrets file is tracked; return warnings otherwise."""
    bad = tracked_secret_files(app_dir)
    if bad:
        files = "\n".join(f"    {p}" for p in bad)
        cmds = "\n".join(f"    git rm --cached \"{p}\"" for p in bad)
        raise SecretsTracked(
            "\nREFUSING TO START: secret files are tracked by git, so a commit/push would publish them:\n"
            f"{files}\n\nIn the btc-signals folder, untrack them (your local copies are kept), then commit:\n{cmds}\n"
            "    git commit -m \"Stop tracking secrets\"\n"
            "If one was ever pushed to GitHub, also delete that API key at polymarket.us/developer "
            "and create a new one.\n")
    warnings = []
    for wrong in (".env.txt", "env", "env.txt", ".env.env"):
        if (app_dir / wrong).exists() and not (app_dir / ".env").exists():
            warnings.append(f"found '{wrong}' but no '.env': Notepad probably added an extension. "
                            f"Rename it: Rename-Item {wrong} .env")
    return warnings
