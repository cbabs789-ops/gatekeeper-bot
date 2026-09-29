"""Live dashboard served by the bot itself: http://<server-ip>:8080/?k=<key>

Read-only. Shows each strategy's profit and loss, open paper trades with live
value, every buy and sell as it happens, and recent Fomo alerts. The key keeps
strangers out; nothing on the page can change the bot.
"""
import asyncio
import hmac
import json
import logging
import secrets
import time
from datetime import datetime

import aiohttp
from aiohttp import web

from . import config, db, fomo, notify, report
from .strategy import summarize

log = logging.getLogger("gatekeeper.web")
PORT = int(float(config.os.environ.get("GK_WEB_PORT", "8080")))


def web_key(con):
    k = config.os.environ.get("GK_WEB_KEY") or db.kv_get(con, "web_key")
    if not k:
        k = secrets.token_urlsafe(12)
        db.kv_set(con, "web_key", k)
    return str(k)


async def public_url(session, con):
    ip = None
    for url in ("http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address", "https://api.ipify.org"):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status == 200:
                    ip = (await r.text()).strip()
                    break
        except Exception:  # noqa: BLE001
            continue
    return "http://%s:%d/?k=%s" % (ip or "YOUR-SERVER-IP", PORT, web_key(con))


def _link(mint):
    return notify.fomo_url(mint)


def _stats(rows):
    s = summarize(rows)
    return {"trades": s.get("trades", 0), "win_rate": round(s.get("win_rate", 0), 1), "pnl": round(s.get("total_pnl", 0), 2),
            "best": round(s.get("best", 0), 2), "worst": round(s.get("worst", 0), 2)}


def _supply(con, mint, cache={}):
    now = time.time()
    hit = cache.get(mint)
    if hit and now - hit[1] < 300:
        return hit[0]
    r = con.execute("SELECT price, fdv FROM snapshots WHERE mint=? AND price>0 AND fdv>0 ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
    v = r["fdv"] / r["price"] if r else None
    cache[mint] = (v, now)
    return v


def _coin_stats(con, mint):
    """Latest market numbers for a coin, like Fomo's header: volume, buys vs sells, 1h change."""
    r = con.execute("SELECT vol_m5, vol_h1, buys_m5, sells_m5, buys_h1, sells_h1, pc_m5, pc_h1 FROM snapshots "
                    "WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
    return dict(r) if r else None


def _mc(spot, sup):
    return round(spot * sup) if spot and sup else None


def chart_data(con, mint, t0, t1, entry, legs):
    """Price path around a trade as % change from our entry, plus our buy and sell points."""
    rows = con.execute("SELECT ts, price FROM snapshots WHERE mint=? AND ts>=? AND ts<=? ORDER BY ts",
                       (mint, t0 - 10 * 60000, t1)).fetchall()
    step = max(1, len(rows) // 240)
    pts = [[r["ts"], round((r["price"] / entry - 1) * 100, 2)] for r in rows[::step] if r["price"]]
    if rows and rows[-1]["price"] and (not pts or pts[-1][0] != rows[-1]["ts"]):
        pts.append([rows[-1]["ts"], round((rows[-1]["price"] / entry - 1) * 100, 2)])
    mk = [[g.get("ts"), g.get("side"), round((g["spot"] / entry - 1) * 100, 2)] for g in legs if g.get("spot") and g.get("ts")]
    return {"points": pts, "legs": mk, "mc_entry": _mc(entry, _supply(con, mint))}


def _levels(p, pos, entry):
    best = (pos.peak_after / entry - 1) * 100 if entry else 0
    lv = {"stop": -p["STOP_LOSS_PCT"]}
    if not pos.took_half:
        lv["target"] = round((p["TAKE_HALF_X"] - 1) * 100, 1)
    else:
        lv["trail"] = round((pos.peak_after * (1 - p["TRAIL_PCT"] / 100) / entry - 1) * 100, 1)
    if p.get("LOCK_START_PCT") and best >= p["LOCK_START_PCT"]:
        lv["lock"] = round((pos.peak_after * (1 - p["LOCK_TRAIL_PCT"] / 100) / entry - 1) * 100, 1)
    if p.get("BREAKEVEN_AT_PCT") and best >= p["BREAKEVEN_AT_PCT"]:
        lv["floor"] = round(2 * (p["FEE_PCT"] + p["PENALTY_PCT"]), 1)
    return lv


def state(runner):
    con = runner.con
    now = int(time.time() * 1000)
    day_start = int(datetime.now(report.TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    kv = lambda k: db.kv_get(con, k)  # noqa: E731

    def ago(v):
        try:
            return round((now - int(float(v))) / 1000)
        except (TypeError, ValueError):
            return None

    out = {"now": now, "health": {"last_poll_s": ago(kv("last_poll")), "watching": kv("watching"),
                                  "fomo_last_s": ago(kv("fomo_last_event")), "fast_s": ago(kv("last_fast_poll")), "fomo_delay_s": kv("fomo_feed_delay")},
           "strategies": [], "open": [], "closed": [], "activity": [], "alerts": [], "equity": {}}

    for name, st in runner.strats.items():
        since = getattr(runner, "epochs", {}).get(name, 0)
        closed = report._closed(con, strategy=name)
        current = report._closed(con, since_ms=since, strategy=name) if since else closed
        today = report._closed(con, since_ms=max(day_start, since), strategy=name)
        unreal = 0.0
        for mint, pos in st.positions.items():
            cs = st.coins.get(mint)
            price, liq = (cs.last_price, cs.last_liq) if cs else (pos.spot_at_entry, 0)
            value = st.broker.sell(price, liq, pos.qty_open) if pos.qty_open > 0 and liq else 0.0
            pnl = pos.proceeds + value - pos.size_usd
            unreal += pnl
            out["open"].append({
                "strategy": name, "symbol": pos.symbol, "mint": mint, "link": _link(mint), "opened_at": pos.opened_at,
                "size": pos.size_usd, "value_if_sold": round(pos.proceeds + value, 2), "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / pos.size_usd * 100, 1), "move_pct": round((price / pos.spot_at_entry - 1) * 100, 1) if pos.spot_at_entry else None,
                "took_half": pos.took_half, "liq": round(liq), "why": pos.why,
                "mc_in": _mc(pos.spot_at_entry, _supply(con, mint)), "mc_now": _mc(price, _supply(con, mint)),
                "stats": _coin_stats(con, mint), "risk": pos.risk,
                "log": [{"ts": g.get("ts"), "side": g.get("side"), "usd": round(g.get("usd") or 0, 2),
                         "mc": _mc(g.get("spot"), _supply(con, mint)), "why": g.get("why") or ""} for g in pos.legs],
                "stop_at": round(pos.spot_at_entry * (1 - st.p["STOP_LOSS_PCT"] / 100), 12),
                "chart": dict(chart_data(con, mint, pos.opened_at, now, pos.spot_at_entry, pos.legs),
                              levels=_levels(st.p, pos, pos.spot_at_entry)) if pos.spot_at_entry else None})
        out["strategies"].append({
            "name": name, "label": report.LABEL.get(name, name), "alerts": name in config.ALERT_PRESETS,
            "all": _stats(closed), "current": _stats(current), "since": since, "today": _stats(today),
            "open": len(st.positions), "unrealized": round(unreal, 2)})
        eq, pts = 0.0, []
        for r in con.execute("SELECT closed_at, pnl_usd FROM trades WHERE mode='live' AND closed_at IS NOT NULL "
                             "AND COALESCE(run_id,'main')=? AND closed_at>=? ORDER BY closed_at", (name, since)):
            eq += r["pnl_usd"] or 0
            pts.append([r["closed_at"], round(eq, 2)])
        step = max(1, len(pts) // 300)
        out["equity"][name] = pts[::step] + (pts[-1:] if pts and (len(pts) - 1) % step else [])

    for r in con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 50"):
        try:
            lg = json.loads(r["legs"] or "[]")
        except ValueError:
            lg = []
        sup = _supply(con, r["mint"])
        sells = [g for g in lg if g.get("side") == "sell"]
        log = [{"ts": g.get("ts"), "side": g.get("side"), "usd": round(g.get("usd") or 0, 2), "mc": _mc(g.get("spot"), sup),
                "why": g.get("why") or ""} for g in lg]
        out["closed"].append({"log": log, "mc_in": _mc(lg[0].get("spot") if lg else None, sup), "mc_out": _mc(sells[-1].get("spot") if sells else None, sup),
                              "id": r["id"], "strategy": r["run_id"] or "main", "symbol": r["symbol"], "link": _link(r["mint"]),
                              "opened_at": r["opened_at"], "closed_at": r["closed_at"], "pnl": round(r["pnl_usd"] or 0, 2),
                              "pnl_pct": round(r["pnl_pct"] or 0, 1), "exit": r["exit_reason"], "why": r["why_entered"]})

    acts = []
    for r in con.execute("SELECT run_id, symbol, mint, legs FROM trades WHERE mode='live' AND opened_at>=?", (now - 3 * 86400000,)):
        try:
            legs = json.loads(r["legs"] or "[]")
        except ValueError:
            continue
        for g in legs:
            acts.append({"ts": g.get("ts"), "strategy": r["run_id"] or "main", "symbol": r["symbol"], "link": _link(r["mint"]),
                         "mc": _mc(g.get("spot"), _supply(con, r["mint"])),
                         "side": g.get("side"), "usd": round(g.get("usd") or 0, 2), "why": g.get("why") or ""})
    out["activity"] = sorted(acts, key=lambda a: a["ts"] or 0, reverse=True)[:60]

    try:
        fomo.ensure_schema(con)
        for r in con.execute("SELECT a.token_address, a.kind, a.ts, (SELECT token FROM fomo_events e WHERE e.token_address=a.token_address "
                             "ORDER BY ts DESC LIMIT 1) AS token FROM fomo_alerts a ORDER BY a.ts DESC LIMIT 25"):
            out["alerts"].append({"ts": r["ts"], "kind": r["kind"], "token": r["token"] or r["token_address"][:6],
                                  "link": _link(r["token_address"]) if r["token_address"] else None})
    except Exception:  # noqa: BLE001
        log.exception("alerts for dashboard")
    return out


class Hub:
    """Pushes each buy and sell to open dashboards the instant it happens."""
    def __init__(self):
        self.subs = set()

    def publish(self, evt):
        msg = json.dumps(evt)
        for q in list(self.subs):
            if q.qsize() < 100:
                q.put_nowait(msg)


HUB = Hub()


def make_app(runner):
    key = web_key(runner.con)

    def authed(req):
        return hmac.compare_digest(req.query.get("k", ""), key)

    async def page(req):
        if not authed(req):
            return web.Response(status=403, text="Not allowed. Send /site to your Telegram bot for the link.")
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def api(req):
        if not authed(req):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            return web.json_response(state(runner), headers={"Cache-Control": "no-store"})
        except Exception as e:  # noqa: BLE001
            log.exception("dashboard state")
            return web.json_response({"error": str(e)[:200]}, status=500)

    async def icon(req):
        return web.Response(body=ICON, content_type="image/png", headers={"Cache-Control": "max-age=86400"})

    async def stream(req):
        if not authed(req):
            return web.Response(status=403)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q = asyncio.Queue()
        HUB.subs.add(q)
        try:
            await resp.write(b": hello\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), 20)
                    await resp.write(("data: %s\n\n" % msg).encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")      # keeps phones and proxies from dropping the connection
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            HUB.subs.discard(q)
        return resp

    async def chart(req):
        if not authed(req):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            r = runner.con.execute("SELECT * FROM trades WHERE id=?", (int(req.query.get("id", "0")),)).fetchone()
        except ValueError:
            r = None
        if not r:
            return web.json_response({"error": "not found"}, status=404)
        legs = json.loads(r["legs"] or "[]")
        entry = legs[0]["spot"] if legs and legs[0].get("spot") else r["entry_price"]
        st = runner.strats.get(r["run_id"] or "main")
        stop = -(st.p["STOP_LOSS_PCT"] if st else 30)
        d = chart_data(runner.con, r["mint"], r["opened_at"], (r["closed_at"] or r["opened_at"]) + 20 * 60000, entry, legs)
        return web.json_response(dict(d, levels={"stop": stop}), headers={"Cache-Control": "no-store"})

    app = web.Application()
    app.router.add_get("/api/chart", chart)
    app.router.add_get("/", page)
    app.router.add_get("/api/stream", stream)
    app.router.add_get("/icon.png", icon)
    app.router.add_get("/api/state", api)
    return app


async def serve(runner):
    app_runner = web.AppRunner(make_app(runner), access_log=None)
    await app_runner.setup()
    await web.TCPSite(app_runner, "0.0.0.0", PORT).start()
    log.info("Dashboard on port %d", PORT)


import base64 as _b64
ICON = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAAEGUlEQVR42u3dPVbjMBQG0KBDS0HFGmgo6NgJNQujZid0FGnYzhTTzGFIILb+3tP92uEwwbr+JNlOcnVze3cQ+S7FIRA4BA6BQ+AQOAQOgUPgEDgEDhE4BA6BQ+AQOAQOiZHr4a/g8e3JMJzKx/P7wP/9ashjgkCEgNIbBxaBiHTCwUREJYWMoOlwVNs2BxahK6Qhjt/L+Hw5GuP/c//6MNZHKxw/ygCiLpQWPprgOC8Di0ZEqvuoj+OMDCxaE6nrozKOUzKw6Eakoo9CRtCcOqoVd4iFDD7a4iAjpY/S/3VLlONcAce3SMkY7mN/eRQy+BgwrUj0lD6EJeLiYxcON13nz54xKpPjlYHHvwwhKSHKo0zLVoaPgt2KwCHdcFhwrLDsqNYcFhz5lh2mFYFD4BA4pEeuHYKZ8+Up4s6rfs0RRsbhkvfAwbGWjP4+4Igko7MPOILJ6OkDDoEjUW3AIXDIrLUBR1QZfa6GwUEGHGTAQUbFuPEWI0MetNMcAWpj1COYcJABBxlwkAEHGXCsvXGFQyatDThMKHCQAQcZcJABBxlwkGErK8FqAw4TChxkwEEGHGTAQQYcAofaCFYbcJABBxlwkAEHGXBkkRE314lHa5LzNe5XBpTE53HTMzv3hJIKR+dP7VxBRhIcnT+1cxEZGXB0/tTOdWQstFu5f33YT2QpGeFxXDree4ik37imwrFnmNuNdKbvuiurydhGZLUJJTCOzgvMNWWExNFia3rmdy4r4xDr8nmHK55fxnhlGZGao89m4d8WWVxGmObY/Emu20gtuGuN2hx7PuP38+XY9MzO/SXtJbGM1kRyy5h6Wrm0238cqr8/UGvKSC9j3uaoLqPuoK4gY9LmaP1FAjsrZBEZMzZHt6+YaL1WhSOqDEQiTSvtFhkdJhrNkVbGRb92qZopsWSMvai12gRUYsno8JLOXEmz5phuEuk/Np8vxznfH5Ucx/wyVtYwclqJIkN6N8cGGVgs0RxkwGHKh4MMOMiAgwwZisNtCzi2jz0Zq08rblvAcZkPMqbNgBtvNGgOgUPgEDhE4BA4BA6BQ+CQbDi8l3Ce1BqLjTg+nt+NQaBsGy/TisAhY3FYdmRacOzCYdmRe8FRf1pRHmlqYy8O5ZG4NposSJVHjtqwW5GWOL5tLeUxSW3snPcrNAcfKWW0nVb4CLrUqIzjFFI+RsmospGs1hx8JJNReVrhI5OMw+FwdXN7V/dFP749nfon73Vrvcioe1myPo7zPhBpt/asfsG6CY4ffSBSfUvS4lZGKxy/8QFKrT1qo5tcDXFc5EOq7wNmx4FIUBb1t7Kj/gYyAjeHFgl6pvXGgUigAh6DA5QQM/J4HDJtPAkmcAgcAofAIXAIHAKHwCFwCBwicAgcAofAIXDIXPkDjCr3gU2MNCUAAAAASUVORK5CYII=")

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Gatekeeper Live</title>
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Gatekeeper"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117"><link rel="apple-touch-icon" href="icon.png"><link rel="icon" href="icon.png">
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--up:#3fb950;--down:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px;padding-top:max(16px,env(safe-area-inset-top))}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:20px;margin:0}h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:26px 0 10px}
.pill{font-size:12px;color:var(--dim)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--up)}
.dot.bad{background:var(--down)}
.big{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.k{font-size:12px;color:var(--dim)}.v{font-size:22px;font-weight:650;margin-top:2px}
.up{color:var(--up)}.down{color:var(--down)}.dim{color:var(--dim)}
.strats{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}
.srow{display:flex;justify-content:space-between;font-size:13px;margin-top:4px}
svg{width:100%;height:70px;display:block;margin-top:8px}
.list{display:flex;flex-direction:column;gap:6px}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 12px;display:grid;grid-template-columns:1fr auto;gap:2px 10px}
.item a{color:var(--acc);text-decoration:none;font-weight:600}.sub{font-size:12px;color:var(--dim);grid-column:1/-1}
.tag{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:1px 7px;margin-left:6px;color:var(--dim)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.cols{grid-template-columns:1fr}}
.empty{color:var(--dim);font-size:13px;padding:8px 2px}
footer{margin:30px 0 10px;font-size:12px;color:var(--dim)}
.chart{grid-column:1/-1;margin-top:6px}.chart svg{height:170px;margin:0}
.item.tap{cursor:pointer}.hint{font-size:11px;color:var(--dim)}
.stats{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:6px 0 2px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px}
.stat .k{font-size:10.5px}.stat .v{font-size:14px;margin:0}
@media(max-width:420px){.stats{grid-template-columns:repeat(2,1fr)}}
.tlog{grid-column:1/-1;margin-top:6px;border-top:1px solid var(--line);padding-top:6px;display:flex;flex-direction:column;gap:3px}
.trow{display:grid;grid-template-columns:1.2fr 1fr 1.3fr .9fr;gap:6px;font-size:12.5px}.trow .wide{grid-column:1/-1;font-size:11.5px;margin-top:-2px}
.legend{grid-column:1/-1;font-size:11px;color:var(--dim);display:flex;gap:10px;flex-wrap:wrap}
.legend i{display:inline-block;width:12px;height:0;border-top:2px dashed;vertical-align:middle;margin-right:4px}
#toasts{position:fixed;left:50%;transform:translateX(-50%);bottom:max(16px,env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:8px;z-index:9;width:min(92vw,440px)}
.toast{background:#1f2630;border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:10px;padding:10px 14px;font-weight:600;box-shadow:0 6px 24px rgba(0,0,0,.5);animation:pop .25s ease-out}
.toast.buy{border-left-color:var(--up)}.toast.close{border-left-color:var(--warn)}
@keyframes pop{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
</style></head><body><div class="wrap">
<header><h1>🤖 Gatekeeper Live</h1><span class="pill" id="health">connecting…</span></header>
<div class="big" id="big"></div>
<h2>Strategies</h2><div class="strats" id="strats"></div>
<h2>Open paper trades <span class="dim" style="text-transform:none;letter-spacing:0">(value if sold right now, after fees and slippage)</span></h2>
<div class="list" id="open"></div>
<div class="cols">
<div><h2>What he's doing</h2><div class="list" id="activity"></div></div>
<div><h2>Fomo alerts</h2><div class="list" id="alerts"></div></div>
</div>
<h2>Closed trades</h2><div class="list" id="closed"></div>
<footer>Tap any coin name to open it in Fomo. Paper trading with fake money. Buys and sells appear the instant they happen; prices refresh every 5 seconds. Not financial advice.</footer>
<div id="toasts"></div>
</div>
<script>
const K=new URLSearchParams(location.search).get("k")||"";
const $=id=>document.getElementById(id);
const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
const money=v=>(v<0?"-$":"$")+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=v=>(v>0?"+":"")+money(v);
const cls=v=>v>0?"up":v<0?"down":"dim";
const pct=v=>v==null?"n/a":(v>0?"+":"")+v.toFixed(1)+"%";
const t=ms=>{const d=new Date(ms);return d.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})};
const mcf=v=>{if(!v)return null;if(v>=1e9)return "$"+(+(v/1e9).toFixed(2))+"B MC";if(v>=1e6)return "$"+(+(v/1e6).toFixed(2))+"M MC";
 if(v>=1e3)return "$"+(+(v/1e3).toFixed(v>=1e5?0:1))+"K MC";return "$"+Math.round(v)+" MC"};
const dur=ms=>{const m=Math.round(ms/60000);return m<60?m+"m":Math.floor(m/60)+"h "+(m%60)+"m"};
const NAMES={main:"Main",wide:"Wide",follow:"Follow",momentum:"Momentum",survivor:"Survivor"};
function link(txt,href){if(!href)return el("span",null,txt);const a=el("a",null,txt);a.href=href;a.target="_blank";a.rel="noopener";return a}
function spark(pts){const ns="http://www.w3.org/2000/svg",s=document.createElementNS(ns,"svg");s.setAttribute("viewBox","0 0 300 70");s.setAttribute("preserveAspectRatio","none");
 if(!pts||pts.length<2)return s;const ys=pts.map(p=>p[1]).concat([0]);const lo=Math.min(...ys),hi=Math.max(...ys),r=(hi-lo)||1;
 const y=v=>66-(v-lo)/r*62;const x=i=>i/(pts.length-1)*300;
 const z=document.createElementNS(ns,"line");z.setAttribute("x1",0);z.setAttribute("x2",300);z.setAttribute("y1",y(0));z.setAttribute("y2",y(0));z.setAttribute("stroke","#30363d");z.setAttribute("stroke-dasharray","3 3");s.appendChild(z);
 const p=document.createElementNS(ns,"polyline");p.setAttribute("points",pts.map((q,i)=>x(i)+","+y(q[1])).join(" "));p.setAttribute("fill","none");
 p.setAttribute("stroke",pts[pts.length-1][1]>=0?"#3fb950":"#f85149");p.setAttribute("stroke-width","2");p.setAttribute("vector-effect","non-scaling-stroke");s.appendChild(p);return s}
function stat(k,v,c){const d=el("div","card");d.append(el("div","k",k),el("div","v "+(c||""),v));return d}
const LV={stop:["#f85149","stop"],target:["#3fb950","sell half"],trail:["#d29922","trailing stop"],lock:["#d29922","profit lock"],floor:["#58a6ff","breakeven floor"]};
function priceChart(d){const box=el("div","chart");const ns="http://www.w3.org/2000/svg";const s=document.createElementNS(ns,"svg");
 const W=340,H=170,L=38,R=6,T=14,B=18;s.setAttribute("viewBox","0 0 "+W+" "+H);box.append(s);
 const pts=(d&&d.points)||[];if(pts.length<2){box.append(el("div","hint","Chart fills in as prices come in."));return box}
 const lv=d.levels||{};const ys=pts.map(p=>p[1]).concat([0],Object.values(lv).filter(v=>v!=null),(d.legs||[]).map(l=>l[2]));
 let lo=Math.min(...ys),hi=Math.max(...ys);const padY=(hi-lo)*0.08||5;lo-=padY;hi+=padY;
 const t0=pts[0][0],t1=pts[pts.length-1][0];const X=t=>L+(t-t0)/((t1-t0)||1)*(W-L-R),Y=v=>T+(hi-v)/(hi-lo)*(H-T-B);
 const add=(tag,a)=>{const e=document.createElementNS(ns,tag);for(const k in a)e.setAttribute(k,a[k]);s.appendChild(e);return e};
 const txt=(x,y,str,anchor)=>{const e=add("text",{x,y,fill:"#8b949e","font-size":"9","text-anchor":anchor||"start"});e.textContent=str};
 const hl=(v,c)=>add("line",{x1:L,x2:W-R,y1:Y(v),y2:Y(v),stroke:c,"stroke-dasharray":"4 3","stroke-width":"1","vector-effect":"non-scaling-stroke"});
 hl(0,"#8b949e");for(const k in lv)if(lv[k]!=null&&LV[k])hl(lv[k],LV[k][0]);
 const me=d.mc_entry;const tag=(v,c,name)=>{const e=add("text",{x:W-R-2,y:Y(v)-3,fill:c,"font-size":"9","text-anchor":"end","font-weight":"600",stroke:"#161b22","stroke-width":"3","paint-order":"stroke"});
  e.textContent=name+(me?" "+mcf(Math.round(me*(1+v/100))).replace(" MC",""):" "+(v>0?"+":"")+v+"%")};
 tag(0,"#c9d1d9","Bought");for(const k in lv)if(lv[k]!=null&&LV[k])tag(lv[k],LV[k][0],LV[k][1][0].toUpperCase()+LV[k][1].slice(1));
 [hi-padY,0,lo+padY].forEach(v=>txt(L-4,Y(v)+3,(v>0?"+":"")+v.toFixed(0)+"%","end"));
 const last=pts[pts.length-1][1];add("polyline",{points:pts.map(p=>X(p[0])+","+Y(p[1])).join(" "),fill:"none",stroke:"#e6edf3","stroke-width":"2","vector-effect":"non-scaling-stroke"});
 (d.legs||[]).forEach(l=>{if(l[0]<t0||l[0]>t1)return;add("circle",{cx:X(l[0]),cy:Y(l[2]),r:4.5,fill:l[1]==="buy"?"#3fb950":"#f0883e",stroke:"#0d1117","stroke-width":"1.5"})});
 const tm=ms=>new Date(ms).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"});txt(L,H-4,tm(t0));txt(W-R,H-4,tm(t1),"end");
 const lg=el("div","legend");const it=(c,n)=>{const sp=el("span");const i=el("i");i.style.borderColor=c;sp.append(i,document.createTextNode(n));lg.append(sp)};
 const pl=el("span","","━ price");pl.style.color="#e6edf3";lg.append(pl);it("#8b949e","entry");for(const k in lv)if(lv[k]!=null&&LV[k])it(LV[k][0],LV[k][1]+" "+(lv[k]>0?"+":"")+lv[k]+"%");
 const dot=(c,n)=>{const sp=el("span",null,"● "+n);sp.style.color=c;lg.append(sp)};dot("#3fb950","buy");dot("#f0883e","sell");box.append(lg);return box}
function tradeLog(log){const box=el("div","tlog");(log||[]).forEach(g=>{const r=el("div","trow");
  const buy=g.side==="buy";r.append(el("span",buy?"up":"down",buy?"🟢 Bought":((g.why||"").includes("half")||(g.why||"").includes("moonbag")?"🟡 Sold part":"🔴 Sold")),
   el("span",null,money(g.usd)),el("span",null,g.mc?"at "+mcf(g.mc):""),el("span","dim",new Date(g.ts).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})));
  if(g.why&&!buy)r.append(el("span","dim wide",g.why));box.append(r)});return box}
const OPEN_CHARTS={};
async function toggleChart(item,id){if(OPEN_CHARTS[id]){delete OPEN_CHARTS[id];const c=item.querySelector(".chart");if(c)c.remove();const g=item.querySelector(".legend");if(g)g.remove();return}
 OPEN_CHARTS[id]="loading";try{const r=await fetch("api/chart?id="+id+"&k="+encodeURIComponent(K));OPEN_CHARTS[id]=await r.json();item.append(priceChart(OPEN_CHARTS[id]))}catch(_){delete OPEN_CHARTS[id]}}
function render(s){
 const h=s.health,ok=h.last_poll_s!=null&&h.last_poll_s<120;
 $("health").replaceChildren(el("span","dot"+(ok?"":" bad")),document.createTextNode((ok?"Live":"Price feed stalled")+" · last price check "+(h.last_poll_s??"?")+"s ago · "+(h.watching??"?")+" coins watched · open trades priced "+(h.fast_s!=null?h.fast_s+"s ago":"every 30s")+" · Fomo "+(h.fomo_last_s!=null?h.fomo_last_s+"s ago":"off")));
 let all=0,today=0,unr=0,open=0;s.strategies.forEach(x=>{all+=x.current.pnl;today+=x.today.pnl;unr+=x.unrealized;open+=x.open});
 $("big").replaceChildren(stat("Closed P/L, current rules",sgn(all),cls(all)),stat("Closed P/L today",sgn(today),cls(today)),stat("Open trades P/L now",sgn(unr),cls(unr)),stat("Open trades",String(open)));
 $("strats").replaceChildren(...s.strategies.map(x=>{const c=el("div","card");const top=el("div","srow");const n=el("b",null,x.label);if(x.alerts)n.append(el("span","tag","alerts on"));
  top.append(n,el("b",cls(x.current.pnl),sgn(x.current.pnl)));c.append(top);
  const r=(a,b)=>{const d=el("div","srow");d.append(el("span","dim",a),el("span",null,b));c.append(d)};
  r(x.since?"Since rules changed "+t(x.since):"All time",x.current.trades+" trades · "+x.current.win_rate+"% win");
  if(x.since&&x.all.trades>x.current.trades)r("All time, incl. old rules",x.all.trades+" trades · "+sgn(x.all.pnl));r("Today",x.today.trades+" trades · "+sgn(x.today.pnl));r("Open now",x.open+" · "+sgn(x.unrealized));
  c.append(spark(s.equity[x.name]));return c}));
 const o=s.open.sort((a,b)=>b.opened_at-a.opened_at);
 $("open").replaceChildren(...(o.length?o.map(p=>{const d=el("div","item");const n=link("$"+p.symbol,p.link);const w=el("div");w.append(n,el("span","tag",NAMES[p.strategy]||p.strategy));if(p.took_half)w.append(el("span","tag","sold half"));
  d.append(w,el("b",cls(p.pnl),sgn(p.pnl)+" ("+pct(p.pnl_pct)+")"));
  if(p.mc_in)d.append(el("div","sub","Bought at "+mcf(p.mc_in)+" · now "+(mcf(p.mc_now)||"?")));
  if(p.risk!=null){const rk=el("div","sub","Rug risk at entry: "+Math.round(p.risk)+"%"+(p.risk>=50?" · will take a quick profit and leave":""));rk.style.color=p.risk>=50?"var(--down)":p.risk>=30?"var(--warn)":"var(--up)";d.append(rk)}
  {const g=el("div","stats"),S=p.stats||{};const cell=(k,v,c)=>{const b=el("div","stat");b.append(el("div","k",k),el("div","v "+(c||""),v));g.append(b)};
   const km=v=>v==null?"n/a":(v>=1e6?"$"+(v/1e6).toFixed(2)+"M":v>=1e3?"$"+(v/1e3).toFixed(1)+"K":"$"+Math.round(v));
   cell("Market cap",(mcf(p.mc_now)||"n/a").replace(" MC",""));cell("Pool",km(p.liq));
   cell("Volume 1h",km(S.vol_h1));cell("Volume 5m",km(S.vol_m5));
   cell("Buys / sells 5m",(S.buys_m5??"?")+" / "+(S.sells_m5??"?"),(S.buys_m5||0)>=(S.sells_m5||0)?"up":"down");
   cell("Buys / sells 1h",(S.buys_h1??"?")+" / "+(S.sells_h1??"?"),(S.buys_h1||0)>=(S.sells_h1||0)?"up":"down");
   cell("Change 5m",S.pc_m5==null?"n/a":pct(S.pc_m5),cls(S.pc_m5||0));cell("Change 1h",S.pc_h1==null?"n/a":pct(S.pc_h1),cls(S.pc_h1||0));d.append(g)}
  d.append(el("div","sub","Price "+pct(p.move_pct)+" since entry · held "+dur(s.now-p.opened_at)+" · pool "+money(p.liq).replace(".00","")+" · $"+p.size+" in, worth "+money(p.value_if_sold)+" if sold"));
  if(p.why)d.append(el("div","sub","Why: "+p.why));if(p.chart)d.append(priceChart(p.chart));d.append(tradeLog(p.log));return d}):[el("div","empty","No open trades. He's waiting for a setup.")]));
 $("activity").replaceChildren(...(s.activity.length?s.activity.map(a=>{const d=el("div","item");const w=el("div");const icon=a.side==="buy"?"🟢 Bought ":"🔴 Sold ";w.append(document.createTextNode(icon),link("$"+a.symbol,a.link),el("span","tag",NAMES[a.strategy]||a.strategy));
  d.append(w,el("span","dim",money(a.usd)));d.append(el("div","sub",t(a.ts)+(a.mc?" · at "+mcf(a.mc):"")+(a.why?" · "+a.why:"")));return d}):[el("div","empty","No trades in the last 3 days.")]));
 $("alerts").replaceChildren(...(s.alerts.length?s.alerts.map(a=>{const d=el("div","item");const w=el("div");w.append(document.createTextNode(a.kind==="cluster"?"🔵 Cluster ":"🔥 Trending "),link("$"+a.token,a.link));
  d.append(w,el("span","dim",t(a.ts)));return d}):[el("div","empty","No Fomo alerts yet.")]));
 $("closed").replaceChildren(...(s.closed.length?s.closed.map(c=>{const d=el("div","item");const w=el("div");w.append(link("$"+c.symbol,c.link),el("span","tag",NAMES[c.strategy]||c.strategy));
  d.append(w,el("b",cls(c.pnl),sgn(c.pnl)+" ("+pct(c.pnl_pct)+")"));if(c.mc_in)d.append(el("div","sub","Bought at "+mcf(c.mc_in)+(c.mc_out?" → sold at "+mcf(c.mc_out):"")));
  d.append(el("div","sub",t(c.closed_at)+" · held "+dur(c.closed_at-c.opened_at)+" · "+(c.exit||"")+" · tap for chart"));
  d.append(tradeLog(c.log));
  d.classList.add("tap");d.onclick=e=>{if(e.target.closest("a"))return;toggleChart(d,c.id)};
  const cached=OPEN_CHARTS[c.id];if(cached&&cached!=="loading")d.append(priceChart(cached));return d}):[el("div","empty","No closed trades yet.")]));
}
async function load(){try{const r=await fetch("api/state?k="+encodeURIComponent(K),{cache:"no-store"});if(!r.ok)throw new Error(r.status);render(await r.json())}
 catch(e){$("health").replaceChildren(el("span","dot bad"),document.createTextNode("Can't reach the bot ("+e.message+"). Retrying…"))}}
load();setInterval(load,5000);
function toast(e){const t=el("div","toast "+e.type);const n=NAMES[e.strategy]||e.strategy;
 t.textContent=e.type==="buy"?"🟢 "+n+" bought $"+e.symbol+" · "+money(e.usd):e.type==="partial"?((e.reason||"").includes("moonbag")?"🌙 "+n+" took profit on $"+e.symbol+", kept a moonbag · ":"🟡 "+n+" sold half of $"+e.symbol+" · ")+money(e.usd):
  "🔴 "+n+" sold $"+e.symbol+" · "+(e.pnl!=null?sgn(e.pnl):"")+(e.reason?" · "+e.reason:"");
 $("toasts").prepend(t);setTimeout(()=>t.remove(),12000);try{navigator.vibrate&&navigator.vibrate(120)}catch(_){}}
function live(){try{const es=new EventSource("api/stream?k="+encodeURIComponent(K));
 es.onmessage=m=>{try{toast(JSON.parse(m.data))}catch(_){}load()};es.onerror=()=>{es.close();setTimeout(live,5000)}}catch(_){}}
live();
</script></body></html>"""
