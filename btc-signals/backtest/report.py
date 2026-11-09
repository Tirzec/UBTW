"""Backtest metrics + a self-contained HTML report (inline SVG, no dependencies)."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from app.paper import calibration, trade_stats

from .replay import BTResult, brier, log_loss


def summarize(res: BTResult, label: str = "") -> dict:
    preds = res.predictions
    pm = [(p["p_model"], p["y"]) for p in preds]
    mk = [(p["p_market"], p["y"]) for p in preds if p["p_market"] is not None]
    out = {
        "label": label,
        "windows_sampled": len({p["slug"] for p in preds}),
        "skipped": res.skipped,
        "trades_rule": trade_stats(res.trades, "pnl_rule"),
        "trades_hold": trade_stats(res.trades, "pnl_hold"),
        "by_duration": {d: trade_stats([t for t in res.trades if t["duration"] == d], "pnl_rule")
                        for d in ("5m", "15m")},
        "model_brier": brier(*zip(*pm)) if pm else None,
        "model_logloss": log_loss(*zip(*pm)) if pm else None,
        "market_brier": brier(*zip(*mk)) if mk else None,
        "market_logloss": log_loss(*zip(*mk)) if mk else None,
        "calibration_model": calibration(pm),
        "calibration_market": calibration(mk),
        "calibration_trades": calibration([(t["p_model"], t["won"]) for t in res.trades]),
        "exit_reasons": {},
    }
    for t in res.trades:
        k = t.get("exit_reason") or "held to resolution"
        out["exit_reasons"][k] = out["exit_reasons"].get(k, 0) + 1
    return out


def _calib_svg(series: list[tuple[str, list[dict], str]]) -> str:
    W, H, P = 360, 300, 36
    x = lambda v: P + v * (W - P - 10)  # noqa: E731
    y = lambda v: H - P - v * (H - P - 10)  # noqa: E731
    s = [f'<svg viewBox="0 0 {W} {H}" width="360" role="img" aria-label="calibration">',
         f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}" stroke="#999" stroke-dasharray="4 4"/>',
         f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(0)}" stroke="#666"/>',
         f'<line x1="{x(0)}" y1="{y(0)}" x2="{x(0)}" y2="{y(1)}" stroke="#666"/>']
    for t in (0, 0.25, 0.5, 0.75, 1):
        s.append(f'<text x="{x(t) - 8}" y="{H - 16}">{t}</text><text x="4" y="{y(t) + 4}">{t}</text>')
    s.append(f'<text x="{W / 2 - 40}" y="{H - 2}">predicted</text>')
    for i, (name, bins, color) in enumerate(series):
        pts = " ".join(f"{x(b['predicted']):.1f},{y(b['actual']):.1f}" for b in bins)
        if pts:
            s.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        for b in bins:
            s.append(f'<circle cx="{x(b["predicted"]):.1f}" cy="{y(b["actual"]):.1f}" r="3.5" fill="{color}">'
                     f'<title>{name} {b["bin"]}: pred {b["predicted"]:.3f} act {b["actual"]:.3f} n={b["n"]}</title></circle>')
        s.append(f'<rect x="{P + 8}" y="{14 + i * 16}" width="10" height="10" fill="{color}"/>'
                 f'<text x="{P + 22}" y="{23 + i * 16}">{html.escape(name)}</text>')
    s.append("</svg>")
    return "".join(s)


def _equity_svg(trades: list[dict], key: str) -> str:
    W, H, P = 520, 220, 30
    eq, c = [], 0.0
    for t in trades:
        c += t[key]
        eq.append(c)
    if len(eq) < 2:
        return "<p>not enough trades</p>"
    lo, hi = min(0, *eq), max(0, *eq)
    span = (hi - lo) or 1
    x = lambda i: P + i / (len(eq) - 1) * (W - P - 10)  # noqa: E731
    y = lambda v: H - P - (v - lo) / span * (H - P - 10)  # noqa: E731
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(eq))
    return (f'<svg viewBox="0 0 {W} {H}" width="520"><line x1="{P}" x2="{W - 10}" y1="{y(0):.1f}" '
            f'y2="{y(0):.1f}" stroke="#999" stroke-dasharray="4 4"/><polyline points="{pts}" fill="none" '
            f'stroke="{"#2a7" if eq[-1] >= 0 else "#d33"}" stroke-width="2"/><text x="2" y="14">{hi:+.2f}</text>'
            f'<text x="2" y="{H - P}">{lo:+.2f}</text><text x="{P}" y="{H - 8}">trade #</text></svg>')


def _table(d: dict) -> str:
    if not d or not d.get("n"):
        return "<p>no trades</p>"
    rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{v:.4f}</td></tr>" if isinstance(v, float)
                   else f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in d.items())
    return f"<table>{rows}</table>"


def write_report(summary: dict, res: BTResult, out_dir: str | Path, cfg: dict) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    (out / "trades.json").write_text(json.dumps(res.trades, indent=1, default=float), encoding="utf-8")
    trades = sorted(res.trades, key=lambda t: t["opened_ts"])
    mb = summary.get("model_brier")
    kb = summary.get("market_brier")
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Backtest report</title>
<style>body{{font:14px system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
table{{border-collapse:collapse;margin:8px 0}}th,td{{padding:3px 10px;border-bottom:1px solid #ddd;text-align:left}}
svg text{{font:10px monospace;fill:#555}}.grid{{display:flex;flex-wrap:wrap;gap:24px}}code{{background:#f4f4f4;padding:1px 4px}}</style></head><body>
<h1>Backtest report {html.escape(summary.get('label', ''))}</h1>
<p>Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. Windows sampled: {summary['windows_sampled']}.
Skipped: {html.escape(json.dumps(summary['skipped']))}.</p>
<p>Fill model: ask = last trade + {cfg.get('half_spread')} , latency {cfg.get('latency_s')}s, impact {cfg.get('impact_per_100usd')}/100 USD,
stake ${cfg.get('stake_usd')}, buffer {cfg.get('buffer')}, Polymarket taker fee curve on entry and exit.</p>
<h2>Probability quality</h2>
<p>Brier: model <b>{mb if mb is None else f'{mb:.4f}'}</b> vs market mid <b>{kb if kb is None else f'{kb:.4f}'}</b> (lower is better).
Log-loss: model {summary.get('model_logloss') or float('nan'):.4f} vs market {summary.get('market_logloss') or float('nan'):.4f}.</p>
<div class="grid"><div><h3>Calibration (all sampled decision points)</h3>{_calib_svg([('model', summary['calibration_model'], '#1f6feb'), ('market', summary['calibration_market'], '#d29922')])}</div>
<div><h3>Calibration (trades taken)</h3>{_calib_svg([('trades', summary['calibration_trades'], '#2a7')])}</div></div>
<h2>Trades: following exit rules</h2>{_table(summary['trades_rule'])}
<h3>Equity (exit rules)</h3>{_equity_svg(trades, 'pnl_rule')}
<h2>Trades: always hold to resolution</h2>{_table(summary['trades_hold'])}
<h3>Equity (hold)</h3>{_equity_svg(trades, 'pnl_hold')}
<h2>By duration (exit rules)</h2><div class="grid">{''.join(f'<div><h3>{d}</h3>{_table(v)}</div>' for d, v in summary['by_duration'].items())}</div>
<h2>Exit reasons</h2><table>{''.join(f'<tr><th>{html.escape(k)}</th><td>{v}</td></tr>' for k, v in summary['exit_reasons'].items()) or '<tr><td>no trades</td></tr>'}</table>
<p>Raw data: <code>summary.json</code>, <code>trades.json</code>.</p></body></html>"""
    p = out / "report.html"
    p.write_text(page, encoding="utf-8")
    return p
