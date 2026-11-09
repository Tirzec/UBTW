// BTC Up/Down Signals dashboard - plain JS, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (x, d = 2) => (x == null ? "–" : "$" + Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (x, d = 1) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
const num = (x, d = 2) => (x == null ? "–" : Number(x).toFixed(d));
const signed = (x, d = 2) => (x == null ? "–" : (x >= 0 ? "+" : "") + Number(x).toFixed(d));
const mmss = (s) => { if (s == null) return "–"; s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

let state = null;
let lastAlertId = null;
let alertsOn = false;
let audioCtx = null;

// ------------------------------------------------------------------ alerts
$("enable-alerts").onclick = async () => {
  try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); await audioCtx.resume(); } catch (e) {}
  if ("Notification" in window && Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  alertsOn = true;
  $("enable-alerts").textContent = "Alerts on";
  beep(880, 0.12);
};

function beep(freq = 880, dur = 0.2, times = 1) {
  if (!audioCtx) return;
  for (let i = 0; i < times; i++) {
    const t = audioCtx.currentTime + i * (dur + 0.08);
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = freq; o.type = "sine";
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.3, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g).connect(audioCtx.destination); o.start(t); o.stop(t + dur + 0.02);
  }
}

function notify(a) {
  if (!alertsOn) return;
  if (a.kind === "signal") beep(988, 0.18, 3);
  else if (a.kind === "risk") beep(300, 0.4, 3);
  else if (a.kind === "trade") beep(740, 0.12, 1);
  else if (a.kind === "exit") beep(a.level === "signal" ? 660 : 520, 0.25, 2);
  else if (a.kind === "degraded") beep(330, 0.35, 1);
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification(a.title, { body: a.body, tag: "btc-" + a.id }); } catch (e) {}
  }
}

function handleAlerts(alerts) {
  if (!alerts || !alerts.length) return;
  const maxId = Math.max(...alerts.map((a) => a.id));
  if (lastAlertId === null) { lastAlertId = maxId; return; } // don't replay history on load
  alerts.filter((a) => a.id > lastAlertId).forEach(notify);
  lastAlertId = Math.max(lastAlertId, maxId);
}

// ------------------------------------------------------------------ render
function renderTop(s) {
  const r = s.reference || {};
  $("ref-spot").textContent = usd(r.spot);
  $("ref-twap").textContent = usd(r.twap);
  const src = $("ref-src");
  src.textContent = r.synthetic ? "ESTIMATE" : (r.source || "–");
  src.className = "tag " + (r.synthetic ? "bad" : "ok");
  const sp = s.spot || {};
  $("spot").textContent = Object.entries(sp).map(([k, v]) => `${k} ${usd(v)}`).join(" · ") || "–";
  const q = s.quality || {};
  $("gap").textContent = q.gap_bps == null ? "–" : `${signed(q.gap_bps, 1)} bps`;

  const bd = $("banner-degraded");
  if (q.degraded) { bd.textContent = "⚠ DEGRADED DATA: NO TRADE. " + (q.reasons || []).join(" · "); bd.classList.remove("hidden"); }
  else bd.classList.add("hidden");

  const bad = Object.values(s.windows || {}).filter((w) => w.window.rules_ok === false);
  const br = $("banner-rules");
  if (bad.length) {
    br.innerHTML = "⚠ RULES MISMATCH: NO TRADE. " + bad.map((w) => `${esc(w.window.duration)}: ${esc(w.window.rules_mismatches.join("; "))}`).join(" | ");
    br.classList.remove("hidden");
  } else br.classList.add("hidden");

  const act = (s.calendar || {}).active || [];
  const bb = $("banner-blackout");
  if (act.length) { bb.textContent = "Economic release blackout: " + act.map((e) => `${e.title} (${e.time})`).join(", "); bb.classList.remove("hidden"); }
  else bb.classList.add("hidden");
}

function renderWindow(dur, w) {
  const el = $("w-" + dur);
  if (!w) { el.innerHTML = `<h2>${dur}</h2><p class="muted">waiting…</p>`; return; }
  const win = w.window, d = w.decision || {}, m = w.market || {};
  const action = d.action || "NO TRADE";
  const cls = action === "UP" ? "UP" : action === "DOWN" ? "DOWN" : "NO";
  const best = d.best_side && d.quotes ? d.quotes[d.best_side] : null;
  const pUp = w.p_up;
  const mktUp = m.up_mid;
  const sub = action !== "NO TRADE" && best
    ? `BUY ${action} at ≤ ${num(best.best_ask)} · model ${pct(best.p_model)} · edge ${signed(d.edge * 100, 1)}%`
    : (best && best.edge != null ? `best: ${d.best_side} edge ${signed(best.edge * 100, 1)}% after fees + buffer` : "");
  const ptbNote = win.ptb_source ? `<span class="tag ${win.ptb_source === "synthetic" ? "bad" : ""}">${esc(win.ptb_source)}</span>` : "";
  const check = win.ptb_check_bps != null ? ` <span class="muted">Δ vs Gamma ${signed(win.ptb_check_bps, 2)} bps</span>` : "";
  const site = win.site_open_spot != null && win.price_to_beat != null
    ? `<div class="muted" title="polymarket.com's price API shows the Chainlink SPOT at the window start. Markets resolve on the 60s TWAP shown here.">site spot open ${usd(win.site_open_spot)} (${signed((win.site_open_spot / win.price_to_beat - 1) * 1e4, 1)} bps), not the resolution price</div>` : "";
  el.innerHTML = `
    <div class="head">
      <div><div class="title">${esc(dur)} · ${esc(win.title || win.slug)}</div>
        <a class="muted" href="${esc(win.url)}" target="_blank" rel="noopener">${esc(win.slug)}</a>
        <span class="tag">TWAP ${esc(win.twap_lookback_s)}s</span></div>
      <div class="countdown">${mmss(win.seconds_left)}</div>
    </div>
    <div class="signal ${cls}">${esc(action)}</div>
    <div class="signal-sub">${esc(sub)}</div>
    <div class="kv">
      <div><span class="label">Price to beat (TWAP)</span><b>${usd(win.price_to_beat)}</b>${ptbNote}${check}${site}</div>
      <div><span class="label">Distance</span><b>${w.d_sigma == null ? "–" : signed(w.d_sigma, 2) + "σ"}</b></div>
      <div><span class="label">Model P(UP)</span><b>${pct(pUp)}</b><span class="muted">prior ${pct(w.p_prior)}</span></div>
      <div><span class="label">Market P(UP)</span><b>${pct(mktUp)}</b><span class="muted">UP ${num(m.up_bid)}/${num(m.up_ask)} · DN ${num(m.down_bid)}/${num(m.down_ask)}</span></div>
    </div>
    <div class="bar" title="model P(UP)"><i style="width:${pUp == null ? 50 : (pUp * 100).toFixed(1)}%"></i></div>
    <h3>Why</h3>
    <ol class="why">${(w.drivers || []).map((x) => `<li class="${x.supports}">${esc(x.text)} <span class="muted">(${x.supports} ${esc(d.best_side || "")}, ${signed(x.contribution, 2)})</span></li>`).join("") || '<li class="muted">no estimate yet</li>'}</ol>
    ${(d.blocked_by || []).length ? `<ul class="blocked">${d.blocked_by.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
    <div class="muted" style="margin-top:6px">model: ${esc((w.model || {}).kind || "?")}${(w.model || {}).n ? ` (n=${w.model.n})` : ""}</div>`;
}

function adviceCell(a) {
  if (!a || !a.action) return '<span class="muted">…</span>';
  return `<span class="advice-${esc(a.action)}">${esc(a.action)}</span> <span class="muted">${esc(a.reason || "")}</span>`;
}

function renderPositions(s) {
  const rows = (s.positions || []).map((p) => {
    const a = p.advice || {};
    return `<tr><td>#${p.id} ${esc(p.duration)} <b>${esc(p.side)}</b></td><td class="num">${num(p.entry_price)} × ${num(p.shares, 1)}</td>
      <td class="num">${a.value_now == null ? "–" : usd(a.value_now)}<br><span class="muted">${a.pnl_now == null ? "" : signed(a.pnl_now)}</span></td>
      <td class="num">${a.fair == null ? "–" : num(a.fair)}</td><td>${adviceCell(a)}</td>
      <td><button data-close="${p.id}">close</button></td></tr>`;
  }).join("");
  $("positions").innerHTML = rows
    ? `<div class="table-wrap"><table><tr><th>Position</th><th class="num">Entry × shares</th><th class="num">Value now</th><th class="num">Fair</th><th>Advice</th><th></th></tr>${rows}</table></div>`
    : '<p class="muted">Add a position you opened manually to get SELL/HOLD guidance every 2s.</p>';
  document.querySelectorAll("[data-close]").forEach((b) => b.onclick = async () => {
    await fetch(`/api/positions/${b.dataset.close}/close`, { method: "POST" });
  });
  const po = (s.paper_open || []).map((t) => `<tr><td>#${t.id} ${esc(t.duration)} <b>${esc(t.side)}</b></td>
      <td class="num">${num(t.entry_price)} × ${num(t.shares, 1)}</td><td>${esc(t.status)}${t.exit_reason ? `<br><span class="muted">${esc(t.exit_reason)}</span>` : ""}</td>
      <td>${adviceCell(t.advice)}</td></tr>`).join("");
  $("paper-open").innerHTML = po ? `<div class="table-wrap"><table>${po}</table></div>` : '<p class="muted">none</p>';
}

function renderStats(st) {
  const r = (st || {}).rule || {}, h = (st || {}).hold || {};
  const cell = (label, v) => `<div><span class="label">${label}</span><b>${v}</b></div>`;
  $("stats").innerHTML = `<div class="stats">
    ${cell("Trades", r.n || 0)}${cell("Win rate", pct(r.win_rate))}${cell("Avg edge", r.avg_edge == null ? "–" : signed(r.avg_edge * 100, 1) + "%")}${cell("EV / trade", r.ev_per_trade == null ? "–" : signed(r.ev_per_trade))}
    ${cell("Total PnL", r.total_pnl == null ? "–" : signed(r.total_pnl))}${cell("Max drawdown", r.max_drawdown == null ? "–" : usd(r.max_drawdown))}${cell("Hold-only PnL", h.total_pnl == null ? "–" : signed(h.total_pnl))}${cell("Windows logged", st.windows_observed ?? 0)}
  </div><p class="muted">Brier score (model, all logged predictions): ${st.brier_model == null ? "–" : num(st.brier_model, 4)} · open paper trades: ${st.open ?? 0}. Check at least 300 windows before trading real money.</p>`;
  $("calib").innerHTML = calibrationSVG(st.calibration_model || []);
  $("equity").innerHTML = equitySVG(st.equity || []);
}

function calibrationSVG(bins) {
  const W = 260, H = 180, P = 26;
  const x = (v) => P + v * (W - P - 8), y = (v) => H - P - v * (H - P - 8);
  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="calibration chart">`;
  s += `<line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}" stroke="#444" stroke-dasharray="3 3"/>`;
  s += `<line x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(0)}" stroke="#555"/><line x1="${x(0)}" y1="${y(0)}" x2="${x(0)}" y2="${y(1)}" stroke="#555"/>`;
  [0, 0.5, 1].forEach((t) => { s += `<text x="${x(t) - 6}" y="${H - 8}">${t}</text><text x="2" y="${y(t) + 3}">${t}</text>`; });
  const maxN = Math.max(1, ...bins.map((b) => b.n));
  bins.forEach((b) => { s += `<circle cx="${x(b.predicted)}" cy="${y(b.actual)}" r="${3 + 6 * Math.sqrt(b.n / maxN)}" fill="#58a6ff" fill-opacity=".75"><title>${b.bin}: predicted ${pct(b.predicted)}, actual ${pct(b.actual)}, n=${b.n}</title></circle>`; });
  if (!bins.length) s += `<text x="${W / 2 - 40}" y="${H / 2}">no settled data yet</text>`;
  return s + "</svg>";
}

function equitySVG(eq) {
  const W = 260, H = 180, P = 8;
  if (eq.length < 2) return `<svg viewBox="0 0 ${W} ${H}" width="100%"><text x="${W / 2 - 40}" y="${H / 2}">no settled trades yet</text></svg>`;
  const lo = Math.min(0, ...eq), hi = Math.max(0, ...eq), span = hi - lo || 1;
  const x = (i) => P + (i / (eq.length - 1)) * (W - 2 * P), y = (v) => H - P - ((v - lo) / span) * (H - 2 * P);
  const pts = eq.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = eq[eq.length - 1];
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="equity curve">
    <line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="#444" stroke-dasharray="3 3"/>
    <polyline points="${pts}" fill="none" stroke="${last >= 0 ? "#2ea043" : "#e5534b"}" stroke-width="2"/>
    <text x="${P}" y="12">${signed(hi)}</text><text x="${P}" y="${H - 2}">${signed(lo)}</text></svg>`;
}

function renderFeeds(s) {
  const rows = (s.feeds || []).map((f) => `<tr><td>${esc(f.name)} <span class="muted">${esc(f.role)}</span></td>
    <td>${f.stale ? `<span class="tag bad">STALE</span> <span class="muted">${esc(f.stale_reason)}</span>` : '<span class="tag ok">LIVE</span>'}</td>
    <td class="num">${f.age_s == null ? "–" : f.age_s + "s"}</td><td class="num">${f.last_price ? usd(f.last_price) : "–"}</td></tr>`).join("");
  const c = s.clob || {};
  $("feeds").innerHTML = `<div class="table-wrap"><table><tr><th>Feed</th><th>Status</th><th class="num">Age</th><th class="num">Last</th></tr>${rows}
    <tr><td>polymarket clob</td><td>${c.connected ? '<span class="tag ok">LIVE</span>' : '<span class="tag bad">DOWN</span>'} <span class="muted">${c.reconnects ? c.reconnects + " reconnects; last: " + esc(c.error || "") : ""}</span></td><td class="num">${c.last_msg_age == null ? "–" : c.last_msg_age + "s"}</td><td></td></tr>
    </table></div><p class="muted">event-loop lag ${num(s.loop_lag_ms, 0)} ms · phone alerts: ${(s.notifications || {}).enabled ? esc(s.notifications.channels.join(", ")) + ` (${s.notifications.sent} sent)` : "off (set NTFY_TOPIC in .env)"}${(s.notifications || {}).error ? ` <span class="blocked">${esc(s.notifications.error)}</span>` : ""}</p>${s.discovery_error ? `<p class="blocked">discovery: ${esc(s.discovery_error)}</p>` : ""}`;
}

function renderCalendar(s) {
  const c = s.calendar || {};
  const up = (c.upcoming || []).map((e) => `<li>${esc(e.time)}: <b>${esc(e.title)}</b> <span class="muted">${esc(e.country)} ${esc(e.impact)}</span></li>`).join("");
  $("calendar").innerHTML = `<h3>Upcoming releases (NO TRADE around these)</h3><ul>${up || '<li class="muted">none this week</li>'}</ul>${c.error ? `<p class="blocked">${esc(c.error)}</p>` : ""}`;
  const d = s.derivs || {};
  $("derivs").innerHTML = `<h3>Derivatives (${esc(d.provider || "?")})</h3>` + (d.ok
    ? `<table><tr><td>Funding</td><td class="num">${signed(d.funding_bps_hr, 3)} bps/hr</td></tr>
       <tr><td>Open interest</td><td class="num">${num(d.open_interest, 0)} BTC (${signed(d.oi_change_pct_5m, 2)}% 5m)</td></tr>
       <tr><td>Perp book imbalance</td><td class="num">${signed(d.book_imb, 2)}</td></tr>
       <tr><td>Liquidations</td><td class="num">${d.liquidations_available ? signed(d.liq_imbalance, 2) : '<span class="muted">n/a from provider</span>'}</td></tr></table>`
    : `<p class="muted">unavailable ${esc(d.error || "")}</p>`);
}

function renderNews(s) {
  $("news-score").textContent = "score " + signed(s.news_score, 2);
  $("news").innerHTML = (s.news || []).map((h) => `<div class="news-item"><span class="lbl-${esc(h.label)}">${esc(h.label)} ${pct(h.confidence, 0)}</span>
    <a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.title)}</a>
    <div class="muted">${esc(h.source)} · ${new Date(h.published * 1000).toLocaleTimeString()}${h.hits && h.hits.length ? " · " + esc(h.hits.join(", ")) : ""}</div></div>`).join("") || '<p class="muted">no headlines yet</p>';
}

function renderAlerts(s) {
  $("alerts").innerHTML = (s.alerts || []).slice().reverse().map((a) => `<div class="alert lvl-${esc(a.level)}">
    <span class="muted">${new Date(a.ts * 1000).toLocaleTimeString()}</span> <b>${esc(a.title)}</b> ${esc(a.body)}</div>`).join("") || '<p class="muted">no alerts yet</p>';
}

function renderBot(b) {
  const badge = $("bot-mode");
  const mode = (b && b.mode) || "off";
  const venue = state && state.venue === "polymarket_us" ? "Polymarket US" : "Polymarket (intl)";
  $("brand-sub").textContent = venue + " · " + (mode === "live" ? "LIVE: places real orders" : mode === "paper" ? "paper trading: no real orders" : "signals only: never trades");
  badge.textContent = mode === "off" ? "TRADER OFF" : mode.toUpperCase();
  badge.title = state && state.venue === "polymarket_us" ? "Polymarket US (CFTC-regulated)" : "polymarket.com";
  badge.className = "mode-badge " + mode;
  if (mode === "off") { $("bot-status-line").textContent = "auto-trader disabled in config.yaml"; return; }
  const r = b.risk || {};
  const line = $("bot-status-line");
  if (r.halted) { line.textContent = "HALTED: " + r.halt_reason; line.className = "bot-state halted"; }
  else if (r.day_stopped) { line.textContent = "Stopped for today: daily loss limit hit"; line.className = "bot-state paused"; }
  else { line.textContent = mode === "live" ? "Trading with REAL money" : "Paper trading (simulated fills on the real order book)"; line.className = "bot-state running"; }
  $("bot-resume").classList.toggle("hidden", !(r.halted || r.day_stopped));
  $("bot-resume").disabled = !r.halted;
  $("bot-note").textContent = (b.status_note || "") + (b.balance != null ? `  ·  balance ${usd(b.balance)}` : "");
  const t = b.stats_today || {}, a = b.stats_all || {};
  const cell = (label, v, cls = "") => `<div><span class="label">${label}</span><b class="${cls}">${v}</b></div>`;
  const pn = (x) => (x == null ? "–" : signed(x));
  const cls = (x) => (x == null ? "" : x >= 0 ? "pos" : "neg");
  $("bot-risk").innerHTML =
    cell("Today's P&L", pn(r.day_pnl), cls(r.day_pnl)) +
    cell("To daily limit", usd(r.distance_to_daily_limit) + ` <span class="muted">of ${usd(r.daily_loss_limit, 0)}</span>`) +
    cell("Today win rate", t.n ? `${pct(t.win_rate)} <span class="muted">(${t.n})</span>` : "–") +
    cell("Losses in a row", `${r.consecutive_losses ?? 0} / ${r.max_consecutive_losses ?? "–"}`) +
    cell("Avg win / avg loss", a.n ? `${pn(a.avg_win)} / ${pn(a.avg_loss)}` : "–") +
    cell("All-time P&L", a.n ? pn(a.total_pnl) : "–", cls(a.total_pnl)) +
    cell("All-time win rate", a.n ? `${pct(a.win_rate)} <span class="muted">(${a.n})</span>` : "–") +
    cell("Fees paid", a.n ? usd(a.fees_paid) : "–");
  const ps = (b.positions || []).map((p) => {
    const held = (p.shares || 0) - (p.exit_shares || 0);
    const pnlPct = p.last_pnl != null && p.cost ? p.last_pnl / (p.cost + p.entry_fee) : null;
    return `<tr><td>#${p.id} ${esc(p.duration)} <b>${esc(p.side)}</b><br><span class="muted">${esc(p.status)} · ${mmss(p.seconds_left)} left</span></td>
      <td class="num">${p.status === "opening" ? `bid ${num(p.entry_price)}` : num(p.entry_price, 3)} × ${num(held || 0, 2)}<br><span class="muted">cost ${usd((p.cost || 0) + (p.entry_fee || 0))}</span></td>
      <td class="num">${p.last_value == null ? "–" : usd(p.last_value)}<br><span class="${cls(p.last_pnl)}">${pn(p.last_pnl)} ${pnlPct == null ? "" : "(" + signed(pnlPct * 100, 1) + "%)"}</span></td>
      <td class="num trigger">TP bid ≥ ${num(p.tp_price)}<br>SL bid ≤ ${num(p.sl_price)}</td>
      <td class="muted">${esc(p.last_note || p.exit_reason || "")}</td></tr>`;
  }).join("");
  $("bot-positions").innerHTML = ps ? `<div class="table-wrap"><table><tr><th>Position</th><th class="num">Entry × shares</th><th class="num">Sellable now (P&L)</th><th class="num">Triggers</th><th>Status</th></tr>${ps}</table></div>` : '<p class="muted">none</p>';
  const tr = (b.trades || []).slice(0, 25).map((p) => `<tr><td>${new Date(p.closed_ts * 1000).toLocaleTimeString()}</td>
      <td>${esc(p.duration)} <b>${esc(p.side)}</b></td><td class="num">${num(p.entry_price, 3)} → ${p.exit_price ? num(p.exit_price, 3) : (p.payout ? "1.00" : "0.00")}</td>
      <td class="num ${cls(p.pnl)}">${pn(p.pnl)}</td><td><span class="tag">${esc(p.exit_trigger || "resolution")}</span> <span class="muted">${esc(p.exit_reason || "")}</span></td></tr>`).join("");
  $("bot-trades").innerHTML = tr ? `<div class="table-wrap"><table><tr><th>Closed</th><th>Trade</th><th class="num">Entry → exit</th><th class="num">P&L</th><th>Why it exited</th></tr>${tr}</table></div>` : '<p class="muted">no closed trades yet</p>';
  const need = b.paper_trades_required || 300, done = b.paper_trades_done || 0;
  $("bot-paper").textContent = mode === "paper"
    ? `Paper trades completed: ${done} / ${need} required before live mode can be enabled. Entries are post-only limit orders (0 fee); exits sell at the bid (taker fee). All P&L includes fees.`
    : `LIVE · stake ${usd((b.settings || {}).stake)} · TP +${pct((b.settings || {}).take_profit_pct, 0)} / SL -${pct((b.settings || {}).stop_loss_pct, 0)}`;
}

$("bot-stop").onclick = async () => {
  if (!confirm("STOP: cancel all open orders and halt the auto-trader?")) return;
  const r = await fetch("/api/bot/stop", { method: "POST" });
  if (!r.ok) alert("STOP failed: " + (await r.text()) + "\nUse: python -m app.trader stop");
};
$("bot-resume").onclick = async () => {
  if (!confirm("Resume automated trading?")) return;
  await fetch("/api/bot/resume", { method: "POST" });
};

// ------------------------------------------------------------------ tabs
let activeTab = (() => { try { return localStorage.getItem("tab") || "crypto"; } catch (e) { return "crypto"; } })();
function showTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  ["crypto", "sports", "combined"].forEach((t) => $("tab-" + t).classList.toggle("hidden", t !== name));
  try { localStorage.setItem("tab", name); } catch (e) {}
}
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
showTab(activeTab);

const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "–";

function statsTable(groups) {
  const rows = Object.entries(groups || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v.n || 0}</td>
    <td class="num">${v.n ? pct(v.win_rate) : "–"}</td><td class="num">${v.n ? signed(v.total_pnl) : "–"}</td>
    <td class="num">${v.n ? signed(v.return_on_stake * 100, 1) + "%" : "–"}</td>
    <td class="num">${v.avg_clv == null ? "–" : signed(v.avg_clv * 100, 1) + "¢ (" + v.clv_n + ")"}</td></tr>`).join("");
  return rows ? `<div class="table-wrap"><table><tr><th></th><th class="num">Trades</th><th class="num">Win</th><th class="num">P&L</th><th class="num">ROI</th><th class="num">Avg CLV</th></tr>${rows}</table></div>`
    : '<p class="muted">no settled trades yet</p>';
}

function renderSports(m) {
  if (!m) { $("sp-status").textContent = "Sports module off (sports.enabled in config.yaml; needs venue: polymarket_us)."; return; }
  const st = m.module_state || {}, r = m.risk || {}, a = m.stats_all || {}, t = m.stats_today || {};
  $("sp-mode").textContent = (m.mode || "").toUpperCase();
  $("sp-status").textContent = `${st.games || 0} upcoming games · leagues ${(st.leagues || []).join(", ").toUpperCase()}` +
    (r.halted ? ` · HALTED: ${r.halt_reason}` : "") + (m.status_note ? ` · ${m.status_note}` : "");
  const cell = (l, v, c = "") => `<div><span class="label">${l}</span><b class="${c}">${v}</b></div>`;
  const cls = (x) => (x == null ? "" : x >= 0 ? "pos" : "neg");
  $("sp-risk").innerHTML = cell("Today's P&L", signed(r.day_pnl || 0), cls(r.day_pnl)) +
    cell("To daily limit", usd(r.distance_to_daily_limit)) + cell("Today win rate", t.n ? pct(t.win_rate) : "–") +
    cell("Avg win / loss", a.n ? `${signed(a.avg_win)} / ${signed(a.avg_loss)}` : "–");
  $("sp-positions").innerHTML = (m.positions || []).length ? `<div class="table-wrap"><table><tr><th>Position</th><th class="num">Entry × shares</th><th class="num">Sellable now</th><th>Status</th></tr>` +
    m.positions.map((p) => `<tr><td>#${p.id} ${esc(p.duration)} <b>${esc(p.side)}</b><br><span class="muted">${esc(p.bet_type || "")} · ${esc(p.status)} · start ${fmtTime(p.starts_at)}</span></td>
      <td class="num">${num(p.entry_price, 3)} × ${num((p.shares || 0) - (p.exit_shares || 0), 2)}</td>
      <td class="num">${p.last_value == null ? "–" : usd(p.last_value)}<br><span class="${cls(p.last_pnl)}">${p.last_pnl == null ? "" : signed(p.last_pnl)}</span></td>
      <td class="muted">${esc(p.last_note || p.exit_reason || "")}</td></tr>`).join("") + "</table></div>" : '<p class="muted">none</p>';
  $("sp-trades").innerHTML = (m.trades || []).length ? `<div class="table-wrap"><table><tr><th>Closed</th><th>Bet</th><th class="num">Entry → exit</th><th class="num">P&L</th><th class="num">CLV</th><th>Why</th></tr>` +
    m.trades.slice(0, 25).map((p) => `<tr><td>${fmtTime(p.closed_ts)}</td><td>${esc(p.duration)} <b>${esc(p.side)}</b> <span class="muted">${esc(p.bet_type || "")}</span></td>
      <td class="num">${num(p.entry_price, 3)} → ${p.exit_price ? num(p.exit_price, 3) : num(p.payout / Math.max(p.shares, 1e-9), 2)}</td>
      <td class="num ${cls(p.pnl)}">${signed(p.pnl)}</td><td class="num ${cls(p.clv)}">${p.clv == null ? "–" : signed(p.clv * 100, 1) + "¢"}</td>
      <td><span class="tag">${esc(p.exit_trigger || "resolution")}</span> <span class="muted">${esc(p.exit_reason || "")}</span></td></tr>`).join("") + "</table></div>" : '<p class="muted">no closed sports trades yet</p>';
  $("sp-paper").textContent = `Paper sports trades: ${m.paper_trades_done || 0} / ${m.paper_trades_required || 200} required before live sports trading.`;
  const src = st.sources || {};
  $("sp-sources").innerHTML = "Odds sources: " + (Object.entries(src).map(([k, v]) => {
    const b = v.budget || {};
    return `${esc(k)} ${v.available ? '<span class="tag ok">on</span>' : '<span class="tag">no key</span>'}` +
      (v.available ? ` <span class="muted">today ${b.day_used || 0}${b.limits && b.limits.day ? "/" + b.limits.day : ""}, month ${b.month_used || 0}${b.limits && b.limits.month ? "/" + b.limits.month : ""}${b.provider_remaining != null ? ", provider says " + b.provider_remaining + " left" : ""}</span>` : "") +
      (v.error ? ` <span class="blocked">${esc(v.error)}</span>` : "");
  }).join(" · ") || "none configured");
  const rows = (st.board || []).map((r) => {
    const b = r.best || {};
    const isSig = !(r.blocked_by || []).length;
    const reasons = (r.reasons || []).map((x) => `<li>${esc(x.text)} <span class="muted">${x.source && x.source.startsWith("http") ? `<a href="${esc(x.source)}" target="_blank" rel="noopener">source</a>` : esc(x.source || "")}${x.ts ? " · " + fmtTime(x.ts) : ""}</span></li>`).join("");
    return `<tr class="${isSig ? "signal-row" : ""}"><td>${esc((r.league || "").toUpperCase())}<br><span class="muted">${fmtTime(r.starts_at)}</span></td>
      <td>${esc(r.game)}<br><span class="muted">${esc(r.bet_type)}${r.line != null ? " " + r.line : ""}</span></td>
      <td>${esc(b.label || "")}</td><td class="num">${b.bid != null ? num(b.bid) : "–"} / ${b.ask != null ? num(b.ask) : "–"}</td>
      <td class="num">${b.fair != null ? pct(b.fair) : "–"}<br><span class="muted">${r.n_books ? r.n_books + " books" : ""}</span></td>
      <td class="num ${b.edge > 0 ? "edge-pos" : "edge-neg"}">${b.edge != null ? signed(b.edge * 100, 1) + "%" : "–"}</td>
      <td class="num">${r.confidence ? num(r.confidence, 2) : "–"}</td>
      <td>${isSig ? '<span class="tag ok">SIGNAL</span>' : `<span class="muted">${esc((r.blocked_by || [])[0] || "")}</span>`}
        ${(r.flags || []).length ? `<br><span class="muted">${esc(r.flags.join(" · "))}</span>` : ""}
        ${reasons ? `<details class="card-reasons"><summary>research card</summary><ul>${reasons}</ul></details>` : ""}</td></tr>`;
  }).join("");
  $("sp-board").innerHTML = rows ? `<div class="table-wrap board"><table><tr><th>League</th><th>Game / market</th><th>Best side</th><th class="num">Bid / ask</th><th class="num">Fair</th><th class="num">Edge</th><th class="num">Conf.</th><th>Status &amp; key factors</th></tr>${rows}</table></div>`
    : '<p class="muted">no upcoming games found yet</p>';
  $("sp-by-league").innerHTML = statsTable(m.by_league);
  $("sp-by-bet").innerHTML = statsTable(m.by_bet_type);
}

function renderCombined(mods) {
  const g = (mods || {})._global || {};
  const rows = Object.entries(mods || {}).filter(([k]) => k !== "_global").map(([k, m]) => {
    const a = m.stats_all || {}, r = m.risk || {};
    return `<tr><td><b>${esc(k)}</b> <span class="tag">${esc((m.mode || "").toUpperCase())}</span></td><td class="num">${signed(r.day_pnl || 0)}</td>
      <td class="num">${a.n || 0}</td><td class="num">${a.n ? pct(a.win_rate) : "–"}</td><td class="num">${a.n ? signed(a.total_pnl) : "–"}</td>
      <td class="num">${a.n ? usd(a.max_drawdown) : "–"}</td><td>${r.halted ? '<span class="tag bad">HALTED</span> ' + esc(r.halt_reason) : (r.day_stopped ? "stopped for today" : "running")}</td></tr>`;
  }).join("");
  $("combined").innerHTML = `<div class="stats"><div><span class="label">Combined today</span><b>${signed(g.day_pnl || 0)}</b></div>
    <div><span class="label">Global daily limit</span><b>${usd(g.daily_loss_limit || 0, 0)}</b></div>
    <div><span class="label">Open exposure</span><b>${usd(g.exposure || 0)}</b></div>
    <div><span class="label">Global max exposure</span><b>${usd(g.max_exposure || 0, 0)}</b></div></div>
    <div class="table-wrap"><table><tr><th>Module</th><th class="num">Today</th><th class="num">Trades</th><th class="num">Win</th><th class="num">All-time P&L</th><th class="num">Max DD</th><th>Status</th></tr>${rows}</table></div>`;
}

function render(s) {
  state = s;
  renderSports((s.modules || {}).sports);
  renderCombined(s.modules);
  renderBot(s.bot);
  renderTop(s);
  renderWindow("5m", (s.windows || {})["5m"]);
  renderWindow("15m", (s.windows || {})["15m"]);
  renderPositions(s);
  renderStats(s.stats || {});
  renderFeeds(s);
  renderCalendar(s);
  renderNews(s);
  renderAlerts(s);
  handleAlerts(s.alerts);
}

// local 1s countdown between pushes
setInterval(() => {
  if (!state) return;
  for (const dur of ["5m", "15m"]) {
    const w = (state.windows || {})[dur];
    if (!w) continue;
    const el = document.querySelector(`#w-${dur} .countdown`);
    if (el) el.textContent = mmss(w.window.end - Date.now() / 1000);
  }
}, 250);

// ------------------------------------------------------------------ positions form
$("pos-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = { duration: f.get("duration"), side: f.get("side"), entry_price: +f.get("entry_price"), shares: +f.get("shares") };
  const r = await fetch("/api/positions", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) alert("Could not add position: " + (await r.text()));
  else e.target.reset();
};

// ------------------------------------------------------------------ connection
function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onopen = () => $("conn").classList.remove("bad");
  ws.onmessage = (ev) => { try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
  ws.onclose = () => { $("conn").classList.add("bad"); setTimeout(connect, 2000); };
}
connect();
