"""Live dashboard served by the bot itself: http://<server-ip>:8080/?k=<key>

Read-only. Shows each strategy's profit and loss, open paper trades with live
value, every buy and sell as it happens, and recent Fomo alerts. The key keeps
strangers out; nothing on the page can change the bot.
"""
import hmac
import json
import logging
import secrets
import time
from datetime import datetime

import aiohttp
from aiohttp import web

from . import config, db, fomo, report
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
    return "https://dexscreener.com/%s/%s" % ("robinhood" if mint.startswith("0x") else "solana", mint)


def _stats(rows):
    s = summarize(rows)
    return {"trades": s.get("trades", 0), "win_rate": round(s.get("win_rate", 0), 1), "pnl": round(s.get("total_pnl", 0), 2),
            "best": round(s.get("best", 0), 2), "worst": round(s.get("worst", 0), 2)}


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
                "stop_at": round(pos.spot_at_entry * (1 - st.p["STOP_LOSS_PCT"] / 100), 12)})
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
        out["closed"].append({"strategy": r["run_id"] or "main", "symbol": r["symbol"], "link": _link(r["mint"]),
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

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/api/state", api)
    return app


async def serve(runner):
    app_runner = web.AppRunner(make_app(runner), access_log=None)
    await app_runner.setup()
    await web.TCPSite(app_runner, "0.0.0.0", PORT).start()
    log.info("Dashboard on port %d", PORT)


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gatekeeper Live</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--text:#e6edf3;--dim:#8b949e;--up:#3fb950;--down:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
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
<footer>Paper trading with fake money. Refreshes every 5 seconds. Open trades are priced every 10 seconds. Not financial advice.</footer>
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
const dur=ms=>{const m=Math.round(ms/60000);return m<60?m+"m":Math.floor(m/60)+"h "+(m%60)+"m"};
const NAMES={main:"Main",wide:"Wide",follow:"Follow"};
function link(txt,href){if(!href)return el("span",null,txt);const a=el("a",null,txt);a.href=href;a.target="_blank";a.rel="noopener";return a}
function spark(pts){const ns="http://www.w3.org/2000/svg",s=document.createElementNS(ns,"svg");s.setAttribute("viewBox","0 0 300 70");s.setAttribute("preserveAspectRatio","none");
 if(!pts||pts.length<2)return s;const ys=pts.map(p=>p[1]).concat([0]);const lo=Math.min(...ys),hi=Math.max(...ys),r=(hi-lo)||1;
 const y=v=>66-(v-lo)/r*62;const x=i=>i/(pts.length-1)*300;
 const z=document.createElementNS(ns,"line");z.setAttribute("x1",0);z.setAttribute("x2",300);z.setAttribute("y1",y(0));z.setAttribute("y2",y(0));z.setAttribute("stroke","#30363d");z.setAttribute("stroke-dasharray","3 3");s.appendChild(z);
 const p=document.createElementNS(ns,"polyline");p.setAttribute("points",pts.map((q,i)=>x(i)+","+y(q[1])).join(" "));p.setAttribute("fill","none");
 p.setAttribute("stroke",pts[pts.length-1][1]>=0?"#3fb950":"#f85149");p.setAttribute("stroke-width","2");p.setAttribute("vector-effect","non-scaling-stroke");s.appendChild(p);return s}
function stat(k,v,c){const d=el("div","card");d.append(el("div","k",k),el("div","v "+(c||""),v));return d}
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
  d.append(el("div","sub","Price "+pct(p.move_pct)+" since entry · held "+dur(s.now-p.opened_at)+" · pool "+money(p.liq).replace(".00","")+" · $"+p.size+" in, worth "+money(p.value_if_sold)+" if sold"));
  if(p.why)d.append(el("div","sub","Why: "+p.why));return d}):[el("div","empty","No open trades. He's waiting for a setup.")]));
 $("activity").replaceChildren(...(s.activity.length?s.activity.map(a=>{const d=el("div","item");const w=el("div");const icon=a.side==="buy"?"🟢 Bought ":"🔴 Sold ";w.append(document.createTextNode(icon),link("$"+a.symbol,a.link),el("span","tag",NAMES[a.strategy]||a.strategy));
  d.append(w,el("span","dim",money(a.usd)));d.append(el("div","sub",t(a.ts)+(a.why?" · "+a.why:"")));return d}):[el("div","empty","No trades in the last 3 days.")]));
 $("alerts").replaceChildren(...(s.alerts.length?s.alerts.map(a=>{const d=el("div","item");const w=el("div");w.append(document.createTextNode(a.kind==="cluster"?"🔵 Cluster ":"🔥 Trending "),link("$"+a.token,a.link));
  d.append(w,el("span","dim",t(a.ts)));return d}):[el("div","empty","No Fomo alerts yet.")]));
 $("closed").replaceChildren(...(s.closed.length?s.closed.map(c=>{const d=el("div","item");const w=el("div");w.append(link("$"+c.symbol,c.link),el("span","tag",NAMES[c.strategy]||c.strategy));
  d.append(w,el("b",cls(c.pnl),sgn(c.pnl)+" ("+pct(c.pnl_pct)+")"));d.append(el("div","sub",t(c.closed_at)+" · held "+dur(c.closed_at-c.opened_at)+" · "+(c.exit||"")));return d}):[el("div","empty","No closed trades yet.")]));
}
async function load(){try{const r=await fetch("api/state?k="+encodeURIComponent(K),{cache:"no-store"});if(!r.ok)throw new Error(r.status);render(await r.json())}
 catch(e){$("health").replaceChildren(el("span","dot bad"),document.createTextNode("Can't reach the bot ("+e.message+"). Retrying…"))}}
load();setInterval(load,5000);
</script></body></html>"""
