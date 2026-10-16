"""Guard: watches the coins you really own and tells you when something matters.

You tell it what you hold (no wallet, no keys: just the coin, how much you put in and the market cap you bought at).
Every 5 minutes it checks each coin and sends a Telegram alert when:
  - it reaches a market-cap target you set
  - it crosses your break-even (the cap you bought at), either way
  - it has moved 20% since the last move alert (so you hear about every 20% step, not every wiggle)
  - the pool shrinks 30%+ within about an hour (the classic sign of liquidity being pulled)
  - sellers swamp buyers: twice as many sells as buys over the hour, with the price down 10%+
  - the top 10 holders cut their share by 5+ points (big holders selling), or a new danger flag appears

Commands:
  /own COIN AMOUNT CAP   e.g. /own SI 1800 29M   (COIN can be $TICKER, a contract address or a link)
  /own COIN AMOUNT       uses today's cap as your entry
  /own                   what you hold now: value, profit or loss, targets
  /target COIN 50M 100M  alert when the cap reaches these (replaces earlier targets)
  /unown COIN            stop watching it
"""
import asyncio
import html
import json
import logging
import re
import time

from . import check, notify
from .sources import DexFailed, dexscreener_batch, safety_check

log = logging.getLogger("gatekeeper.guard")
MIN, HOUR = 60000, 3600000
SCHEMA = """
CREATE TABLE IF NOT EXISTS owned (
  mint TEXT PRIMARY KEY, chain TEXT, symbol TEXT, amount_usd REAL, entry_cap REAL, added_at INTEGER, targets TEXT,
  hit TEXT, move_ref REAL, side TEXT, liq_hist TEXT, top10 REAL, danger TEXT, safety_ts INTEGER, last TEXT, alerted TEXT
);
"""


def parse_usd(s):
    """'29M' -> 29e6, '1.2b' -> 1.2e9, '500k' -> 5e5, '$1,800' -> 1800. None if it isn't a number."""
    m = re.fullmatch(r"\$?([\d,]*\.?\d+)\s*([kmb]?)", (s or "").strip().lower())
    if not m:
        return None
    x = float(m.group(1).replace(",", ""))
    return x * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2)]


def km(x):
    x = float(x or 0)
    return "$%.2fB" % (x / 1e9) if x >= 1e9 else "$%.1fM" % (x / 1e6) if x >= 1e6 else "$%.0fK" % (x / 1e3)


def money(x):
    return "$%s" % format(round(x), ",")


class Guard:
    def __init__(self, runner):
        self.r = runner
        self.con = runner.con
        self.con.executescript(SCHEMA)

    def rows(self):
        return [dict(x) for x in self.con.execute("SELECT * FROM owned ORDER BY added_at")]

    def _find(self, arg):
        a = (arg or "").strip().lstrip("$").lower()
        for r in self.rows():
            if r["mint"].lower() == a or (r["symbol"] or "").lower() == a:
                return r
        return None

    @staticmethod
    def cap_of(pair):
        return float(pair.get("marketCap") or pair.get("fdv") or 0)

    # ---- commands
    async def own(self, arg):
        parts = (arg or "").split()
        if not parts:
            return await self.text()
        coin, amount, cap = parts[0], parse_usd(parts[1]) if len(parts) > 1 else None, parse_usd(parts[2]) if len(parts) > 2 else None
        if amount is None:
            return "Send it like this: /own SI 1800 29M (the coin, the dollars you put in, and the market cap you bought at)."
        addr, chain, pair, note = await check.resolve(self.r.session, coin)
        if not addr:
            return "🛡️ " + note
        now_cap = self.cap_of(pair)
        if not now_cap:
            return "🛡️ I found the coin but can't read its market cap right now. Try again in a few minutes."
        entry = cap or now_cap
        sym = (pair.get("baseToken") or {}).get("symbol") or "?"
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        old = self.con.execute("SELECT targets FROM owned WHERE mint=?", (addr,)).fetchone()
        self.con.execute("INSERT OR REPLACE INTO owned(mint, chain, symbol, amount_usd, entry_cap, added_at, targets, hit, move_ref, side, liq_hist, alerted) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (addr, chain, sym, amount, entry, int(time.time() * 1000), old["targets"] if old else "[]", "[]", now_cap,
                          "up" if now_cap >= entry else "down", json.dumps([[int(time.time() * 1000), liq]]), "{}"))
        value = amount * now_cap / entry
        return ("🛡️ Watching $%s for you.\nYou put in %s at a %s cap. It's at %s now, so your position is worth about %s (%+.0f%%).\n"
                "I'll alert you on break-even, every 20%% move, pool drains, heavy selling and big holders selling. "
                "Add targets with /target %s 50M 100M.%s" % (
                    html.escape(sym), money(amount), km(entry), km(now_cap), money(value), (now_cap / entry - 1) * 100,
                    html.escape(sym), ("\n" + note) if note else ""))

    async def target(self, arg):
        parts = (arg or "").split()
        r = self._find(parts[0]) if parts else None
        if not r:
            return "Send /target COIN 50M 100M for a coin you've added with /own."
        ts = sorted(t for t in (parse_usd(x) for x in parts[1:]) if t)
        if not ts:
            return "Add at least one market cap, like /target %s 50M 100M." % html.escape(r["symbol"])
        self.con.execute("UPDATE owned SET targets=?, hit='[]' WHERE mint=?", (json.dumps(ts), r["mint"]))
        lines = ["%s → your position about %s" % (km(t), money(r["amount_usd"] * t / r["entry_cap"])) for t in ts]
        return "🎯 Targets for $%s:\n%s" % (html.escape(r["symbol"]), "\n".join(lines))

    async def unown(self, arg):
        r = self._find(arg)
        if not r:
            return "I'm not watching that coin. Send /own to see what I am watching."
        self.con.execute("DELETE FROM owned WHERE mint=?", (r["mint"],))
        return "🛡️ Stopped watching $%s." % html.escape(r["symbol"])

    async def text(self):
        rows = self.rows()
        if not rows:
            return ("🛡️ <b>Guard</b>: not watching anything yet.\nTell me what you hold: /own SI 1800 29M "
                    "(coin, dollars in, market cap you bought at). No wallet or keys needed.")
        by_chain = {}
        for r in rows:
            by_chain.setdefault(r["chain"] or "solana", []).append(r["mint"])
        pairs = {}
        for ch, mints in by_chain.items():
            got = await dexscreener_batch(self.r.session, mints, ch)
            if not isinstance(got, DexFailed):
                pairs.update(got)
        L, tin, tnow = ["🛡️ <b>Guard</b>: the coins you hold"], 0, 0
        for r in rows:
            p = pairs.get(r["mint"])
            cap = self.cap_of(p) if p else 0
            tin += r["amount_usd"]
            if not cap:
                L.append("$%s · %s in at %s · no price right now" % (html.escape(r["symbol"]), money(r["amount_usd"]), km(r["entry_cap"])))
                tnow += r["amount_usd"]
                continue
            v = r["amount_usd"] * cap / r["entry_cap"]
            tnow += v
            tg = json.loads(r["targets"] or "[]")
            L.append("$%s · cap %s (you bought at %s) · %s in, worth about %s (%+.0f%%)%s" % (
                html.escape(r["symbol"]), km(cap), km(r["entry_cap"]), money(r["amount_usd"]), money(v), (cap / r["entry_cap"] - 1) * 100,
                " · targets " + ", ".join(km(t) for t in tg) if tg else ""))
        if len(rows) > 1:
            L.append("\nTotal: %s in, worth about %s (%+.0f%%)" % (money(tin), money(tnow), (tnow / tin - 1) * 100 if tin else 0))
        L.append("Values assume the coin's supply hasn't changed and leave out selling costs.")
        return "\n".join(L)

    def summary(self, con=None):
        """For the stats feed: which coins, never amounts (the feed is a file on GitHub). The feed passes its own connection."""
        rows = [dict(x) for x in (con or self.con).execute("SELECT * FROM owned ORDER BY added_at")]
        return "Guarding %d coin(s): %s" % (len(rows), ", ".join("$" + (r["symbol"] or "?") for r in rows)) if rows else "Not guarding any coins."

    # ---- watching
    async def _alert(self, r, kind, msg, every_h, now):
        al = json.loads(r.get("alerted") or "{}")
        if now - al.get(kind, 0) < every_h * HOUR:
            return
        al[kind] = now
        r["alerted"] = json.dumps(al)
        self.con.execute("UPDATE owned SET alerted=? WHERE mint=?", (r["alerted"], r["mint"]))
        await notify.send(self.r.session, "🛡️ <b>$%s</b>: %s\n%s" % (html.escape(r["symbol"]), msg, notify.dex_link(r["mint"], r["chain"])))

    async def check_one(self, r, p, now):
        cap = self.cap_of(p)
        if not cap:
            return
        entry, amt = r["entry_cap"], r["amount_usd"]
        val = lambda c: money(amt * c / entry)  # noqa: E731
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        tx = (p.get("txns") or {}).get("h1") or {}
        buys, sells = int(tx.get("buys") or 0), int(tx.get("sells") or 0)
        pc1 = float((p.get("priceChange") or {}).get("h1") or 0)
        # targets
        hit = json.loads(r["hit"] or "[]")
        for t in json.loads(r["targets"] or "[]"):
            if cap >= t and t not in hit:
                hit.append(t)
                self.con.execute("UPDATE owned SET hit=? WHERE mint=?", (json.dumps(hit), r["mint"]))
                await notify.send(self.r.session, "🎯 <b>$%s hit your %s target</b>. Cap %s, your position is worth about %s.\n%s" % (
                    html.escape(r["symbol"]), km(t), km(cap), val(cap), notify.dex_link(r["mint"], r["chain"])))
        # break-even
        side = "up" if cap >= entry else "down"
        if side != r["side"]:
            self.con.execute("UPDATE owned SET side=? WHERE mint=?", (side, r["mint"]))
            await self._alert(r, "be_" + side, ("back above your entry (%s). Cap %s, worth about %s." if side == "up" else
                                                "fell below your entry (%s). Cap %s, worth about %s.") % (km(entry), km(cap), val(cap)), 6, now)
        # every 20% step
        ref = r["move_ref"] or cap
        if abs(cap / ref - 1) >= 0.20:
            self.con.execute("UPDATE owned SET move_ref=? WHERE mint=?", (cap, r["mint"]))
            await notify.send(self.r.session, "%s <b>$%s</b> moved %+.0f%% to a %s cap. Your position: about %s (%+.0f%% overall).\n%s" % (
                "📈" if cap > ref else "📉", html.escape(r["symbol"]), (cap / ref - 1) * 100, km(cap), val(cap), (cap / entry - 1) * 100,
                notify.dex_link(r["mint"], r["chain"])))
        # pool draining: compare with the pool about an hour ago
        hist = [x for x in json.loads(r["liq_hist"] or "[]") if now - x[0] <= 2 * HOUR] + [[now, liq]]
        self.con.execute("UPDATE owned SET liq_hist=? WHERE mint=?", (json.dumps(hist[-30:]), r["mint"]))
        old = [x[1] for x in hist if 45 * MIN <= now - x[0] <= 90 * MIN and x[1] > 0]
        if old and liq < max(old) * 0.7:
            await self._alert(r, "pool", "⚠️ the pool shrank %.0f%% in about an hour (%s → %s). That's how rugs start. Consider getting out." % (
                (1 - liq / max(old)) * 100, km(max(old)), km(liq)), 3, now)
        # sellers swamping
        if sells >= 50 and sells >= 2 * buys and pc1 <= -10:
            await self._alert(r, "selling", "heavy selling: %d sells vs %d buys in the last hour, price %+.0f%%. Cap %s, worth about %s." % (
                sells, buys, pc1, km(cap), val(cap)), 3, now)
        # big holders, every 30 minutes (Solana only: the free holder check covers Solana)
        if (r["chain"] or "solana") == "solana" and now - (r["safety_ts"] or 0) >= 30 * MIN:
            try:
                s = await safety_check(self.r.session, "solana", r["mint"])
            except Exception:  # noqa: BLE001
                s = None
            if s:
                t10, dg = s.get("top10"), s.get("danger") or ""
                if t10 is not None and r["top10"] is not None and t10 <= r["top10"] - 5:
                    await self._alert(r, "holders", "big holders are selling: the top 10 wallets went from %.0f%% to %.0f%% of the supply." % (r["top10"], t10), 6, now)
                if dg and not r["danger"] and r["safety_ts"]:         # new since the last check, not there from the start
                    await self._alert(r, "danger", "a new safety warning appeared: %s" % html.escape(str(dg)[:150]), 12, now)
                self.con.execute("UPDATE owned SET top10=?, danger=?, safety_ts=? WHERE mint=?",
                                 (t10 if t10 is not None else r["top10"], dg, now, r["mint"]))

    async def loop(self):
        await asyncio.sleep(90)
        while True:
            now = int(time.time() * 1000)
            try:
                rows = self.rows()
                by_chain = {}
                for r in rows:
                    by_chain.setdefault(r["chain"] or "solana", []).append(r)
                for ch, rs in by_chain.items():
                    got = await dexscreener_batch(self.r.session, [r["mint"] for r in rs], ch)
                    if isinstance(got, DexFailed):
                        continue                       # the price feed didn't answer: no alerts on missing data
                    for r in rs:
                        if got.get(r["mint"]):
                            try:
                                await self.check_one(r, got[r["mint"]], now)
                            except Exception:  # noqa: BLE001
                                log.exception("Guard check failed for %s", r["symbol"])
            except Exception:  # noqa: BLE001
                log.exception("Guard update failed")
            await asyncio.sleep(300)
