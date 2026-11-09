"""HTTP API smoke test (engine constructed but not started: no network)."""
from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app


def test_positions_api_and_state(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "t.db")
    with TestClient(create_app(cfg, start_engine=False)) as c:
        assert c.get("/").status_code == 200
        r = c.post("/api/positions", json={"duration": "5m", "side": "up",
                                           "entry_price": 0.55, "shares": 20})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        st = c.get("/api/state").json()
        assert st["positions"][0]["id"] == pid and st["positions"][0]["side"] == "UP"
        assert c.post("/api/positions", json={"duration": "1h", "side": "UP",
                                              "entry_price": 0.5, "shares": 1}).status_code == 422
        assert c.post(f"/api/positions/{pid}/close").json() == {"ok": True}
        assert c.get("/api/state").json()["positions"] == []
