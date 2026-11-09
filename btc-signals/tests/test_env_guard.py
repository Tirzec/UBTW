"""The app refuses to start when .env (or another secrets file) is tracked by git."""
import shutil
import subprocess

import pytest

from app import env_guard

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git needed to build the test repo")


def _repo(tmp_path):
    app = tmp_path / "btc-signals"
    app.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in {"user.email": "t@example.com", "user.name": "t"}.items():
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)
    (app / ".env.example").write_text("POLYMARKET_KEY_ID=\n")
    (app / "config.yaml").write_text("x: 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return app


def test_untracked_env_is_fine(tmp_path):
    app = _repo(tmp_path)
    (app / ".env").write_text("POLYMARKET_SECRET_KEY=abc\n")       # exists but not added
    assert env_guard.check_env_safety(app) == []


@pytest.mark.parametrize("use_git", [True, False])
def test_tracked_env_refuses_to_start(tmp_path, monkeypatch, use_git):
    app = _repo(tmp_path)
    (app / ".env").write_text("POLYMARKET_SECRET_KEY=abc\n")
    (app / "key.pem").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "btc-signals/.env", "btc-signals/key.pem"], check=True)
    if not use_git:                                                  # git not installed: read .git/index
        monkeypatch.setattr(env_guard.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as e:
        env_guard.check_env_safety(app)
    msg = str(e.value)
    assert "REFUSING TO START" in msg and 'git rm --cached ".env"' in msg and "key.pem" in msg
    assert "abc" not in msg                                          # never echoes the secret


def test_notepad_txt_extension_warning(tmp_path):
    app = _repo(tmp_path)
    (app / ".env.txt").write_text("x")
    assert any("Rename-Item" in w for w in env_guard.check_env_safety(app))


def test_this_checkout_does_not_track_env():
    assert env_guard.tracked_secret_files() == []
