"""Clock check: SNTP parsing against a local fake server (no network), HTTP Date fallback."""
import socket
import struct
import threading
import time

from app import clock


def _fake_ntp(skew: float):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def serve():
        data, addr = s.recvfrom(48)
        now = time.time() + skew + clock.NTP_EPOCH
        ts = struct.pack("!II", int(now), int((now % 1) * 2**32))
        reply = bytes([0x24, 2, 0, 0]) + bytes(20) + data[40:48] + ts + ts
        s.sendto(reply, addr)
        s.close()
    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1]


def test_sntp_offset_measures_skew():
    port = _fake_ntp(skew=-3.0)                  # server 3 s behind -> PC is 3 s ahead
    off = clock.sntp_offset("127.0.0.1", timeout=2, port=port)
    assert abs(off - 3.0) < 0.05


def test_check_clock_status_bands(monkeypatch):
    monkeypatch.setattr(clock, "windows_time_status", lambda: {})
    for off, want in [(0.2, "PASS"), (-1.5, "WARN"), (5.0, "FAIL")]:
        monkeypatch.setattr(clock, "sntp_offset", lambda srv, t, o=off: o)
        r = clock.check_clock(ntp_servers=["x"])
        assert r.status == want and r.ok == (want != "FAIL")


def test_falls_back_to_http_when_udp_blocked(monkeypatch):
    def blocked(*a, **k):
        raise OSError("timed out")
    monkeypatch.setattr(clock, "sntp_offset", blocked)
    monkeypatch.setattr(clock, "http_offset", lambda url: (0.8, 0.6))
    monkeypatch.setattr(clock, "windows_time_status", lambda: {})
    r = clock.check_clock(ntp_servers=["a"], http_urls=["https://x"])
    assert r.status == "PASS" and r.source.startswith("http")      # 0.8 within the 1-s header band
    monkeypatch.setattr(clock, "http_offset", lambda url: blocked())
    assert clock.check_clock(ntp_servers=["a"], http_urls=["https://x"]).status == "FAIL"
