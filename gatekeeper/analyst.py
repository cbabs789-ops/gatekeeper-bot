"""AI trader: a paper bot that judges coins the way a person would.

The other bots follow fixed rules. This one hands each candidate to an AI model (Claude) along with what a
trader would look at: the candle chart, the pool, who holds it, the dev's record, what your Fomo traders did,
the bot's own rug and win scores. It answers buy or pass, with a written reason, a stop and how long to hold.
While it holds a coin it re-reads the chart and decides to hold, tighten the stop or sell.

Where its candidates come from:
  1. every coin Main buys (a second opinion: does judgment avoid the bad ones?)
  2. established coins on the Hold bot's watchlist that are climbing
  3. your own ideas, sent with /pick

Guard rails the AI cannot override: paper only, a hard stop loss on every trade, a size limit, and a daily
spending cap on the AI calls themselves. Needs ANTHROPIC_API_KEY in the config file; without it the bot is idle.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime

import aiohttp

from . import db, fomo, notify, risk, winmodel
from .sources import dexscreener_batch, pair_to_snapshot, safety_check

log = logging.getLogger("gatekeeper.analyst")
API = "https://api.anthropic.com/v1/messages"
GECKO = "https://api.geckoterminal.com/api/v2/networks/%s"
MIN, HOUR, DAY = 60000, 3600000, 86400000


def _env(name, default):
    return os.environ.get("GK_AI_" + name, default)


MODEL = _env("MODEL", "claude-sonnet-5-5")
PRICE_IN, PRICE_OUT = float(_env("IN_PER_M", 2)), float(_env("OUT_PER_M", 10))     # dollars per million tokens
DAILY_CAP = float(_env("DAILY_USD", 200))
MAX_PER_HOUR = int(_env("MAX_PER_HOUR", 40))
SIZES = {1: 100, 2: 200, 3: 300}
MAX_OPEN = int(_env("MAX_OPEN", 6))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, mint TEXT, symbol TEXT, source TEXT, decision TEXT,
  conviction INTEGER, thesis TEXT, flags TEXT, price REAL, cost REAL
);
CREATE INDEX IF NOT EXISTS ai_reviews_mint ON ai_reviews(mint, ts);
CREATE TABLE IF NOT EXISTS ai_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, chain TEXT, symbol TEXT, source TEXT, opened_at INTEGER,
  entry_price REAL, entry_liq REAL, size_usd REAL, peak REAL, stop_price REAL, horizon_h REAL, thesis TEXT,
  cost_pct REAL, last_review INTEGER, last_review_price REAL, closed_at INTEGER, exit_price REAL, pnl_usd REAL,
  pnl_pct REAL, exit_reason TEXT
);
"""

SYSTEM = """You are a careful, experienced meme coin trader making paper trades. You are shown one coin at a time.
Judge it the way a sharp discretionary trader would: read the candle chart first, then the pool, the holders,
the dev's history and the order flow. Most of these coins fall, so passing is the normal answer; buy only when the
chart and the research both look good.

Things that should make you pass: the whole run came from one or two candles; lower highs after a spike; heavy
selling into every bounce; the pool shrinking while the price holds; a few wallets owning too much; a dev with many
dead coins; a coin already far up in the last hour; thin trading. Things that help: a steady climb on rising volume,
pullbacks that are bought quickly, a deep pool that is holding, wide ownership, buyers ahead of sellers.

Answer with ONE JSON object and nothing else."""

BUY_FORMAT = """Decide: buy or pass.
{"decision": "buy" or "pass",
 "conviction": 1, 2 or 3 (1 = small bet, 3 = strongest; only used for a buy),
 "stop_pct": how far under the entry your stop sits, 10 to 35,
 "horizon_hours": how long you would hold at most, 1 to 168,
 "thesis": "two or three plain sentences: what you see on the chart and why you buy or pass",
 "red_flags": ["short phrases", "..."]}"""

HOLD_FORMAT = """You hold this coin on paper. Decide: hold or sell. You may also raise your stop to lock in profit.
{"decision": "hold" or "sell",
 "stop_vs_entry_pct": where your stop should sit relative to your ENTRY price, e.g. -20 or +10 (it can only move up),
 "reason": "one or two plain sentences"}"""


def km(x):
    x = float(x or 0)
    return "$%.1fM" % (x / 1e6) if x >= 1e6 else "$%.0fK" % (x / 1e3) if x >= 1e3 else "$%.0f" % x


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def candle_lines(rows, unit):
    """Candles (newest first from the API) -> compact oldest-first lines, prices as % of the latest close."""
    rows = sorted(rows or [], key=lambda r: r[0])
    if not rows or not rows[-1][4]:
        return ""
    last = rows[-1][4]
    out = []
    for ts, o, h, l, c, v in rows:
        ago = (rows[-1][0] - ts) / (60 if unit == "min" else 3600)
        out.append("-%d%s o%+.0f h%+.0f l%+.0f c%+.0f vol$%s" % (
            ago, "m" if unit == "min" else "h", (o / last - 1) * 100, (h / last - 1) * 100, (l / last - 1) * 100,
            (c / last - 1) * 100, km(v)[1:]))
    return "\n".join(out)


class Analyst:
    def __init__(self, runner):
        self.r = runner
        self.con = runner.con
        self.con.executescript(SCHEMA)
        self.key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.calls = []                    # timestamps of recent AI calls (rate limit)
        self.busy = set()                  # mints being reviewed right now
        self.last_error = ""

    # ---- spending guard
    def _day(self):
        return "ai_spend_" + datetime.now().strftime("%Y-%m-%d")

    def spent_today(self, con=None):
        return float(db.kv_get(con or self.con, self._day()) or 0)

    def can_call(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 3600]
        if not self.key:
            return False, "no API key set"
        if self.spent_today() >= DAILY_CAP:
            return False, "daily AI budget of $%g reached" % DAILY_CAP
        if len(self.calls) >= MAX_PER_HOUR:
            return False, "hourly review limit reached"
        return True, ""

    async def ask(self, prompt, max_tokens=500):
        """One AI call. Returns (parsed JSON or None, cost in dollars)."""
        ok, why = self.can_call()
        if not ok:
            self.last_error = why
            return None, 0.0
        self.calls.append(time.time())
        body = {"model": MODEL, "max_tokens": max_tokens, "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]}
        try:
            async with self.r.session.post(API, json=body, timeout=aiohttp.ClientTimeout(total=60), headers={
                    "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    self.last_error = "AI call failed (%s): %s" % (resp.status, str((data or {}).get("error", {}).get("message", ""))[:120])
                    log.warning(self.last_error)
                    return None, 0.0
        except Exception as e:  # noqa: BLE001
            self.last_error = "AI call failed: %s" % str(e)[:120]
            log.warning(self.last_error)
            return None, 0.0
        u = data.get("usage") or {}
        cost = (u.get("input_tokens", 0) * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT) / 1e6
        db.kv_set(self.con, self._day(), "%.5f" % (self.spent_today() + cost))
        text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        out = parse_json(text)
        if out is None:
            self.last_error = "AI answer was not readable"
        return out, cost

    # ---- what the AI gets to look at
    async def candles(self, chain, pool, established):
        if not pool:
            return ""
        net = "solana" if chain == "solana" else chain
        frames = (("hour", 4, 42, "hour"), ("hour", 1, 24, "hour")) if established else (("minute", 5, 48, "min"), ("minute", 1, 30, "min"))
        parts = []
        for tf, agg, limit, unit in frames:
            try:
                async with self.r.session.get(GECKO % net + "/pools/%s/ohlcv/%s?aggregate=%d&limit=%d" % (pool, tf, agg, limit),
                                              headers={"accept": "application/json"}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        rows = (((await resp.json(content_type=None)).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                        lines = candle_lines(rows, unit)
                        if lines:
                            parts.append("%d-%s candles (time ago, open/high/low/close as %% from the latest price, volume):\n%s" % (
                                agg, "minute" if unit == "min" else "hour", lines))
            except Exception as e:  # noqa: BLE001
                log.info("candles failed: %s", e)
            await asyncio.sleep(1.5)
        return "\n\n".join(parts)

    async def brief(self, mint, chain, pair, extra=""):
        """Everything a trader would look at, as text. Returns (text, snap)."""
        now = int(time.time() * 1000)
        snap = pair_to_snapshot(mint, pair, now)
        created = pair.get("pairCreatedAt") or now
        row = self.con.execute("SELECT graduated_at FROM coins WHERE mint=?", (mint,)).fetchone()
        born = (row["graduated_at"] if row and row["graduated_at"] else created)
        age_min = max(0, (now - born) / MIN)
        liq = snap["liq"] or 0
        established = age_min > 3 * 1440
        saf = self.r.safety.get(mint)
        if not saf:
            try:
                saf = await safety_check(self.r.session, chain, mint)
            except Exception:  # noqa: BLE001
                saf = None
        tx, pc, vol = pair.get("txns") or {}, pair.get("priceChange") or {}, pair.get("volume") or {}
        L = ["COIN: $%s (%s) on %s" % (snap.get("_symbol") or "?", (snap.get("_name") or "")[:40], chain),
             "Age: %s · market cap %s · pool %s" % ("%.1f hours" % (age_min / 60) if age_min < 2880 else "%.0f days" % (age_min / 1440), km(snap.get("fdv")), km(liq)),
             "Price change: 5m %s%% · 1h %s%% · 6h %s%% · 24h %s%%" % (pc.get("m5", "?"), pc.get("h1", "?"), pc.get("h6", "?"), pc.get("h24", "?")),
             "Buys/sells: 5m %s/%s · 1h %s/%s · 24h %s/%s" % tuple(
                 (tx.get(k) or {}).get(s, "?") for k in ("m5", "h1", "h24") for s in ("buys", "sells")),
             "Volume: 1h %s · 24h %s" % (km(vol.get("h1")), km(vol.get("h24")))]
        if saf:
            L.append("Holders: top 10 own %s%% · %s insider-linked wallets · liquidity %s · mint authority %s · freeze authority %s%s" % (
                "%.0f" % saf["top10"] if saf.get("top10") is not None else "?", saf.get("insiders") if saf.get("insiders") is not None else "?",
                "n/a" if saf.get("lp_na") else "%.0f%% locked" % (saf.get("lp_locked") or 0),
                "off" if saf.get("mint_revoked") else "STILL ON", "off" if saf.get("freeze_revoked") else "STILL ON",
                " · DANGER FLAG: %s" % str(saf["danger"])[:80] if saf.get("danger") else ""))
            if saf.get("creator_prev") is not None:
                L.append("Dev: launched %s earlier coins, %s of them dead" % (saf.get("creator_prev"), saf.get("creator_dead") or 0))
        else:
            L.append("Holders: safety data unavailable right now")
        try:
            rrow = risk.live_row(snap, saf, snap.get("_socials"), age_min)
            rug, _ = risk.score(self.r.rug_model, rrow)
            win = winmodel.score(self.r.win_model, rrow) if (self.r.win_model or {}).get("counts") else None
            L.append("Bot's own scores (learned from young coins; less meaningful for old ones): rug risk %s%%%s" % (
                rug, " · win score %s of 100" % win if win is not None else ""))
        except Exception:  # noqa: BLE001
            pass
        ins = self.r.trench.insiders_in(mint) if getattr(self.r, "trench", None) else 0
        if ins:
            L.append("Known insider wallets that bought it: %d" % ins)
        try:
            fomo.ensure_schema(self.con)
            b = self.con.execute("SELECT COUNT(DISTINCT trader) FROM fomo_events WHERE ts>? AND side='buy' AND token_address=?", (now - DAY, mint)).fetchone()[0]
            s = self.con.execute("SELECT COUNT(DISTINCT trader) FROM fomo_events WHERE ts>? AND side='sell' AND token_address=?", (now - DAY, mint)).fetchone()[0]
            if b or s:
                L.append("Fomo app traders, last 24h: %d bought, %d sold" % (b, s))
        except Exception:  # noqa: BLE001
            pass
        words = ((getattr(self.r, "trends", None) and self.r.trends.state) or {}).get("words") or []
        if words:
            L.append("Words hot in the news right now: %s" % ", ".join(words[:12]))
        if extra:
            L.append(extra)
        chart = await self.candles(chain, pair.get("pairAddress"), established)
        L.append("\nCHART\n" + (chart or "candle data unavailable; judge from the price changes above"))
        return "\n".join(L), snap

    # ---- deciding
    def open_trades(self, con=None):
        return [dict(x) for x in (con or self.con).execute("SELECT * FROM ai_trades WHERE closed_at IS NULL ORDER BY opened_at")]

    def _log(self, mint, sym, source, d, price, cost):
        self.con.execute("INSERT INTO ai_reviews(ts,mint,symbol,source,decision,conviction,thesis,flags,price,cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (int(time.time() * 1000), mint, sym, source, str(d.get("decision", "?"))[:10], int(d.get("conviction") or 0),
                          str(d.get("thesis") or d.get("reason") or "")[:600], ", ".join(str(x) for x in (d.get("red_flags") or []))[:300], price, cost))

    async def consider(self, mint, chain, source, extra="", force=False):
        """Review one coin and maybe buy it. Returns a short text describing what happened."""
        if mint in self.busy:
            return "Already looking at that one."
        if not self.key:
            return "The AI trader has no API key yet."
        now = int(time.time() * 1000)
        opens = self.open_trades()
        if any(t["mint"] == mint for t in opens):
            return "The AI trader already holds that coin."
        if not force:
            last = self.con.execute("SELECT MAX(ts) FROM ai_reviews WHERE mint=? AND source!='manage'", (mint,)).fetchone()[0]
            if last and now - last < 6 * HOUR:
                return "Reviewed recently."
            if len(opens) >= MAX_OPEN:
                return "The AI trader is full (%d open trades)." % MAX_OPEN
        self.busy.add(mint)
        try:
            pair = (await dexscreener_batch(self.r.session, [mint], chain)).get(mint)
            if not pair:
                return "Couldn't get a live price for that coin."
            text, snap = await self.brief(mint, chain, pair, extra)
            d, cost = await self.ask(text + "\n\n" + BUY_FORMAT)
            sym = snap.get("_symbol") or "?"
            if not d:
                return "The AI trader couldn't review $%s: %s" % (sym, self.last_error or "no answer")
            self._log(mint, sym, source, d, snap["price"], cost)
            thesis = str(d.get("thesis") or "")[:500]
            if str(d.get("decision")).lower() != "buy":
                flags = ", ".join(str(x) for x in (d.get("red_flags") or [])[:4])
                return "🧠 AI trader passed on $%s.\n%s%s" % (html.escape(sym), html.escape(thesis), "\nRed flags: " + html.escape(flags) if flags else "")
            if len(self.open_trades()) >= MAX_OPEN:
                return "🧠 AI trader liked $%s but is full (%d open trades).\n%s" % (html.escape(sym), MAX_OPEN, html.escape(thesis))
            conv = min(3, max(1, int(d.get("conviction") or 1)))
            stop = min(35, max(10, float(d.get("stop_pct") or 25)))
            horizon = min(168, max(1, float(d.get("horizon_hours") or 24)))
            price, liq = snap["price"], snap["liq"] or 0
            cost_pct = 1.5 if liq >= 300000 else 3.0          # deep pools fill better than thin ones
            self.con.execute(
                "INSERT INTO ai_trades(mint,chain,symbol,source,opened_at,entry_price,entry_liq,size_usd,peak,stop_price,horizon_h,thesis,cost_pct,last_review,last_review_price) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mint, chain, sym, source, now, price, liq, SIZES[conv], price, price * (1 - stop / 100), horizon, thesis, cost_pct, now, price))
            msg = "🟢 <b>PAPER BUY</b> [AI trader] $%s · $%d (conviction %d of 3)\n%s\nPlan: stop at -%.0f%%, hold up to %s · cap %s · pool %s\n%s" % (
                html.escape(sym), SIZES[conv], conv, html.escape(thesis), stop,
                "%.0f hours" % horizon if horizon < 48 else "%.0f days" % (horizon / 24), km(snap.get("fdv")), km(liq), notify.dex_link(mint, chain))
            await notify.send(self.r.session, msg)
            return msg
        finally:
            self.busy.discard(mint)

    async def close(self, t, price, reason, now):
        c = t["cost_pct"] / 100
        net = (price / t["entry_price"]) * (1 - c) / (1 + c) - 1 if t["entry_price"] else 0
        pnl = t["size_usd"] * net
        self.con.execute("UPDATE ai_trades SET closed_at=?, exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=? WHERE id=?",
                         (now, price, pnl, net * 100, reason[:300], t["id"]))
        held = (now - t["opened_at"]) / HOUR
        await notify.send(self.r.session, "%s <b>PAPER SELL</b> [AI trader] $%s: %+.0f%% ($%+.2f) after %s\n%s\n%s" % (
            "✅" if pnl > 0 else "🔴", html.escape(t["symbol"]), net * 100, pnl,
            "%.1f hours" % held if held < 48 else "%.1f days" % (held / 24), html.escape(reason), notify.dex_link(t["mint"], t["chain"])))

    async def manage(self, now):
        opens = self.open_trades()
        for chain in {t["chain"] for t in opens}:
            mine = [t for t in opens if t["chain"] == chain]
            pairs = await dexscreener_batch(self.r.session, [t["mint"] for t in mine], chain)
            for t in mine:
                pair = pairs.get(t["mint"]) or {}
                try:
                    price = float(pair.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    price = 0
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if not price:
                    continue
                peak = max(t["peak"] or 0, price)
                if peak != t["peak"]:
                    self.con.execute("UPDATE ai_trades SET peak=? WHERE id=?", (peak, t["id"]))
                # --- the guard rails: these fire without asking the AI
                if price <= t["stop_price"]:
                    await self.close(t, price, "Stop hit (set at %+.0f%% from entry)" % ((t["stop_price"] / t["entry_price"] - 1) * 100), now)
                    continue
                if liq and t["entry_liq"] and liq < t["entry_liq"] * 0.5:
                    await self.close(t, price, "Pool collapsed to %s from %s" % (km(liq), km(t["entry_liq"])), now)
                    continue
                if now - t["opened_at"] >= t["horizon_h"] * HOUR:
                    await self.close(t, price, "Reached its planned holding time", now)
                    continue
                # --- otherwise let the AI re-read the chart now and then
                every = 3 * HOUR if (t["entry_liq"] or 0) >= 300000 else 20 * MIN
                moved = abs(price / (t["last_review_price"] or price) - 1) >= 0.15
                since = now - (t["last_review"] or t["opened_at"])
                if since < every and not (moved and since >= 5 * MIN):
                    continue
                gain = (price / t["entry_price"] - 1) * 100
                extra = ("YOUR POSITION: bought %.1f hours ago, now %+.0f%% from your entry, best so far %+.0f%%, your stop sits at %+.0f%% from entry. "
                         "Your reason for buying: %s" % ((now - t["opened_at"]) / HOUR, gain, (peak / t["entry_price"] - 1) * 100,
                                                        (t["stop_price"] / t["entry_price"] - 1) * 100, t["thesis"] or ""))
                text, _ = await self.brief(t["mint"], t["chain"], pair, extra)
                d, cost = await self.ask(text + "\n\n" + HOLD_FORMAT, max_tokens=250)
                self.con.execute("UPDATE ai_trades SET last_review=?, last_review_price=? WHERE id=?", (now, price, t["id"]))
                if not d:
                    continue
                self._log(t["mint"], t["symbol"], "manage", d, price, cost)
                if str(d.get("decision")).lower() == "sell":
                    await self.close(t, price, "AI sold: %s" % str(d.get("reason") or "")[:240], now)
                    continue
                try:
                    new_stop = t["entry_price"] * (1 + float(d.get("stop_vs_entry_pct")) / 100)
                except (TypeError, ValueError):
                    new_stop = 0
                if t["stop_price"] < new_stop < price * 0.97:          # a stop only ever moves up, and stays under the price
                    self.con.execute("UPDATE ai_trades SET stop_price=? WHERE id=?", (new_stop, t["id"]))

    async def on_main_buy(self, pos):
        """Main just bought this coin: get the AI's second opinion (and its own trade if it agrees)."""
        try:
            chain = "robinhood" if str(pos.mint).startswith("0x") else "solana"
            await self.consider(pos.mint, chain, "main", extra="Context: the rule-based bot just bought this coin. Its reasons: %s" % (pos.why or "")[:400])
        except Exception:  # noqa: BLE001
            log.exception("AI review of a Main buy failed")

    async def pick(self, arg):
        """/pick: the owner's own idea. The AI still decides, and explains."""
        from . import check
        if not self.key:
            return self.setup_text()
        addr, chain, pair, note = await check.resolve(self.r.session, arg)
        if not addr:
            return "🧠 " + note
        out = await self.consider(addr, chain, "pick", extra="Context: the owner of this bot suggested this coin and is bullish on it. Judge it on its merits.", force=True)
        return out + ("\n" + html.escape(note) if note else "")

    async def loop(self):
        await asyncio.sleep(200)
        n = 0
        while True:
            now = int(time.time() * 1000)
            try:
                if self.key:
                    await self.manage(now)
                    if n % 15 == 0:            # every 15 min: look over the established coins that are climbing
                        from .hold import trend_fails
                        watch = getattr(getattr(self.r, "hold", None), "watch", {}) or {}
                        looked = 0
                        for c in sorted(watch.values(), key=lambda c: -c["pc24h"]):
                            if looked >= 2:
                                break
                            if trend_fails(c):
                                continue
                            res = await self.consider(c["mint"], "solana", "hold")
                            looked += 0 if res in ("Reviewed recently.", "The AI trader already holds that coin.") else 1
            except Exception:  # noqa: BLE001
                log.exception("AI trader update failed")
            n += 1
            await asyncio.sleep(60)

    # ---- report
    def setup_text(self):
        return ("🧠 <b>AI trader</b>: built, but it needs an Anthropic API key before it can think.\n"
                "1. Create a key at console.anthropic.com (API keys). Never paste it into a chat.\n"
                "2. On the server, run the hidden-input command you were given to save it as ANTHROPIC_API_KEY.\n"
                "3. Restart the bot. Daily AI budget: $%g." % DAILY_CAP)

    def text(self, con=None):
        con = con or self.con          # the stats feed calls this from its own thread, with its own connection
        if not self.key:
            return self.setup_text()
        now = int(time.time() * 1000)
        L = ["🧠 <b>AI trader</b> (paper): judges each coin like a person, with a written reason",
             "AI cost today: $%.2f of a $%g daily budget · model %s" % (self.spent_today(con), DAILY_CAP, MODEL)]
        if self.last_error:
            L.append("Last problem: %s" % html.escape(self.last_error))
        closed = [dict(x) for x in con.execute("SELECT * FROM ai_trades WHERE closed_at IS NOT NULL ORDER BY closed_at DESC")]
        if closed:
            wins = sum(1 for t in closed if t["pnl_usd"] > 0)
            L.append("\nClosed: %d trades · %d%% win · $%+.2f" % (len(closed), wins * 100 // len(closed), sum(t["pnl_usd"] for t in closed)))
            for src, name in (("main", "coins Main also bought"), ("hold", "established coins"), ("pick", "your picks")):
                ts = [t for t in closed if t["source"] == src]
                if ts:
                    L.append("  %s: %d trades · $%+.2f" % (name, len(ts), sum(t["pnl_usd"] for t in ts)))
            for t in closed[:6]:
                L.append("$%s: %+.0f%% ($%+.2f) · %s" % (html.escape(t["symbol"]), t["pnl_pct"], t["pnl_usd"], html.escape((t["exit_reason"] or "")[:90])))
        else:
            L.append("\nNo closed trades yet.")
        # the second-opinion scorecard: what happened to Main's buys the AI agreed with vs passed on
        agree, passed = [], []
        for rv in con.execute("SELECT mint, decision, ts FROM ai_reviews WHERE source='main'"):
            m = con.execute("SELECT pnl_usd FROM trades WHERE mode='live' AND COALESCE(run_id,'main')='main' AND mint=? AND closed_at IS NOT NULL "
                                 "AND opened_at BETWEEN ? AND ?", (rv["mint"], rv["ts"] - 10 * MIN, rv["ts"] + 10 * MIN)).fetchone()
            if m and m["pnl_usd"] is not None:
                (agree if rv["decision"] == "buy" else passed).append(m["pnl_usd"])
        if agree or passed:
            L.append("\n<b>Second opinion on Main's buys</b> (Main's own result on each)")
            L.append("AI said buy: %d trades · Main made $%+.2f on them" % (len(agree), sum(agree)))
            L.append("AI said pass: %d trades · Main made $%+.2f on them" % (len(passed), sum(passed)))
        opens = self.open_trades(con)
        if opens:
            L.append("\n<b>Holding now</b>")
            for t in opens:
                L.append("$%s · $%d · %.1f hours in · stop at %+.0f%% · %s" % (
                    html.escape(t["symbol"]), t["size_usd"], (now - t["opened_at"]) / HOUR, (t["stop_price"] / t["entry_price"] - 1) * 100,
                    html.escape((t["thesis"] or "")[:140])))
        last = [dict(x) for x in con.execute("SELECT * FROM ai_reviews WHERE source!='manage' ORDER BY ts DESC LIMIT 5")]
        if last:
            L.append("\n<b>Latest calls</b>")
            for rv in last:
                L.append("%s $%s: %s" % ("🟢" if rv["decision"] == "buy" else "⚪", html.escape(rv["symbol"] or "?"), html.escape((rv["thesis"] or "")[:160])))
        L.append("\nSend /pick COIN to have it judge one of your ideas. Paper only.")
        return "\n".join(L)
