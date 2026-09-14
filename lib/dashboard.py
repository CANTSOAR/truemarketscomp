"""
dashboard.py  —  live web dashboard for the AS market maker.

Shares state with maker.py via a module-level store.
Run alongside maker.py by importing it, or standalone for a demo.

Usage (from maker.py):
    import dashboard
    dashboard.start_background(port=8000)

Or standalone:
    python3 dashboard.py
"""

import asyncio
import threading
import time
from collections import deque
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ── shared state (written by maker.py, read by this server) ──────────────────

state: dict[str, Any] = {
    "mid":          None,
    "fair_value":   None,
    "bid_quote":    None,
    "ask_quote":    None,
    "spread":       None,
    "sigma":        None,
    "inventory_btc": 0.0,
    "bid_oid":      None,
    "ask_oid":      None,
    "bid_status":   None,
    "ask_status":   None,
    "cycle":        0,
    "uptime_start": time.time(),
    "last_update":  None,
    "balances":     {},
    "fill_history": deque(maxlen=50),   # list of fill dicts
    "cycle_history": deque(maxlen=200), # list of cycle snapshot dicts
    "errors":       deque(maxlen=20),
}


def record_cycle(snap: dict):
    state["cycle_history"].append({**snap, "ts": time.time()})
    state["last_update"] = time.time()
    state["cycle"] += 1


def record_fill(side: str, qty: float, price: float):
    state["fill_history"].appendleft({
        "ts": time.time(), "side": side, "qty": qty, "price": price,
        "notional": qty * price,
    })


def record_error(msg: str):
    state["errors"].appendleft({"ts": time.time(), "msg": msg})


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI()


@app.get("/api/state")
def api_state():
    s = dict(state)
    s["fill_history"]   = list(state["fill_history"])
    s["cycle_history"]  = list(state["cycle_history"])[-50:]
    s["errors"]         = list(state["errors"])
    s["uptime_secs"]    = int(time.time() - state["uptime_start"])
    return JSONResponse(s)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(HTML)


# ── background server launcher ────────────────────────────────────────────────

def start_background(port: int = 8000):
    """Start uvicorn in a daemon thread so maker.py's event loop is unaffected."""
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port,
                         log_level="warning", loop="none")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AS Market Maker — Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; padding: 20px; }
  .value, .order-row span, td { transition: color 0.25s ease; }
  .bar { transition: height 0.4s ease; }
  h1 { font-size: 18px; color: #58a6ff; margin-bottom: 16px; }
  h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card .label { color: #8b949e; font-size: 11px; margin-bottom: 4px; }
  .card .value { font-size: 20px; font-weight: bold; }
  .green { color: #3fb950; }
  .red   { color: #f85149; }
  .blue  { color: #58a6ff; }
  .dim   { color: #8b949e; }
  .yellow { color: #d29922; }
  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; }
  th { color: #8b949e; text-align: left; padding: 4px 8px; border-bottom: 1px solid #30363d; font-size: 11px; }
  td { padding: 4px 8px; border-bottom: 1px solid #21262d; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
  .badge-active { background: #1f6feb33; color: #58a6ff; }
  .badge-pending { background: #9e6a0333; color: #d29922; }
  .badge-complete { background: #1a7f3733; color: #3fb950; }
  .badge-none { background: #30363d; color: #8b949e; }
  .sparkline { display: flex; align-items: flex-end; gap: 2px; height: 40px; margin-top: 8px; }
  .bar { flex: 1; background: #58a6ff44; border-radius: 2px 2px 0 0; min-height: 2px; }
  .uptime { font-size: 11px; color: #8b949e; }
  .order-row { display: flex; justify-content: space-between; align-items: center; padding: 6px 0; border-bottom: 1px solid #21262d; }
</style>
</head>
<body>
<h1>⚡ Avellaneda-Stoikov Market Maker</h1>
<div class="grid">
  <div class="card"><div class="label">Mid Price</div><div class="value blue" id="mid">—</div></div>
  <div class="card"><div class="label">Fair Value (EWMA)</div><div class="value" id="fair">—</div></div>
  <div class="card"><div class="label">Spread</div><div class="value yellow" id="spread">—</div></div>
  <div class="card"><div class="label">Realised σ</div><div class="value dim" id="sigma">—</div></div>
  <div class="card"><div class="label">Net Inventory</div><div class="value" id="inv">—</div></div>
  <div class="card"><div class="label">Cycles / Uptime</div><div class="value" id="cycle">—</div><div class="uptime" id="uptime">—</div></div>
</div>

<div class="section">
  <h2>Open Orders</h2>
  <div class="order-row"><span class="green" id="bid_quote">BID —</span><span id="bid_status"></span><span class="dim" id="bid_oid">—</span></div>
  <div class="order-row"><span class="red" id="ask_quote">ASK —</span><span id="ask_status"></span><span class="dim" id="ask_oid">—</span></div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div class="section">
    <h2>Balances</h2>
    <table><thead><tr><th>Asset</th><th>Qty</th></tr></thead><tbody id="balances"></tbody></table>
  </div>
  <div class="section">
    <h2>Mid Price (last 60 samples)</h2>
    <div class="sparkline" id="sparkline"></div>
  </div>
</div>

<div class="section" style="margin-bottom:16px">
  <h2>Fill History</h2>
  <table>
    <thead><tr><th>Time</th><th>Side</th><th>Qty BTC</th><th>Price</th><th>Notional</th></tr></thead>
    <tbody id="fills"></tbody>
  </table>
</div>

<div class="section">
  <h2>Recent Errors</h2>
  <table><thead><tr><th>Time</th><th>Message</th></tr></thead><tbody id="errors"></tbody></table>
</div>
<p class="dim" style="margin-top:8px" id="footer">connecting…</p>

<script>
const $ = id => document.getElementById(id);
const fmt = (n, dec=2) => n == null ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtT = ts => ts == null ? '—' : new Date(ts*1000).toLocaleTimeString();
const setHTML = (id, html) => { const e = $(id); if (e && e.innerHTML !== html) e.innerHTML = html; };
const setTxt  = (id, txt, cls) => {
  const e = $(id); if (!e) return;
  if (e.textContent !== txt) e.textContent = txt;
  if (cls !== undefined && e.className !== cls) e.className = cls;
};
const badge = s => `<span class="badge badge-${s||'none'}">${s||'—'}</span>`;

async function refresh() {
  let d;
  try { d = await (await fetch('/api/state')).json(); }
  catch (e) { return; }   // transient — keep last good frame, no flash

  setTxt('mid',   '$'+fmt(d.mid),        'value blue');
  setTxt('fair',  '$'+fmt(d.fair_value), 'value');
  setTxt('spread','$'+fmt(d.spread),     'value yellow');
  setTxt('sigma', d.sigma==null ? 'building…' : fmt(d.sigma,4)+' USD/interval', 'value dim');
  const invCls = d.inventory_btc>0 ? 'value green' : d.inventory_btc<0 ? 'value red' : 'value';
  setTxt('inv', fmt(d.inventory_btc,6)+' BTC', invCls);
  setTxt('cycle', String(d.cycle ?? 0), 'value');
  const u = d.uptime_secs||0;
  setTxt('uptime', `${Math.floor(u/3600)}h ${Math.floor((u%3600)/60)}m ${u%60}s`);

  setTxt('bid_quote', 'BID $'+fmt(d.bid_quote), 'green');
  setTxt('ask_quote', 'ASK $'+fmt(d.ask_quote), 'red');
  setHTML('bid_status', badge(d.bid_status));
  setHTML('ask_status', badge(d.ask_status));
  setTxt('bid_oid', d.bid_oid ? d.bid_oid.slice(0,8)+'…' : '—', 'dim');
  setTxt('ask_oid', d.ask_oid ? d.ask_oid.slice(0,8)+'…' : '—', 'dim');

  // sparkline — only the bars change height (CSS-transitioned), no layout churn
  const prices = (d.cycle_history||[]).map(c=>c.mid).filter(Boolean).slice(-60);
  const minP = Math.min(...prices), maxP = Math.max(...prices), range = maxP-minP||1;
  setHTML('sparkline', prices.length
    ? prices.map(p=>`<div class="bar" style="height:${Math.max(2,Math.round(((p-minP)/range)*38))}px" title="${fmt(p)}"></div>`).join('')
    : '<span class="dim">building…</span>');

  setHTML('balances', Object.entries(d.balances||{}).map(([a,v])=>
    `<tr><td>${a}</td><td class="blue">${fmt(v,6)}</td></tr>`).join('')
    || '<tr><td colspan=2 class="dim">No balance data</td></tr>');

  setHTML('fills', (d.fill_history||[]).slice(0,10).map(f=>
    `<tr><td class="dim">${fmtT(f.ts)}</td><td class="${f.side==='buy'?'green':'red'}">${f.side.toUpperCase()}</td>`+
    `<td>${fmt(f.qty,6)}</td><td class="blue">$${fmt(f.price)}</td><td>$${fmt(f.notional)}</td></tr>`).join('')
    || '<tr><td colspan=5 class="dim">No fills yet</td></tr>');

  setHTML('errors', (d.errors||[]).slice(0,5).map(e=>
    `<tr><td class="dim">${fmtT(e.ts)}</td><td class="red">${e.msg}</td></tr>`).join('')
    || '<tr><td colspan=2 class="green">No errors</td></tr>');

  setTxt('footer', `last update ${fmtT(d.last_update)} · live`);
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("Dashboard: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
