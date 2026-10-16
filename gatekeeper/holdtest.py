"""Hold test: replay the Hold bot's rules (and other versions of them) on months of real price history.

The Hold bot only learns from live trades, about one a day. This tests the same rules on the 4-hour candles
GeckoTerminal keeps for established Solana coins (up to about 160 days back), so the slow lane can be judged in
days instead of months.

How a trade is replayed:
  - the signal is read on a finished 4-hour candle; the buy happens at the NEXT candle's open (no peeking)
  - inside each later candle the stop and the trailing stop are checked before the candle's high can raise the peak,
    so a candle that touched both counts as the worse outcome
  - up to 5 trades open at once, $300 each, 1.5% cost per side (the Hold bot's own numbers), 3 days before
    buying the same coin again
Not in the replay: buyers vs sellers (no history for it) and the live safety check.

Big caveat, said in the report too: the coins tested are the ones that are established TODAY. Coins that looked
established months ago and then died are missing, so every result here is better than reality. A rule that loses
even on today's survivors is dead; a rule that wins still needs the live paper test.
"""
import asyncio
import json
import logging
import time

import aiohttp

from . import db
from .hold import SKIP

log = logging.getLogger("gatekeeper.holdtest")
H4 = 4 * 3600 * 1000
DAY = 86400000
OHLCV = "https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/hour?aggregate=4&limit=1000&currency=usd&token=base"
TOKEN_POOLS = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools?page=1"
BET, COST, MAX_OPEN, COOLDOWN = 300.0, 0.015, 5, 3 * DAY
SCHEMA = """
CREATE TABLE IF NOT EXISTS hold_ohlcv (pool TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (pool, ts));
CREATE TABLE IF NOT EXISTS hold_ohlcv_meta (pool TEXT PRIMARY KEY, mint TEXT, symbol TEXT, supply REAL, fetched_at INTEGER);
"""

# (label, entry mode, stop %, trail starts at +%, trail %, max days, cap range)
VARIANTS = [
    ("Hold bot as set now: climbing, stop -35%, trail 30% from +30%, 14 days", "climb", 35, 30, 30, 14, None),
    ("Hold bot before Oct 12: climbing, stop -20%, trail 25% from +25%, 7 days", "climb", 20, 25, 25, 7, None),
    ("Climbing, tight: stop -20%, trail 20% from +20%, 14 days", "climb", 20, 20, 20, 14, None),
    ("Climbing, wide: stop -50%, trail 40% from +50%, 30 days", "climb", 50, 50, 40, 30, None),
    ("Climbing, small caps only ($5M to $50M)", "climb", 35, 30, 30, 14, (5e6, 50e6)),
    ("Climbing, big caps only ($50M to $500M)", "climb", 35, 30, 30, 14, (50e6, 500e6)),
    ("Dip in an uptrend (down 8-25% in 24h, still above its 7-day average)", "dip", 35, 30, 30, 14, None),
    ("Dip in an uptrend, wide: stop -50%, trail 40% from +50%, 30 days", "dip", 50, 50, 40, 30, None),
    ("Breakout to a new 7-day high", "breakout", 35, 30, 30, 14, None),
    ("Breakout, tight: stop -20%, trail 20% from +20%, 7 days", "breakout", 20, 20, 20, 7, None),
    ("Breakout, wide: stop -50%, trail 40% from +50%, 30 days", "breakout", 50, 50, 40, 30, None),
    ("Reference: buy any coin at any time, same exits as now", "any", 35, 30, 30, 14, None),
]


def ensure(con):
    con.executescript(SCHEMA)


async def _get(session, url):
    for attempt in range(3):
        try:
            async with session.get(url, headers={"accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 429:                       # GeckoTerminal's free limit: wait and retry
                    await asyncio.sleep(20 * (attempt + 1))
                    continue
                if r.status != 200:
                    return None
                return await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            log.info("GeckoTerminal %s: %s", url[:90], e)
            await asyncio.sleep(3)
    return None


def _loose(c):
    """Wider than the Hold bot's watchlist, so the test covers coins near the edges too."""
    if c["symbol"].upper() in SKIP or not c.get("pool") or not c["price"]:
        return False
    if len(c["symbol"]) <= 6 and c["symbol"].endswith("x") and c["symbol"][:-1].isupper():
        return False                                       # tokenized stocks
    return 2e6 <= c["fdv"] <= 1e9 and c["liq"] >= 100000 and c["liq"] >= c["fdv"] * 0.003


async def gather(runner, max_coins=160, progress=None):
    """Pick the coins and download their 4-hour candles (cached for 12 hours)."""
    con = runner.con
    ensure(con)
    coins = await runner.hold.fetch()
    uni = {m: c for m, c in coins.items() if _loose(c)}
    # coins the Hold bot has watched before, even if they're off today's lists
    for r in con.execute("SELECT DISTINCT mint FROM hold_snaps").fetchall():
        if r["mint"] in uni or len(uni) >= max_coins:
            continue
        data = await _get(runner.session, TOKEN_POOLS % r["mint"])
        await asyncio.sleep(2.5)
        best = None
        for p in (data or {}).get("data") or []:
            a = p.get("attributes") or {}
            liq = float(a.get("reserve_in_usd") or 0)
            if not best or liq > best[0]:
                best = (liq, a.get("address"), float(a.get("base_token_price_usd") or 0),
                        float(a.get("market_cap_usd") or 0) or float(a.get("fdv_usd") or 0), (a.get("name") or "").split(" / ")[0])
        if best and best[1] and best[2] > 0 and best[3] > 0:
            uni[r["mint"]] = {"mint": r["mint"], "symbol": best[4] or "?", "pool": best[1], "price": best[2], "fdv": best[3], "liq": best[0]}
    picked = sorted(uni.values(), key=lambda c: -c["liq"])[:max_coins]
    now = int(time.time() * 1000)
    fetched = 0
    for i, c in enumerate(picked):
        meta = con.execute("SELECT fetched_at FROM hold_ohlcv_meta WHERE pool=?", (c["pool"],)).fetchone()
        supply = c["fdv"] / c["price"] if c["price"] else 0
        if meta and now - meta["fetched_at"] < 12 * 3600 * 1000:
            con.execute("UPDATE hold_ohlcv_meta SET supply=?, symbol=? WHERE pool=?", (supply, c["symbol"], c["pool"]))
            continue
        data = await _get(runner.session, OHLCV % c["pool"])
        rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        con.executemany("INSERT OR REPLACE INTO hold_ohlcv VALUES(?,?,?,?,?,?,?)",
                        [(c["pool"], int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5] or 0))
                         for x in rows if len(x) >= 6 and float(x[4] or 0) > 0])
        con.execute("INSERT OR REPLACE INTO hold_ohlcv_meta VALUES(?,?,?,?,?)", (c["pool"], c["mint"], c["symbol"], supply, now))
        fetched += 1
        if progress and fetched % 40 == 0:
            await progress("📥 Downloaded price history for %d of %d coins…" % (i + 1, len(picked)))
        await asyncio.sleep(2.2)
    return [c["pool"] for c in picked]


def load(con, pools):
    out = {}
    for pool in pools:
        meta = con.execute("SELECT * FROM hold_ohlcv_meta WHERE pool=?", (pool,)).fetchone()
        rows = con.execute("SELECT ts, o, h, l, c, v FROM hold_ohlcv WHERE pool=? ORDER BY ts", (pool,)).fetchall()
        if meta and meta["supply"] and len(rows) >= 60:
            out[pool] = {"symbol": meta["symbol"], "supply": meta["supply"], "k": [tuple(r) for r in rows]}
    return out


def _signal(mode, k, i, supply, caps):
    """Does candle i (finished) give a buy signal?"""
    ts, o, h, l, c, v = k[i]
    if i < 42 or (ts - k[i - 6][0]) > 26 * 3600 * 1000:        # needs 7 days of history and no gap in the last day
        return False
    cap = c * supply
    lo, hi = caps or (5e6, 500e6)
    if not (lo <= cap <= hi):
        return False
    if sum(x[5] for x in k[i - 5:i + 1]) < 100000:              # under $100K traded in 24h: too quiet to be "established"
        return False
    pc24 = c / k[i - 6][4] - 1
    pc4 = c / k[i - 1][4] - 1
    if mode == "climb":
        return 0.03 <= pc24 <= 0.50 and pc4 >= 0
    if mode == "dip":
        avg7 = sum(x[4] for x in k[i - 42:i]) / 42
        return -0.25 <= pc24 <= -0.08 and c > avg7
    if mode == "breakout":
        return c >= max(x[4] for x in k[i - 42:i]) and pc24 <= 0.60
    return True                                                 # "any"


def _exit(k, j0, entry, stop, tstart, trail, days):
    """Walk forward from the entry candle j0. Returns (exit index, exit price, reason, still open at the end)."""
    stop_px = entry * (1 - stop / 100)
    peak = entry
    t0 = k[j0][0]
    for j in range(j0, len(k)):
        ts, o, h, l, c, v = k[j]
        if j > j0 and o <= stop_px:
            return j, o, "stop (gapped)", False
        if l <= stop_px:
            return j, stop_px, "stop", False
        if peak >= entry * (1 + tstart / 100):
            tp = peak * (1 - trail / 100)
            if j > j0 and o <= tp:
                return j, o, "trailing stop", False
            if l <= tp:
                return j, tp, "trailing stop", False
        peak = max(peak, h)
        if ts - t0 >= days * DAY:
            return j, c, "time limit", False
    return len(k) - 1, k[-1][4], "still open (valued at the last price)", True


def simulate(data, variant):
    label, mode, stop, tstart, trail, days, caps = variant
    cands = []                                                  # every possible trade, in time order
    for pool, d in data.items():
        k = d["k"]
        nxt = 0
        for i in range(len(k) - 1):
            if i < nxt or not _signal(mode, k, i, d["supply"], caps):
                continue
            entry = k[i + 1][1]
            if entry <= 0:
                continue
            j, px, why, still = _exit(k, i + 1, entry, stop, tstart, trail, days)
            cands.append({"pool": pool, "sym": d["symbol"], "t_in": k[i + 1][0], "t_out": k[j][0], "entry": entry, "exit": px,
                          "why": why, "open": still})
            # this coin can't be bought again until 3 days after this trade would have ended
            while nxt < len(k) and k[nxt][0] < k[j][0] + COOLDOWN:
                nxt += 1
    cands.sort(key=lambda t: t["t_in"])
    taken, opens = [], []
    for t in cands:
        opens = [x for x in opens if x["t_out"] > t["t_in"]]
        if len(opens) >= MAX_OPEN or any(x["pool"] == t["pool"] for x in opens):
            continue
        g = t["exit"] / t["entry"]
        t["pnl"] = BET * (g * (1 - COST) / (1 + COST) - 1)
        taken.append(t)
        opens.append(t)
    return taken


def _sum(ts):
    if not ts:
        return {"n": 0, "win": 0, "pnl": 0.0}
    return {"n": len(ts), "win": round(sum(1 for t in ts if t["pnl"] > 0) / len(ts) * 100), "pnl": round(sum(t["pnl"] for t in ts), 2)}


def run(con, pools):
    data = load(con, pools)
    if not data:
        return {"coins": 0}
    t0 = min(d["k"][0][0] for d in data.values())
    t1 = max(d["k"][-1][0] for d in data.values())
    mid = (t0 + t1) // 2
    res = {"coins": len(data), "start": t0, "end": t1, "mid": mid, "rows": []}
    for v in VARIANTS:
        ts = simulate(data, v)
        a, b = [t for t in ts if t["t_in"] < mid], [t for t in ts if t["t_in"] >= mid]
        best = sorted(ts, key=lambda t: -t["pnl"])[:2]
        worst = sorted(ts, key=lambda t: t["pnl"])[:1]
        res["rows"].append({"label": v[0], "all": _sum(ts), "first": _sum(a), "second": _sum(b),
                            "still_open": sum(1 for t in ts if t["open"]),
                            "best": ["$%s %+.0f%%" % (t["sym"], (t["exit"] / t["entry"] - 1) * 100) for t in best],
                            "worst": ["$%s %+.0f%%" % (t["sym"], (t["exit"] / t["entry"] - 1) * 100) for t in worst]})
    res["rows"].sort(key=lambda r: -min(r["first"]["pnl"], r["second"]["pnl"]))
    return res


def text(res):
    if not res or not res.get("coins"):
        return "💎 Hold test: no price history downloaded yet (or none long enough). Send /holdtest to run it."
    from datetime import datetime
    from .report import TZ
    f = lambda ms: datetime.fromtimestamp(ms / 1000, TZ).strftime("%b %-d")  # noqa: E731
    L = ["💎 <b>Hold test</b>: the slow lane's rules replayed on %d established coins, %s to %s (4-hour candles)" % (
            res["coins"], f(res["start"]), f(res["end"])),
         "Each line: trades · win rate · total profit · first half / second half (split %s). $300 a trade, up to 5 at once, 1.5%% cost each way." % f(res["mid"]),
         "A rule is only real if both halves are positive.\n"]
    ref = next((r for r in res["rows"] if r["label"].startswith("Reference")), None)

    def good(r):
        # both halves positive, enough trades, and better than buying at random in BOTH halves: on fair random prices
        # a rule can still show a few hundred dollars either way by luck, so beating the reference is what counts
        a, x, y = r["all"], r["first"], r["second"]
        if not (x["pnl"] > 0 and y["pnl"] > 0 and a["n"] >= 30) or r is ref:
            return False
        return not ref or (x["pnl"] > ref["first"]["pnl"] and y["pnl"] > ref["second"]["pnl"])

    for r in res["rows"]:
        a, x, y = r["all"], r["first"], r["second"]
        ok = "✅ " if good(r) else ""
        L.append("%s<b>%s</b>: %d · %d%% · $%+.0f · $%+.0f / $%+.0f%s" % (
            ok, r["label"], a["n"], a["win"], a["pnl"], x["pnl"], y["pnl"],
            " · best %s · worst %s" % (", ".join(r["best"]), ", ".join(r["worst"])) if a["n"] else ""))
    passed = [r for r in res["rows"] if good(r)]
    L.append("")
    if passed:
        L.append("Verdict: %d rule(s) made money in both halves AND beat buying at random in both halves (✅). Best: %s." % (
            len(passed), passed[0]["label"]))
    else:
        L.append("Verdict: no rule made money in both halves while also beating buying at random.")
    if ref:
        L.append("The reference line (buy any coin at any time) shows what the market alone did; a rule has to beat it to be worth anything.")
    L.append("⚠️ Survivorship: these are coins that are established TODAY. Coins that looked established months ago and then "
             "died aren't in the test, so real results would be worse. A rule that loses here is dead; one that wins still needs "
             "the live paper test. Not in the replay: buyers vs sellers and the safety check.")
    return "\n".join(L)


def save(con, res):
    db.kv_set(con, "holdtest_result", json.dumps(res))


def saved_text(con):
    try:
        res = json.loads(db.kv_get(con, "holdtest_result") or "null")
    except ValueError:
        res = None
    return text(res) if res else "Not run yet. Send /holdtest in Telegram (takes about 10 minutes)."
