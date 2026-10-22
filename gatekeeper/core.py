"""Core: version 2's main engine. Trend-following on big, liquid coins, tested on years of history first.

Why this and not meme coins: three weeks of paper trading, a full rule test, a study of every recorded coin and a
replay of months of established-coin history all said the same thing. On meme coins the costs (about 6% a round
trip) and the rugs eat everything. Big liquid coins cost well under 1% to trade, can't be rugged, and have years of
daily prices to test on before a single paper trade.

How it works:
  1. Download about 2 years (720 days) of daily candles for each coin from Kraken's free public API.
     (GeckoTerminal was used first, but its free history stops at about 180 days: too short for a 100-day rule.)
  2. Replay five simple, well-known trend rules, decided BEFORE looking at any result, on that history.
  3. Pick the rule using ONLY the older half of the history. Report what that rule then did on the newer half,
     next to simply buying and holding the same coins. That newer-half number is the honest score.
  4. Paper-trade the picked rule live: once a day after the daily candle closes, each coin is either held or not.
     $1,000 of paper money per coin, 0.5% cost per side.
The rules are rebuilt every Sunday on the latest history, and a rule change is announced, never silent.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp

from . import db, notify

log = logging.getLogger("gatekeeper.core")
DAY = 86400000
SLEEVE, COST = 1000.0, 0.005
EVAL_VERSION = "2-kraken"
COINS = [  # (symbol, Kraken pair)
    ("SOL", "SOLUSD"), ("BTC", "XBTUSD"), ("ETH", "ETHUSD"), ("JUP", "JUPUSD"),
    ("BONK", "BONKUSD"), ("WIF", "WIFUSD"), ("POPCAT", "POPCATUSD"),
]
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC?pair=%s&interval=1440"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker?pair=%s"
RULES = [  # decided before any test was run; classic, widely published trend rules
    ("avg50", "Hold while the price is above its 50-day average"),
    ("avg100", "Hold while the price is above its 100-day average"),
    ("cross", "Hold while the 20-day average is above the 100-day average"),
    ("breakout", "Buy a new 20-day high, sell a new 10-day low"),
    ("mom30", "Hold while the price is above where it was 30 days ago (checked weekly)"),
]
NAMES = dict(RULES)
SCHEMA = """
CREATE TABLE IF NOT EXISTS core_candles (sym TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, PRIMARY KEY (sym, ts));
CREATE TABLE IF NOT EXISTS core_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, mint TEXT, rule TEXT, opened_at INTEGER, entry REAL, size_usd REAL,
  closed_at INTEGER, exit REAL, pnl_usd REAL, pnl_pct REAL, why TEXT
);
"""


def _sma(c, i, n):
    return sum(c[i - n + 1:i + 1]) / n if i >= n - 1 else None


def want_in(rule, c, i, held):
    """Should the coin be held after the close of day i? (c = closes)"""
    if rule == "avg50":
        a = _sma(c, i, 50)
        return a is not None and c[i] > a
    if rule == "avg100":
        a = _sma(c, i, 100)
        return a is not None and c[i] > a
    if rule == "cross":
        a, b = _sma(c, i, 20), _sma(c, i, 100)
        return a is not None and b is not None and a > b
    if rule == "breakout":
        if i < 20:
            return False
        if not held:
            return c[i] > max(c[i - 20:i])
        return not c[i] < min(c[i - 10:i])
    if rule == "mom30":
        if i < 30:
            return False
        if i % 7 and i > 30:                       # weekly check: keep yesterday's answer on other days
            return held
        return c[i] > c[i - 30]
    return True                                    # "hold": buy and hold


def replay(rule, k):
    """k: [(ts, o, h, l, c)] daily. Trades happen at the next day's open. Returns (equity by day {ts: $}, trades)."""
    closes = [x[4] for x in k]
    held, units, cash, entry, trades, eq = False, 0.0, SLEEVE, 0.0, [], {}
    for i in range(len(k)):
        ts, o = k[i][0], k[i][1]
        if i > 0:
            go = want_in(rule, closes, i - 1, held)
            if go and not held and o > 0:
                units, cash, held, entry, t_in = cash * (1 - COST) / o, 0.0, True, o, ts
            elif not go and held:
                cash, held = units * o * (1 - COST), False
                trades.append({"t_in": t_in, "t_out": ts, "ret": o / entry - 1})
                units = 0.0
        eq[ts] = cash + units * k[i][4] * (1 - COST if held else 1)
    if held:
        trades.append({"t_in": t_in, "t_out": None, "ret": k[-1][4] / entry - 1})
    return eq, trades


def portfolio(rule, data, t_from=None, t_to=None):
    """Sum of every coin's sleeve over [t_from, t_to). Each sleeve starts with $1,000 on its first day in the window.
    Returns return %, max drawdown %, trades, final value and money in."""
    days = sorted({ts for k in data.values() for ts in (x[0] for x in k)})
    days = [d for d in days if (t_from is None or d >= t_from) and (t_to is None or d < t_to)]
    if not days:
        return None
    total, start_in, ntr, wins = {d: 0.0 for d in days}, 0.0, 0, 0
    for sym, k in data.items():
        kk = [x for x in k if (t_from is None or x[0] >= t_from) and (t_to is None or x[0] < t_to)]
        if len(kk) < 30:
            continue
        # start each window with the indicators already warmed up: replay the whole history, then rebase
        eq_all, trs = replay(rule, k)
        base_ts = kk[0][0]
        prev = [ts for ts in eq_all if ts < base_ts]
        base = eq_all[prev[-1]] if prev else SLEEVE
        last = None
        for d in days:
            if d in eq_all and d >= base_ts:
                last = eq_all[d] / base * SLEEVE
            if last is not None:
                total[d] += last
            elif d >= base_ts:
                total[d] += SLEEVE
        start_in += SLEEVE
        ins = [t for t in trs if t["t_in"] >= base_ts and (t_to is None or t["t_in"] < t_to)]
        ntr += len(ins)
        wins += sum(1 for t in ins if t["ret"] > 2 * COST)
    if not start_in:
        return None
    vals = [total[d] for d in days if total[d] > 0]
    peak, dd = 0.0, 0.0
    for v in vals:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"ret": round((vals[-1] / start_in - 1) * 100, 1), "dd": round(dd * 100, 1), "trades": ntr,
            "win": round(wins / ntr * 100) if ntr else 0, "end": round(vals[-1], 2), "in": start_in}


def evaluate(data):
    t0 = min(k[0][0] for k in data.values())
    t1 = max(k[-1][0] for k in data.values())
    mid = t0 + (t1 - t0) // 2
    rows = []
    for key, _ in RULES + [("hold", "Buy and hold (reference)")]:
        rows.append({"rule": key, "all": portfolio(key, data), "old": portfolio(key, data, None, mid), "new": portfolio(key, data, mid, None)})
    ref = next(r for r in rows if r["rule"] == "hold")
    cand = [r for r in rows if r["rule"] != "hold" and r["old"] and r["old"]["ret"] > 0]
    # pick on the OLDER half only: best return per unit of drawdown
    pick = max(cand, key=lambda r: r["old"]["ret"] / max(5.0, -r["old"]["dd"]), default=None)
    verdict = "none"
    if pick and pick["new"]:
        n, h = pick["new"], ref["new"]
        if n["ret"] > 0 and (n["ret"] >= h["ret"] or n["dd"] >= h["dd"] * 0.6):
            verdict = "pass"
        elif n["ret"] > 0:
            verdict = "weak"
        else:
            verdict = "fail"
    return {"built": int(time.time() * 1000), "start": t0, "end": t1, "mid": mid, "coins": sorted(data), "rows": rows,
            "pick": pick["rule"] if pick else None, "verdict": verdict}


class Core:
    def __init__(self, runner):
        self.r = runner
        self.con = runner.con
        self.con.executescript(SCHEMA)
        self.missing = []

    async def _get(self, url):
        for attempt in range(3):
            try:
                async with self.r.session.get(url, headers={"accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=25)) as r:
                    if r.status == 429:
                        await asyncio.sleep(20 * (attempt + 1))
                        continue
                    return await r.json(content_type=None) if r.status == 200 else None
            except Exception as e:  # noqa: BLE001
                log.info("Core fetch %s: %s", url[:80], e)
                await asyncio.sleep(3)
        return None

    @staticmethod
    def _kraken_rows(data):
        """Kraken answers {"error": [], "result": {"<its pair name>": [...], "last": ...}}; the pair name varies (XXBTZUSD)."""
        if not data or data.get("error"):
            return None
        res = data.get("result") or {}
        vals = [v for k, v in res.items() if k != "last"]
        return vals[0] if vals else None

    async def download(self):
        """Refresh daily candles for every coin. Returns {sym: [(ts,o,h,l,c)]} with yesterday's candle as the last one."""
        if not db.kv_get(self.con, "core_src_kraken"):
            self.con.execute("DELETE FROM core_candles")          # drop the short GeckoTerminal history from the first day
            db.kv_set(self.con, "core_src_kraken", 1)
        self.missing = []
        for sym, pair in COINS:
            rows = self._kraken_rows(await self._get(KRAKEN_OHLC % pair))
            if not rows:
                self.missing.append(sym)
                log.info("Core: no Kraken history for %s", sym)
                continue
            self.con.executemany("INSERT OR REPLACE INTO core_candles VALUES(?,?,?,?,?,?)",
                                 [(sym, int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in rows if float(x[4] or 0) > 0])
            await asyncio.sleep(1.5)
        return self.load()

    def load(self, con=None):
        today = int(time.time() // 86400) * DAY
        out = {}
        for sym, _ in COINS:
            k = [tuple(x) for x in (con or self.con).execute("SELECT ts, o, h, l, c FROM core_candles WHERE sym=? AND ts<? ORDER BY ts", (sym, today))]
            if len(k) >= 120:                      # needs the 100-day average plus some history
                out[sym] = k
        return out

    def result(self, con=None):
        try:
            return json.loads(db.kv_get(con or self.con, "core_eval") or "null")
        except ValueError:
            return None

    async def rebuild(self, announce=True):
        data = await self.download()
        if not data:
            return None
        res = await asyncio.to_thread(evaluate, data)
        old = self.result()
        db.kv_set(self.con, "core_eval", json.dumps(res))
        if announce and old and old.get("pick") != res.get("pick"):
            await notify.send(self.r.session, "🧭 <b>Core changed its rule</b> after the weekly retest: %s → %s." % (
                NAMES.get(old.get("pick"), "none"), NAMES.get(res.get("pick"), "none")))
        return res

    def open_trades(self, con=None):
        return [dict(x) for x in (con or self.con).execute("SELECT * FROM core_trades WHERE closed_at IS NULL")]

    async def daily(self):
        """After the daily close: hold or don't hold each coin under the picked rule."""
        res = self.result()
        rule = (res or {}).get("pick")
        data = self.load()
        if not rule or not data:
            return
        # one ticker call per coin: Kraken rejects a whole multi-coin request if any one pair name is unknown
        now = int(time.time() * 1000)
        held = {t["sym"]: t for t in self.open_trades()}
        for sym, pair in COINS:
            k = data.get(sym)
            if not k:
                continue
            hit = self._kraken_rows(await self._get(KRAKEN_TICKER % pair)) or {}   # same answer shape as the candles
            try:
                px = float(hit.get("c", [0])[0] or 0)
            except (TypeError, ValueError, AttributeError):
                px = 0.0
            mint = pair
            if not k or not px:
                continue
            closes = [x[4] for x in k]
            # rule switched on Sunday: positions opened under the old rule are judged by the new one from now on
            go = want_in(rule, closes, len(closes) - 1, sym in held)
            if go and sym not in held:
                why = "%s (daily close $%s)" % (NAMES[rule], _px(closes[-1]))
                self.con.execute("INSERT INTO core_trades(sym, mint, rule, opened_at, entry, size_usd, why) VALUES(?,?,?,?,?,?,?)",
                                 (sym, mint, rule, now, px, SLEEVE, why))
                await notify.send(self.r.session, "🧭 <b>CORE PAPER BUY</b> %s · $%d at $%s\n%s" % (sym, SLEEVE, _px(px), why))
            elif not go and sym in held:
                t = held[sym]
                net = (px / t["entry"]) * (1 - COST) / (1 + COST) - 1
                self.con.execute("UPDATE core_trades SET closed_at=?, exit=?, pnl_usd=?, pnl_pct=? WHERE id=?",
                                 (now, px, SLEEVE * net, net * 100, t["id"]))
                await notify.send(self.r.session, "%s <b>CORE PAPER SELL</b> %s at $%s: %+.1f%% ($%+.2f) after %.0f days\nThe rule says step aside: %s" % (
                    "✅" if net > 0 else "🔴", sym, _px(px), net * 100, SLEEVE * net, (now - t["opened_at"]) / DAY, NAMES[rule].lower()))

    async def loop(self):
        await asyncio.sleep(120)
        last_day = db.kv_get(self.con, "core_last_day")
        while True:
            try:
                now = datetime.now(timezone.utc)
                # EVAL_VERSION: bump it whenever the test itself changes, so the stored result is redone at once
                # (the Kraken switch on Oct 21 waited for Sunday because of this)
                stale = db.kv_get(self.con, "core_eval_version") != EVAL_VERSION
                if stale or not self.result() or (now.weekday() == 6 and now.hour == 1 and db.kv_get(self.con, "core_rebuilt") != now.strftime("%Y-%m-%d")):
                    await self.rebuild(announce=bool(self.result()))
                    db.kv_set(self.con, "core_rebuilt", now.strftime("%Y-%m-%d"))
                    db.kv_set(self.con, "core_eval_version", EVAL_VERSION)
                day = now.strftime("%Y-%m-%d")
                if now.hour >= 0 and now.minute >= 10 and last_day != day:
                    await self.download()           # yesterday's candle is final now
                    await self.daily()
                    last_day = day
                    db.kv_set(self.con, "core_last_day", day)
            except Exception:  # noqa: BLE001
                log.exception("Core update failed")
            await asyncio.sleep(600)

    # ---- report
    def text(self, con=None):
        con = con or self.con
        res = self.result(con)
        L = ["🧭 <b>Core</b> (version 2): trend-following on big, liquid coins (%s). $%d of paper money per coin, 0.5%% cost per side." % (
            ", ".join(s for s, _ in COINS), SLEEVE)]
        if not res:
            return L[0] + "\nFirst history download and test haven't finished yet (a few minutes after the bot starts)."
        f = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b %-d %Y")  # noqa: E731
        if getattr(self, "missing", None):
            L.append("No Kraken history for: %s (left out)." % ", ".join(self.missing))
        L.append("Tested on %s to %s (%d coins). Rule picked on the older half only; the newer half (from %s) is the honest score.\n" % (
            f(res["start"]), f(res["end"]), len(res["coins"]), f(res["mid"])))
        L.append("Rule: return older half / newer half · worst drop from a high (newer half) · trades")
        for r in res["rows"]:
            o, n = r["old"] or {}, r["new"] or {}
            name = NAMES.get(r["rule"], "Buy and hold (reference)")
            L.append("%s%s: %+.0f%% / %+.0f%% · %.0f%% · %d" % (
                "👉 " if r["rule"] == res.get("pick") else "", name, o.get("ret", 0), n.get("ret", 0), n.get("dd", 0), (r["all"] or {}).get("trades", 0)))
        v = {"pass": "✅ Passed: the picked rule made money on the newer half and beat buy-and-hold on return or held up with a much smaller drop.",
             "weak": "⚠️ Weak: the picked rule made money on the newer half but didn't beat buy-and-hold on return or drawdown.",
             "fail": "❌ Failed: the picked rule lost money on the newer half.",
             "none": "❌ No rule made money even on the older half."}[res["verdict"]]
        L.append("\n" + v)
        L.append("Live paper rule: %s" % NAMES.get(res.get("pick"), "none"))
        opens = self.open_trades(con)
        closed = [dict(x) for x in con.execute("SELECT * FROM core_trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC")]
        if opens:
            L.append("\n<b>Holding now</b>: " + ", ".join("%s (bought %s)" % (t["sym"], f(t["opened_at"])) for t in opens))
        else:
            L.append("\nHolding nothing right now: the rule says every coin is in a downtrend.")
        if closed:
            w = sum(1 for t in closed if t["pnl_usd"] > 0)
            L.append("Closed: %d trades · %d won · $%+.2f" % (len(closed), w, sum(t["pnl_usd"] for t in closed)))
            for t in closed[:6]:
                L.append("%s: %+.1f%% ($%+.2f)" % (t["sym"], t["pnl_pct"], t["pnl_usd"]))
        L.append("Retested every Sunday on the latest history. Slow by design: a few trades a month.")
        return "\n".join(L)


def _px(x):
    return ("%.2f" % x) if x >= 1 else ("%.6f" % x).rstrip("0")
