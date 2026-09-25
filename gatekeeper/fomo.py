"""Fomo data through FOMO API (fomoapi.io, unofficial, not affiliated with Fomo).

Two parts:
  1. Live feed (free): every buy and sell Fomo users make, over a websocket.
     We store them and alert on two patterns:
       - CLUSTER: 2+ traders on your list buy the same coin within 30 minutes
       - CROWD:   5+ different Fomo traders buy the same coin within 15 minutes
  2. Trend scan (costs credits): pulls positions for your traders and the people
     they follow, then groups every coin by theme.

Free plan: 250,000 credits a month. Positions and following cost 250 each.
Looking up a trader's wallets costs 2,500, so we avoid it.
"""
import asyncio
import json
import logging
import re
import time
from collections import defaultdict

import aiohttp

from . import config, db

log = logging.getLogger("gatekeeper.fomo")
API = "https://api.fomoapi.io"
WS = "wss://api.fomoapi.io/ws/alerts?key={}"
MIN = 60_000

DEFAULT_TRADERS = ("bigbabba,Onepeterrr,DegenCapitalLLC,xandereef,0xangeryy,quakerrz,tonkadriving,soldax,wsbmod,songz,"
                   "horseimnot,MoonDat,zolandinho,Oura456,CryptoKvon,cashmachine,AltcoinMiyagi,0xNoshy,seralberttrades,"
                   "Iri0o,sol_engineer,rbthreek,pointfarmcap,THEpurestInu,pedrigavifrenki,figaro,pennylane,bamblewood8,"
                   "FullNosyCobra,Aurelius0121,px_721,smileycapital,Salem1299534,CorporateMund0,badabeepp,Chadwardthewise")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fomo_events (
  ts INTEGER, trader TEXT, side TEXT, token TEXT, token_address TEXT, chain TEXT, usd REAL, raw TEXT
);
CREATE INDEX IF NOT EXISTS fomo_ts ON fomo_events(ts);
CREATE INDEX IF NOT EXISTS fomo_tok ON fomo_events(token_address, ts);
CREATE TABLE IF NOT EXISTS fomo_alerts (token_address TEXT, kind TEXT, ts INTEGER);
"""


def key():
    return config.os.environ.get("FOMO_API_KEY", "")


def watchlist():
    raw = config.os.environ.get("GK_FOMO_TRADERS") or DEFAULT_TRADERS
    return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()]


def ensure_schema(con):
    con.executescript(SCHEMA)


# ------------------------------------------------------------------ themes
THEMES = [
    ("AI and tech", r"\b(ai|gpt|agent|bot|neural|claude|openai|grok|llm|robot|cyber|quantum|compute|gpu|data)\b|openai|agi"),
    ("Tokenized stocks", r"^(tsla|nvda|aapl|msft|amzn|goog|googl|meta|spy|qqq|coin|hood|mstr|pltr|gme|amc)x?\b|\bstock\b|equity|nasdaq|s&p"),
    ("Gold and commodities", r"gold|xau|paxg|silver|oil|commodit|bullion"),
    ("Politics", r"trump|maga|biden|elon|musk|vote|president|melania|barron|kamala|vance|doge\b(?=.*gov)|america|usa"),
    ("Dogs", r"dog|inu|shib|doge|pup|wif|bonk|corgi|shiba|pug"),
    ("Cats", r"cat|kitty|meow|popcat|mew|nyan|kitten"),
    ("Frogs and Pepe", r"pepe|frog|toad|kek|ribbit"),
    ("Other animals", r"monkey|ape|chimp|bear|bull|panda|penguin|pengu|hippo|moo|goat|fish|whale|bird|duck|rat|pig|horse|cow|shark|lion|tiger|capy"),
    ("Celebrities and internet people", r"kanye|ye\b|drake|taylor|swift|jeanphil|jean|phil|mrbeast|streamer|kai|speed|tate|diddy|snoop|rogan|ansem"),
    ("Social and viral trends", r"twitter|tweet|tiktok|insta|reddit|viral|meme|trend|attention|brainrot|stonk|sigma|rizz|npc|based|cope"),
    ("Holidays and seasons", r"halloween|oween|xmas|christmas|santa|thanksgiving|uptober|october|summer|winter|spooky|pumpkin"),
    ("Chain and DeFi tokens", r"^(sol|eth|btc|wbtc|weth|usdc|usdt|jup|ray|op|arb|mon|monad|base|bnb|sui|hype)$|\b(swap|dex|stake|yield|lend|lending)\b"),
]


def theme_of(symbol, name=""):
    s = ("%s %s" % (symbol or "", name or "")).lower().strip()
    for label, pat in THEMES:
        if re.search(pat, s):
            return label
    return "Other"


# ------------------------------------------------------------------ REST
class Client:
    def __init__(self, session, con):
        self.s, self.con = session, con

    def _spend(self, credits):
        month = time.strftime("%Y-%m")
        used = int(db.kv_get(self.con, "fomo_credits_" + month, "0")) + credits
        db.kv_set(self.con, "fomo_credits_" + month, used)
        return used

    def credits_used(self):
        return int(db.kv_get(self.con, "fomo_credits_" + time.strftime("%Y-%m"), "0"))

    async def get(self, path, credits=250, params=None):
        cap = int(float(config.os.environ.get("GK_FOMO_MONTHLY_CREDITS", "230000")))
        if self.credits_used() + credits > cap:
            raise RuntimeError("Monthly FOMO API credit cap reached (%d). Raise GK_FOMO_MONTHLY_CREDITS or wait for next month." % cap)
        for attempt in range(3):
            async with self.s.get(API + path, params=params or {}, headers={"authorization": "Bearer " + key()},
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 429:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError("FOMO API %s on %s: %s" % (r.status, path, body[:200]))
                self._spend(credits)
                return json.loads(body)
        raise RuntimeError("FOMO API kept rate-limiting " + path)


def _list_in(obj, *keys):
    """Find the list of records inside a response, whatever it's called."""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for k in keys + ("data", "items", "results", "positions", "users", "following", "trades"):
        v = obj.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            inner = _list_in(v, *keys)
            if inner:
                return inner
    return []


def _first(d, *keys, default=None):
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur and cur[part] is not None:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return default


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_positions(resp):
    out = []
    groups = []
    if isinstance(resp, dict) and (isinstance(resp.get("open"), list) or isinstance(resp.get("closed"), list)):
        groups = [("open", resp.get("open") or []), ("closed", resp.get("closed") or [])]
    else:
        groups = [(None, _list_in(resp, "positions"))]
    for status, rows in groups:
        for p in rows:
            if not isinstance(p, dict):
                continue
            sym = _first(p, "symbol", "tokenSymbol", "token.symbol", "token", "ticker")
            if isinstance(sym, dict):
                sym = sym.get("symbol")
            out.append({
                "symbol": str(sym or "?"),
                "name": _first(p, "name", "tokenName", "token.name", default=""),
                "address": _first(p, "address", "tokenAddress", "mint", "token.address", "token.mint", default=""),
                "chain": str(_first(p, "chain", "token.chain", default="") or ""),
                "status": status or str(_first(p, "status", default="") or "").lower() or ("closed" if _first(p, "closedAt", "exitAt", default=None) else "open"),
                "realized": _num(_first(p, "realizedPnl", "realizedPnlUsd", "pnl.realized", "pnlUsd", "pnl")) or 0.0,
                "unrealized": _num(_first(p, "unrealizedPnl", "unrealizedPnlUsd", "pnl.unrealized")) or 0.0,
                "cost": _num(_first(p, "costBasisUsd", "costBasis", "costUsd", "entryUsd", "invested", "boughtUsd")),
                "thesis": _first(p, "thesis", default=None),
                "opened": _first(p, "openedAt", "entryAt", "firstBuyAt", "createdAt"),
            })
    return out


def parse_following(resp):
    out = []
    for u in _list_in(resp, "following", "users"):
        if not isinstance(u, dict):
            continue
        h = _first(u, "handle", "username", "userName", "name")
        if h:
            out.append({"handle": str(h).lstrip("@"), "followers": _num(_first(u, "followers", "followerCount")) or 0,
                        "volume": _num(_first(u, "volume", "volumeUsd")) or 0, "pnl24h": _num(_first(u, "pnl24h", "pnl24H", "pnl")) or 0})
    return out


# ------------------------------------------------------------------ trend scan
async def trend_scan(session, con, seeds=None, expand_following=True, max_traders=40):
    """Positions for seed traders (+ the best of who they follow), grouped by theme."""
    ensure_schema(con)
    c = Client(session, con)
    seeds = seeds or watchlist()
    traders = list(dict.fromkeys(seeds))
    notes = []
    if expand_following and seeds:
        try:
            fol = parse_following(await c.get("/v2/users/%s/following" % seeds[0]))
            fol.sort(key=lambda u: (u["pnl24h"], u["volume"]), reverse=True)
            for u in fol:
                if u["handle"] not in traders and len(traders) < max_traders:
                    traders.append(u["handle"])
            notes.append("Pulled %d of @%s's follows" % (len(fol), seeds[0]))
        except Exception as e:  # noqa: BLE001
            notes.append("Couldn't read @%s's follows: %s" % (seeds[0], e))
    traders = traders[:max_traders]
    per_trader, failures = {}, []
    for h in traders:
        try:
            per_trader[h] = parse_positions(await c.get("/v2/users/%s/positions" % h))
        except Exception as e:  # noqa: BLE001
            failures.append("%s (%s)" % (h, str(e)[:60]))
            if "credit cap" in str(e):
                break
        await asyncio.sleep(0.4)
    return summarize_scan(per_trader, failures, notes, c.credits_used())


def summarize_scan(per_trader, failures, notes, credits):
    themes = defaultdict(lambda: {"traders": set(), "positions": 0, "wins": 0, "closed": 0, "pnl": 0.0, "open_pnl": 0.0})
    tokens = defaultdict(lambda: {"traders": set(), "theme": "", "pnl": 0.0, "symbol": "", "open": 0})
    trader_pnl = {}
    chains = defaultdict(lambda: {"positions": 0, "open": 0, "pnl": 0.0, "traders": set()})
    for h, rows in per_trader.items():
        tot = 0.0
        for r in rows:
            ch = chains[(r["chain"] or "unknown").lower()]
            ch["positions"] += 1
            ch["open"] += 1 if r["status"] == "open" else 0
            ch["pnl"] += r["realized"] + r["unrealized"]
            ch["traders"].add(h)
            th = theme_of(r["symbol"], r["name"])
            t = themes[th]
            t["traders"].add(h)
            t["positions"] += 1
            pnl = r["realized"] + r["unrealized"]
            t["pnl"] += r["realized"]
            t["open_pnl"] += r["unrealized"]
            if r["status"] == "closed":
                t["closed"] += 1
                t["wins"] += 1 if r["realized"] > 0 else 0
            k = r["address"] or r["symbol"]
            tk = tokens[k]
            tk["traders"].add(h)
            tk["theme"], tk["symbol"], tk["chain"] = th, r["symbol"], r["chain"]
            tk["pnl"] += pnl
            tk["open"] += 1 if r["status"] == "open" else 0
            tot += pnl
        trader_pnl[h] = tot
    return {"themes": themes, "tokens": tokens, "trader_pnl": trader_pnl, "failures": failures, "chains": chains,
            "notes": notes, "credits": credits, "scanned": len(per_trader)}


def money(x):
    return ("-" if x < 0 else "") + "$" + format(abs(int(round(x))), ",")


def scan_text(res):
    L = ["🔎 <b>Fomo trend scan</b>", "Traders scanned: %d" % res["scanned"]]
    L += res["notes"]
    chs = sorted(res.get("chains", {}).items(), key=lambda kv: kv[1]["positions"], reverse=True)
    if chs:
        L.append("\n<b>Which chain they trade on</b> (positions · still open · profit incl. open)")
        for name, c in chs[:6]:
            L.append("%s: %d · %d · %s (%d traders)" % (name.title(), c["positions"], c["open"], money(c["pnl"]), len(c["traders"])))
    th = sorted(res["themes"].items(), key=lambda kv: (len(kv[1]["traders"]), kv[1]["positions"]), reverse=True)
    if th:
        top = th[0]
        L.append("\n<b>Top theme: %s</b> (%d of %d traders in it)" % (top[0], len(top[1]["traders"]), res["scanned"]))
        L.append("\n<b>By theme</b> (traders · positions · win rate · profit taken · open profit)")
        for name, t in th[:10]:
            wr = "%d%%" % round(t["wins"] / t["closed"] * 100) if t["closed"] else "n/a"
            L.append("%s: %d · %d · %s · %s · %s" % (name, len(t["traders"]), t["positions"], wr, money(t["pnl"]), money(t["open_pnl"])))
    shared = [(k, v) for k, v in res["tokens"].items() if len(v["traders"]) >= 2]
    shared.sort(key=lambda kv: (len(kv[1]["traders"]), kv[1]["pnl"]), reverse=True)
    if shared:
        L.append("\n<b>Coins several of them hold or traded</b>")
        for k, v in shared[:10]:
            L.append("$%s (%s, %s): %d traders, %d still open, combined %s" % (v["symbol"], v["theme"], v.get("chain") or "?", len(v["traders"]), v["open"], money(v["pnl"])))
    best = sorted(res["trader_pnl"].items(), key=lambda kv: kv[1], reverse=True)[:5]
    if best:
        L.append("\n<b>Best in this scan</b>")
        L += ["@%s: %s" % (h, money(p)) for h, p in best]
    if res["failures"]:
        L.append("\nCouldn't read: " + ", ".join(res["failures"][:8]))
    L.append("\nFOMO API credits used this month: %s of 250,000" % format(res["credits"], ","))
    return "\n".join(L)


# ------------------------------------------------------------------ live feed
class Feed:
    CLUSTER_WINDOW = 30 * MIN
    CROWD_WINDOW = 15 * MIN

    def __init__(self, con, on_alert):
        self.con = con
        self.on_alert = on_alert          # async fn(kind, info)
        ensure_schema(con)
        self.watch = {h.lower() for h in watchlist()}
        self.min_usd = float(config.os.environ.get("GK_FOMO_MIN_USD", "200"))
        self.crowd_n = int(float(config.os.environ.get("GK_FOMO_CROWD", "5")))
        self.cluster_n = int(float(config.os.environ.get("GK_FOMO_CLUSTER", "2")))

    def _recent(self, addr, since, watch_only):
        rows = self.con.execute("SELECT trader, usd, token FROM fomo_events WHERE token_address=? AND side='buy' AND ts>=? AND usd>=?",
                                (addr, since, self.min_usd)).fetchall()
        buyers = {}
        for r in rows:
            if watch_only and r["trader"].lower() not in self.watch:
                continue
            buyers[r["trader"]] = buyers.get(r["trader"], 0) + (r["usd"] or 0)
        return buyers

    def _alerted(self, addr, kind, now):
        r = self.con.execute("SELECT MAX(ts) t FROM fomo_alerts WHERE token_address=? AND kind=?", (addr, kind)).fetchone()
        return r["t"] and now - r["t"] < 6 * 3600 * 1000

    async def handle(self, m):
        if m.get("type") != "alert" or m.get("alertType") not in ("buy", "sell"):
            return
        now = int(m.get("ts") or time.time() * 1000)
        addr = m.get("tokenAddress") or m.get("token") or ""
        self.con.execute("INSERT INTO fomo_events VALUES(?,?,?,?,?,?,?,?)",
                         (now, m.get("trader") or "", m["alertType"], m.get("token") or "", addr, m.get("chain") or "",
                          _num(m.get("usdValue")) or 0, json.dumps(m)[:2000]))
        if m["alertType"] != "buy" or (_num(m.get("usdValue")) or 0) < self.min_usd:
            return
        info = {"token": m.get("token"), "address": addr, "chain": m.get("chain") or ""}
        cl = self._recent(addr, now - self.CLUSTER_WINDOW, True)
        if len(cl) >= self.cluster_n and not self._alerted(addr, "cluster", now):
            self.con.execute("INSERT INTO fomo_alerts VALUES(?,?,?)", (addr, "cluster", now))
            await self.on_alert("cluster", dict(info, buyers=cl))
            return
        cr = self._recent(addr, now - self.CROWD_WINDOW, False)
        if len(cr) >= self.crowd_n and sum(cr.values()) >= 5000 and not self._alerted(addr, "crowd", now):
            self.con.execute("INSERT INTO fomo_alerts VALUES(?,?,?)", (addr, "crowd", now))
            await self.on_alert("crowd", dict(info, buyers=cr))

    async def run(self):
        if not key():
            log.info("No FOMO_API_KEY; Fomo feed off")
            return
        backoff = 3
        logged = 0
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect(WS.format(key()), heartbeat=30, timeout=20) as ws:
                        log.info("Fomo feed connected")
                        db.kv_set(self.con, "fomo_feed_connected", int(time.time() * 1000))
                        backoff = 3
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    break
                                continue
                            try:
                                m = json.loads(msg.data)
                            except ValueError:
                                continue
                            if m.get("type") == "welcome":
                                db.kv_set(self.con, "fomo_feed_delay", m.get("delaySeconds", 0))
                                continue
                            if m.get("type") != "alert" and logged < 5:
                                logged += 1
                                log.info("Fomo feed message: %s", str(m)[:300])
                            try:
                                await self.handle(m)
                                db.kv_set(self.con, "fomo_last_event", int(time.time() * 1000))
                            except Exception:  # noqa: BLE001
                                log.exception("Fomo feed handler error")
            except Exception as e:  # noqa: BLE001
                log.warning("Fomo feed disconnected: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


def feed_report(con, hours=24):
    """Free report built only from the live feed we've stored."""
    ensure_schema(con)
    since = int(time.time() * 1000) - hours * 3600 * 1000
    rows = con.execute("SELECT trader, side, token, token_address, chain, usd FROM fomo_events WHERE ts>=?", (since,)).fetchall()
    if not rows:
        return "No Fomo feed activity recorded in the last %dh yet." % hours
    watch = {h.lower() for h in watchlist()}
    themes = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "traders": set()})
    toks = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "traders": set(), "watch": set(), "chain": "", "sym": ""})
    for r in rows:
        th = theme_of(r["token"])
        themes[th][r["side"]] += r["usd"] or 0
        themes[th]["traders"].add(r["trader"])
        t = toks[r["token_address"] or r["token"]]
        t[r["side"]] += r["usd"] or 0
        t["sym"], t["chain"] = r["token"], r["chain"]
        if r["side"] == "buy":
            t["traders"].add(r["trader"])
            if r["trader"].lower() in watch:
                t["watch"].add(r["trader"])
    L = ["📡 <b>Fomo feed, last %dh</b>" % hours, "Trades seen: %s from %d traders" % (
        format(len(rows), ","), len({r["trader"] for r in rows}))]
    L.append("\n<b>Where the money is flowing (net buying by theme)</b>")
    for name, t in sorted(themes.items(), key=lambda kv: kv[1]["buy"] - kv[1]["sell"], reverse=True)[:8]:
        L.append("%s: net %s, %d traders" % (name, money(t["buy"] - t["sell"]), len(t["traders"])))
    L.append("\n<b>Most-bought coins</b>")
    for k, t in sorted(toks.items(), key=lambda kv: len(kv[1]["traders"]), reverse=True)[:8]:
        extra = " · your list: %s" % ", ".join("@" + x for x in sorted(t["watch"])[:4]) if t["watch"] else ""
        L.append("$%s (%s): %d buyers, net %s%s" % (t["sym"], t["chain"] or "?", len(t["traders"]), money(t["buy"] - t["sell"]), extra))
    return "\n".join(L)
