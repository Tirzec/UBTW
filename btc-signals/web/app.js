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

function render(s) {
  state = s;
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
