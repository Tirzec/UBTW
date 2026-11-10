"""A background (hidden-window) start must never sit waiting for the LIVE confirmation phrase."""
import pytest

import app.main as main_mod
import app.trading.live_gate as gate


def test_background_live_start_refuses_without_prompting(monkeypatch):
    monkeypatch.setattr(main_mod, "load_config", lambda: {"trading": {"enabled": True, "mode": "live"}})
    asked = []
    monkeypatch.setattr(gate, "check_live_gates", lambda *a, **k: asked.append(1) or {})
    with pytest.raises(SystemExit) as e:
        main_mod.main(["--background", "--no-browser", "--no-keep-awake"])
    assert "background" in str(e.value) and "-Visible" in str(e.value)
    assert not asked                    # never reached the typed confirmation
