"""Hold bot: bigger paper bets on established coins, held for days.

Main trades coins that graduated hours ago. This is the opposite end: coins that already made it (a market cap
of $10M or more, a deep pool, at least 5 days old, thousands of real buyers) and are in an uptrend. It bets $300,
uses a wide trailing stop instead of a profit target, and holds for up to a week.

It keeps its own watchlist and its own trades, apart from Main and the experiment bots. Paper only.
There is no recorded history for these coins yet, so nothing here has been tested: the paper trades ARE the test.
"""
import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime

import aiohttp

from . import notify
from .sources import dexscreener_batch, safety_check

log = logging.getLogger("gatekeeper.hold")
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/%s?page=%d&include=base_token"
MIN, DAY = 60000, 86400000


def _env(name, default):
    return float(os.environ.get("GK_HOLD_" + name, default))


P = {
    "BET": _env("BET", 300), "MAX_OPEN": int(_env("MAX_OPEN", 3)),
    "MIN_CAP": _env("MIN_CAP", 10e6), "MAX_CAP": _env("MAX_CAP", 150e6),     # above this it's a major, not a meme play
    "MIN_LIQ": _env("MIN_LIQ", 300000), "MIN_AGE_DAYS": _env("MIN_AGE_DAYS", 5), "MIN_BUYERS": _env("MIN_BUYERS", 300),
    "MAX_TOP10": _env("MAX_TOP10", 30),
    "UP24_MIN": _env("UP24_MIN", 3), "UP24_MAX": _env("UP24_MAX", 50),       # rising, but not a one-day spike
    "STOP": _env("STOP", 20), "TRAIL_START": _env("TRAIL_START", 25), "TRAIL": _env("TRAIL", 25),
    "MAX_DAYS": _env("MAX_DAYS", 7), "COOLDOWN_DAYS": _env("COOLDOWN_DAYS", 3),
    "COST": _env("COST", 1.5),                                                # % per side: fee plus slippage on a deep pool
}
SKIP = {"SOL", "WSOL", "USDC", "USDT", "USDG", "USDS", "PYUSD", "JUP", "JTO", "RAY", "ORCA", "JITOSOL", "MSOL", "BSOL",
        "WBTC", "CBBTC", "WETH", "ETH", "BTC", "JLP", "PYTH", "W", "RENDER", "HNT", "USD1", "FDUSD", "EURC"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS hold_snaps (
  mint TEXT, ts INTEGER, symbol TEXT, price REAL, fdv REAL, liq REAL, pc1h REAL, pc6h REAL, pc24h REAL,
  buys24 INTEGER, sells24 INTEGER, buyers24 INTEGER, sellers24 INTEGER, vol24 REAL
);
CREATE INDEX IF NOT EXISTS hold_snaps_mint ON hold_snaps(mint, ts);
CREATE TABLE IF NOT EXISTS hold_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, opened_at INTEGER, entry_price REAL, entry_fdv REAL,
  entry_liq REAL, size_usd REAL, peak REAL, why TEXT, closed_at INTEGER, exit_price REAL, pnl_usd REAL, pnl_pct REAL,
  exit_reason TEXT
);
"""


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def km(x):
    x = float(x or 0)
    return "$%.1fM" % (x / 1e6) if x >= 1e6 else "$%.0fK" % (x / 1e3)


def parse(data, now):
    """GeckoTerminal pool list -> {mint: coin}, keeping each coin's deepest pool."""
    inc = {x["id"]: x.get("attributes") or {} for x in (data or {}).get("included") or []}
    out = {}
    for p in (data or {}).get("data") or []:
        a = p.get("attributes") or {}
        try:
            b = inc.get(p["relationships"]["base_token"]["data"]["id"]) or {}
        except (KeyError, TypeError):
            continue
        mint, sym = b.get("address"), (b.get("symbol") or "").strip()
        if not mint or not sym:
            continue
        try:
            created = datetime.fromisoformat((a.get("pool_created_at") or "").replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            created = now
        pc, tx = a.get("price_change_percentage") or {}, (a.get("transactions") or {}).get("h24") or {}
        c = {"mint": mint, "symbol": sym, "name": b.get("name") or "", "price": _f(a.get("base_token_price_usd")),
             "fdv": _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")), "liq": _f(a.get("reserve_in_usd")),
             "age_days": (now - created) / DAY, "pc1h": _f(pc.get("h1")), "pc6h": _f(pc.get("h6")), "pc24h": _f(pc.get("h24")),
             "buys24": int(tx.get("buys") or 0), "sells24": int(tx.get("sells") or 0),
             "buyers24": int(tx.get("buyers") or 0), "sellers24": int(tx.get("sellers") or 0),
             "vol24": _f((a.get("volume_usd") or {}).get("h24"))}
        if mint not in out or c["liq"] > out[mint]["liq"]:
            if mint in out:
                c["age_days"] = max(c["age_days"], out[mint]["age_days"])      # the coin is as old as its oldest pool
            out[mint] = c
        else:
            out[mint]["age_days"] = max(out[mint]["age_days"], c["age_days"])
    return out


def established(c):
    """Is this a coin that already made it? (the watchlist test)"""
    if re.fullmatch(r"[A-Z0-9]{2,6}x", c["symbol"]):          # tokenized stocks (TSLAx, NVDAx): not what this bot trades
        return False
    return (c["symbol"].upper() not in SKIP and c["price"] > 0
            and P["MIN_CAP"] <= c["fdv"] <= P["MAX_CAP"] and c["liq"] >= P["MIN_LIQ"]
            and c["liq"] >= c["fdv"] * 0.005                      # a pool far too thin for its cap means a fake cap
            and c["age_days"] >= P["MIN_AGE_DAYS"] and c["buyers24"] >= P["MIN_BUYERS"])


def trend_fails(c):
    f = []
    if not (P["UP24_MIN"] <= c["pc24h"] <= P["UP24_MAX"]):
        f.append("24h %+.0f%% (wants +%d%% to +%d%%)" % (c["pc24h"], P["UP24_MIN"], P["UP24_MAX"]))
    if c["pc6h"] < 0:
        f.append("down %.0f%% over 6h" % -c["pc6h"])
    if c["pc1h"] < -5:
        f.append("falling now (%.0f%% in 1h)" % c["pc1h"])
    if c["buyers24"] <= c["sellers24"]:
        f.append("more sellers than buyers today (%d vs %d)" % (c["sellers24"], c["buyers24"]))
    return f


class Hold:
    def __init__(self, runner):
        self.r = runner
        self.con = runner.con
        self.con.executescript(SCHEMA)
        self.watch = {}          # mint -> latest coin reading
        self.seen = {}           # mint -> scans in a row it qualified
        self.safety = {}         # mint -> (ts, report)
        self.last_scan = 0

    # ---- data
    async def fetch(self):
        now = int(time.time() * 1000)
        coins = {}
        for kind, pages in (("trending_pools", 3), ("pools", 5)):
            for page in range(1, pages + 1):
                url = GECKO % (kind, page) + ("&sort=h24_volume_usd_desc" if kind == "pools" else "")
                try:
                    async with self.r.session.get(url, headers={"accept": "application/json"},
                                                  timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status == 200:
                            for m, c in parse(await resp.json(content_type=None), now).items():
                                if m not in coins or c["liq"] > coins[m]["liq"]:
                                    coins[m] = c
                        else:
                            log.info("GeckoTerminal %s page %d: %s", kind, page, resp.status)
                except Exception as e:  # noqa: BLE001
                    log.info("GeckoTerminal fetch failed: %s", e)
                await asyncio.sleep(2.5)
        return coins

    async def safe(self, mint):
        """(ok, why). Cached for 6 hours."""
        now = time.time()
        hit = self.safety.get(mint)
        if not hit or now - hit[0] > 6 * 3600:
            try:
                hit = (now, await safety_check(self.r.session, "solana", mint))
            except Exception:  # noqa: BLE001
                hit = (now, None)
            self.safety[mint] = hit
        s = hit[1]
        if not s:
            return False, "safety check unavailable"
        if not s.get("mint_revoked"): return False, "dev can still print tokens"
        if not s.get("freeze_revoked"): return False, "dev can freeze tokens"
        if s.get("danger"): return False, "danger flag: %s" % str(s["danger"])[:60]
        if not s.get("lp_na") and (s.get("lp_locked") or 0) < 90: return False, "liquidity only %.0f%% locked" % (s.get("lp_locked") or 0)
        if s.get("top10") is not None and s["top10"] > P["MAX_TOP10"]: return False, "top 10 holders own %.0f%%" % s["top10"]
        return True, "top 10 hold %s%%" % ("%.0f" % s["top10"] if s.get("top10") is not None else "?")

    def open_trades(self):
        return [dict(x) for x in self.con.execute("SELECT * FROM hold_trades WHERE closed_at IS NULL ORDER BY opened_at")]

    # ---- trading
    async def close(self, t, price, reason, now):
        cost = P["COST"] / 100
        gross = price / t["entry_price"] if t["entry_price"] else 0
        net = gross * (1 - cost) / (1 + cost) - 1
        pnl = t["size_usd"] * net
        self.con.execute("UPDATE hold_trades SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=? WHERE id=?",
                         (now, price, pnl, net * 100, reason, t["id"]))
        await notify.send(self.r.session, "%s <b>PAPER SELL</b> [Hold] $%s: %+.0f%% ($%+.2f) after %.1f days\n%s\n%s" % (
            "✅" if pnl > 0 else "🔴", html.escape(t["symbol"]), net * 100, pnl, (now - t["opened_at"]) / DAY,
            html.escape(reason), notify.dex_link(t["mint"], "solana")))

    async def manage(self, now):
        opens = self.open_trades()
        if not opens:
            return
        pairs = await dexscreener_batch(self.r.session, [t["mint"] for t in opens], "solana")
        for t in opens:
            pair = pairs.get(t["mint"]) or {}
            price = _f(pair.get("priceUsd")) or (self.watch.get(t["mint"]) or {}).get("price") or 0
            liq = _f((pair.get("liquidity") or {}).get("usd")) or (self.watch.get(t["mint"]) or {}).get("liq") or 0
            if not price:
                continue
            peak = max(t["peak"] or t["entry_price"], price)
            if peak != t["peak"]:
                self.con.execute("UPDATE hold_trades SET peak=? WHERE id=?", (peak, t["id"]))
            entry = t["entry_price"]
            best = (peak / entry - 1) * 100
            if liq and t["entry_liq"] and liq < t["entry_liq"] * 0.6:
                await self.close(t, price, "Pool shrank to %s from %s at entry" % (km(liq), km(t["entry_liq"])), now)
            elif price <= entry * (1 - P["STOP"] / 100):
                await self.close(t, price, "Stop loss (-%d%%)" % P["STOP"], now)
            elif best >= P["TRAIL_START"] and price <= peak * (1 - P["TRAIL"] / 100):
                await self.close(t, price, "Trailing stop (was up %.0f%%)" % best, now)
            elif now - t["opened_at"] >= P["MAX_DAYS"] * DAY:
                await self.close(t, price, "Held the full %d days" % P["MAX_DAYS"], now)

    async def scan(self, now):
        coins = await self.fetch()
        if not coins:
            return
        watch = {m: c for m, c in coins.items() if established(c)}
        self.seen = {m: self.seen.get(m, 0) + 1 for m in watch}
        self.watch, self.last_scan = watch, now
        self.con.executemany(
            "INSERT INTO hold_snaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(c["mint"], now, c["symbol"], c["price"], c["fdv"], c["liq"], c["pc1h"], c["pc6h"], c["pc24h"],
              c["buys24"], c["sells24"], c["buyers24"], c["sellers24"], c["vol24"]) for c in watch.values()])
        self.con.execute("DELETE FROM hold_snaps WHERE ts<?", (now - 30 * DAY,))
        held = {t["mint"] for t in self.open_trades()}
        slots = P["MAX_OPEN"] - len(held)
        recent = {x["mint"] for x in self.con.execute("SELECT mint FROM hold_trades WHERE opened_at>?", (now - P["COOLDOWN_DAYS"] * DAY,))}
        # strongest steady climbers first
        for c in sorted(watch.values(), key=lambda c: -c["pc24h"]):
            if slots <= 0:
                break
            if c["mint"] in held or c["mint"] in recent or self.seen.get(c["mint"], 0) < 2 or trend_fails(c):
                continue
            ok, why_safe = await self.safe(c["mint"])
            if not ok:
                c["blocked"] = why_safe
                continue
            why = "cap %s, pool %s, %.0f days old, %+.0f%% in 24h and %+.0f%% in 6h, %d buyers vs %d sellers today, %s" % (
                km(c["fdv"]), km(c["liq"]), c["age_days"], c["pc24h"], c["pc6h"], c["buyers24"], c["sellers24"], why_safe)
            self.con.execute("INSERT INTO hold_trades(mint,symbol,opened_at,entry_price,entry_fdv,entry_liq,size_usd,peak,why) VALUES(?,?,?,?,?,?,?,?,?)",
                             (c["mint"], c["symbol"], now, c["price"], c["fdv"], c["liq"], P["BET"], c["price"], why))
            slots -= 1
            await notify.send(self.r.session, "🟢 <b>PAPER BUY</b> [Hold] $%s · $%d\n%s\nPlan: stop at -%d%%, trail %d%% under the high once up %d%%, hold up to %d days\n%s" % (
                html.escape(c["symbol"]), P["BET"], html.escape(why), P["STOP"], P["TRAIL"], P["TRAIL_START"], P["MAX_DAYS"],
                notify.dex_link(c["mint"], "solana")))

    async def loop(self):
        await asyncio.sleep(150)
        every = _env("MIN", 5)
        n = 0
        while True:
            now = int(time.time() * 1000)
            try:
                await self.manage(now)
                if n % 3 == 0:                 # prices on open trades every 5 min, a full scan every 15
                    await self.scan(now)
            except Exception:  # noqa: BLE001
                log.exception("Hold bot update failed")
            n += 1
            await asyncio.sleep(every * 60)

    # ---- report
    def text(self):
        now = int(time.time() * 1000)
        L = ["💎 <b>Hold bot</b> (paper, $%d a trade): established coins, held for days" % P["BET"],
             "Buys coins with a cap of %s or more, a pool of %s+, %d+ days old and climbing. Stop -%d%%, then a %d%% trailing stop. Up to %d days." % (
                 km(P["MIN_CAP"]), km(P["MIN_LIQ"]), P["MIN_AGE_DAYS"], P["STOP"], P["TRAIL"], P["MAX_DAYS"])]
        closed = [dict(x) for x in self.con.execute("SELECT * FROM hold_trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC")]
        if closed:
            wins = sum(1 for t in closed if t["pnl_usd"] > 0)
            L.append("\nClosed: %d trades · %d%% win · $%+.2f" % (len(closed), wins * 100 // len(closed), sum(t["pnl_usd"] for t in closed)))
            for t in closed[:8]:
                L.append("$%s: %+.0f%% ($%+.2f) · %s" % (html.escape(t["symbol"]), t["pnl_pct"], t["pnl_usd"], html.escape(t["exit_reason"] or "")))
        else:
            L.append("\nNo closed trades yet.")
        opens = self.open_trades()
        if opens:
            L.append("\n<b>Holding now</b>")
            for t in opens:
                cur = (self.watch.get(t["mint"]) or {}).get("price")
                move = " · %+.0f%% since entry" % ((cur / t["entry_price"] - 1) * 100) if cur and t["entry_price"] else ""
                L.append("$%s · bought at a %s cap · %.1f days ago%s" % (html.escape(t["symbol"]), km(t["entry_fdv"]), (now - t["opened_at"]) / DAY, move))
        if not self.last_scan:
            L.append("\nFirst scan hasn't run yet (it starts a few minutes after the bot does).")
            return "\n".join(L)
        L.append("\n<b>Watchlist</b>: %d established coins (scanned %d min ago)" % (len(self.watch), (now - self.last_scan) / MIN))
        for c in sorted(self.watch.values(), key=lambda c: -c["fdv"])[:12]:
            fails = trend_fails(c)
            verdict = c.get("blocked") or ("; ".join(fails[:2]) if fails else "in an uptrend")
            L.append("$%s · cap %s · pool %s · 24h %+.0f%% · %s" % (html.escape(c["symbol"]), km(c["fdv"]), km(c["liq"]), c["pc24h"], html.escape(verdict)))
        L.append("\nUntested: there is no recorded history for these coins yet, so the paper trades are the test.")
        return "\n".join(L)
