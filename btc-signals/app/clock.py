"""Is this computer's clock right? Works the same on Windows, macOS and Linux.

The 5-minute markets open and close on exact boundaries and Polymarket US rejects signed
requests whose timestamp is more than 30 s off, so the clock matters.

Two independent measurements, standard library only:
  1. SNTP (UDP port 123) against time.windows.com / time.nist.gov / pool.ntp.org: about 10-50 ms.
  2. The HTTPS `Date` header from api.polymarket.us: the exchange's own clock, 1-second resolution,
     used when UDP 123 is blocked (some routers / networks block it).
On Windows it also reads `w32tm /query /status` (whether Windows Time is syncing, and from where).

Run:  python -m app.clock
"""
from __future__ import annotations

import email.utils
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field

NTP_SERVERS = ["time.windows.com", "time.nist.gov", "pool.ntp.org"]
HTTP_URLS = ["https://api.polymarket.us", "https://gateway.polymarket.us"]
NTP_EPOCH = 2208988800                    # seconds from 1900-01-01 to 1970-01-01
PASS_S = 1.0                              # |offset| this small is fine for 5-minute windows
WARN_S = 2.0                              # above this: FAIL


@dataclass
class ClockResult:
    ok: bool
    status: str                           # PASS | WARN | FAIL
    offset_s: float | None                # local clock minus true time (+ = PC is ahead)
    source: str
    detail: str
    samples: list = field(default_factory=list)
    windows_time: dict = field(default_factory=dict)


def sntp_offset(server: str, timeout: float = 2.0, port: int = 123) -> float:
    """Clock offset (local - server) in seconds from one SNTP exchange (RFC 4330)."""
    pkt = bytearray(48)
    pkt[0] = 0x23                                       # LI 0, version 4, mode 3 (client)
    t1 = time.time()
    pkt[40:48] = struct.pack("!II", int(t1 + NTP_EPOCH), int((t1 % 1) * 2**32))   # transmit time
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(bytes(pkt), (server, port))
        data, _ = s.recvfrom(512)
    t4 = time.time()
    if len(data) < 48:
        raise ValueError("short NTP reply")
    stratum = data[1]
    if stratum == 0:
        raise ValueError("NTP server sent a kiss-of-death reply")

    def ts(off: int) -> float:
        sec, fr = struct.unpack("!II", data[off:off + 8])
        return sec - NTP_EPOCH + fr / 2**32

    t2, t3 = ts(32), ts(40)                              # server receive / transmit
    return ((t1 - t2) + (t4 - t3)) / 2                   # local - server


def http_offset(url: str, timeout: float = 5.0) -> tuple[float, float]:
    """(offset, uncertainty) from the server's Date header (whole seconds)."""
    import httpx

    t1 = time.time()
    r = httpx.head(url, timeout=timeout, follow_redirects=False)
    t4 = time.time()
    d = r.headers.get("date")
    if not d:
        raise ValueError("no Date header")
    server = email.utils.parsedate_to_datetime(d).timestamp()
    # the header is truncated to the second: true server time is in [server, server + 1)
    mid = (t1 + t4) / 2
    return mid - (server + 0.5), 0.5 + (t4 - t1) / 2


def windows_time_status() -> dict:
    """Windows only: what `w32tm /query /status` says (empty dict elsewhere / if unavailable)."""
    if sys.platform != "win32":
        return {}
    try:
        r = subprocess.run(["w32tm", "/query", "/status"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    if r.returncode != 0:
        # 0x80070426 = "The service has not been started"
        return {"error": (r.stdout + r.stderr).strip()[:200] or f"w32tm exit {r.returncode}",
                "service_running": False}
    out = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    out["service_running"] = True
    return out


def check_clock(ntp_servers: list[str] | None = None, http_urls: list[str] | None = None,
                ntp_timeout: float = 2.0) -> ClockResult:
    samples = []
    for srv in ntp_servers if ntp_servers is not None else NTP_SERVERS:
        try:
            samples.append(("ntp:" + srv, sntp_offset(srv, ntp_timeout), 0.05))
            break                                         # one good NTP answer is enough
        except (OSError, ValueError) as e:
            samples.append(("ntp:" + srv, None, str(e)[:80]))
    good_ntp = [s for s in samples if s[1] is not None]
    if not good_ntp:
        for url in http_urls if http_urls is not None else HTTP_URLS:
            try:
                off, unc = http_offset(url)
                samples.append(("http:" + url, off, unc))
                break
            except Exception as e:  # noqa: BLE001
                samples.append(("http:" + url, None, str(e)[:80]))
    good = [s for s in samples if s[1] is not None]
    wt = windows_time_status()
    if not good:
        return ClockResult(False, "FAIL", None, "", "could not reach any time server "
                           "(NTP UDP 123 and HTTPS both failed)", samples, wt)
    src, off, unc = good[0]
    # with the 1-second HTTP Date header, only an offset clearly beyond the band counts
    err = max(0.0, abs(off) - (unc if isinstance(unc, float) and src.startswith("http") else 0.0))
    status = "PASS" if err <= PASS_S else ("WARN" if err <= WARN_S else "FAIL")
    ahead = "ahead" if off > 0 else "behind"
    detail = f"PC clock is {abs(off):.3f}s {ahead} ({src.split(':', 1)[1]}"
    detail += f", +/-{unc:.2f}s)" if src.startswith("http") else ")"
    return ClockResult(status != "FAIL", status, off, src, detail, samples, wt)


FIX_WINDOWS = """How to fix it on Windows:
  1. Settings > Time & language > Date & time: turn ON "Set time automatically"
     and "Set time zone automatically", then click "Sync now".
  2. If "Sync now" fails, open PowerShell as Administrator and run:
       Set-Service w32time -StartupType Automatic
       Start-Service w32time
       w32tm /resync
       w32tm /query /status
"""


def main() -> int:
    from .console import safe_console
    safe_console()
    r = check_clock()
    print(f"[{r.status}] clock: {r.detail}")
    for src, off, extra in r.samples:
        print(f"    {src:42s} " + (f"offset {off:+.3f}s" if off is not None else f"failed: {extra}"))
    if r.windows_time:
        wt = r.windows_time
        if wt.get("service_running"):
            print(f"    Windows Time: source {wt.get('Source', '?')}, last sync {wt.get('Last Successful Sync Time', '?')}")
        else:
            print(f"    Windows Time service is NOT running: {wt.get('error', '')}")
    if r.status != "PASS" or (r.windows_time and not r.windows_time.get("service_running", True)):
        print(FIX_WINDOWS if sys.platform == "win32" else
              "Turn on automatic time sync in your system settings (NTP).")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
