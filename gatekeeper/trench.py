"""Trench wallets: on-chain traders who keep getting into moonshots early.

1. Discover: for coins that went 10x+ within 12h (from the rug-risk history), read the wallets that bought
   between 15 min before and 30 min after graduation. Do the same for a sample of coins that did NOT run,
   so wallets that simply buy everything (bots, spray buyers) can be filtered out.
2. Rank: wallets early in 2+ different moonshots, by hit rate (moonshots / all early buys we examined).
3. Watch: Helius pushes the top wallets' trades to the bot (webhook), with slow polling as a fallback.
   Every buy is recorded, graded 1h/6h/24h later in the scorecard, and 2+ trench wallets buying the same
   coin within 30 minutes sends an alert.
"""
import asyncio
import html
import logging
import random
import secrets
import time

from . import config, db, fomo, notify
from .helius import Helius, key, swaps_for

log = logging.getLogger("gatekeeper.trench")
MIN = 60000

SCHEMA = """
CREATE TABLE IF NOT EXISTS trench_coins (mint TEXT PRIMARY KEY, moonshot INTEGER, peak_x REAL, symbol TEXT, done_at INTEGER, buyers INTEGER);
CREATE TABLE IF NOT EXISTS early_buys (mint TEXT, wallet TEXT, ts INTEGER, moonshot INTEGER, peak_x REAL, PRIMARY KEY (mint, wallet));
CREATE INDEX IF NOT EXISTS early_wallet ON early_buys(wallet);
CREATE TABLE IF NOT EXISTS trench_wallets (wallet TEXT PRIMARY KEY, hits INTEGER, seen INTEGER, hit_rate REAL, avg_x REAL, best_x REAL,
  examples TEXT, rank INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS trench_events (sig TEXT, wallet TEXT, side TEXT, mint TEXT, sol REAL, ts INTEGER, PRIMARY KEY (sig, mint, wallet));
CREATE INDEX IF NOT EXISTS trench_ev_ts ON trench_events(ts);
"""


def ensure(con):
    con.executescript(SCHEMA)


def short(w):
    return w[:4] + "…" + w[-4:]


class Trench:
    def __init__(self, runner):
        self.r = runner
        ensure(runner.con)
        self.wallets = {}          # wallet -> row dict (tracked)
        self.load()
        self.alerted = {}

    def load(self):
        self.wallets = {r["wallet"]: dict(r) for r in self.r.con.execute(
            "SELECT * FROM trench_wallets WHERE rank IS NOT NULL ORDER BY rank LIMIT ?",
            (int(float(config.os.environ.get("GK_TRENCH_TRACK", "40"))),))}

    # ------------------------------------------------------------------ discovery
    async def early_buyers(self, h, mint, grad_ms):
        """Wallets that bought `mint` from 15 min before to 30 min after graduation."""
        t0, t1 = (grad_ms - 15 * MIN) / 1000, (grad_ms + 30 * MIN) / 1000
        before, window = None, []
        for _ in range(60):                               # walk back through history (1 credit per 1,000 txs)
            page = await h.signatures(mint, before)
            if not page:
                break
            for s in page:
                bt = s.get("blockTime") or 0
                if t0 <= bt <= t1 and not s.get("err"):
                    window.append(s["signature"])
            before = page[-1]["signature"]
            if (page[-1].get("blockTime") or 0) < t0 or len(page) < 1000:
                break
        else:
            return None                                   # too much history to reach the start; skip
        if not window:
            return []
        window = window[-200:]                            # oldest 200 in the window (pages come newest first)
        buyers = {}
        for tx in await h.parse(window):
            for w, side, m, sol in swaps_for(tx, None):
                if side == "buy" and m == mint and w not in buyers:
                    buyers[w] = (tx.get("timestamp") or 0) * 1000
        return buyers

    async def discover(self, max_coins=30):
        con, h = self.r.con, Helius(self.r.session, self.r.con)
        now = int(time.time() * 1000)
        cand = [dict(r) for r in con.execute(
            "SELECT f.mint, MAX(f.peak_x) px, c.graduated_at, c.symbol FROM coin_features f JOIN coins c ON c.mint=f.mint "
            "WHERE c.source IS NULL AND f.ts<? AND c.mint NOT IN (SELECT mint FROM trench_coins) GROUP BY f.mint",
            (now - 12 * 3600 * 1000,))]
        wins = [c for c in cand if (c["px"] or 1) >= 10 and (c["px"] or 1) < 1000]      # >1000x is almost always a data glitch
        rest = [c for c in cand if (c["px"] or 1) < 3]
        random.shuffle(rest)
        todo = wins[:max_coins // 2] + rest[:max(max_coins // 2, len(wins[:max_coins // 2]) * 2)]
        done = 0
        for c in todo:
            if not h.can_spend(400):
                break
            try:
                buyers = await self.early_buyers(h, c["mint"], c["graduated_at"])
            except Exception as e:  # noqa: BLE001
                log.info("early buyers %s: %s", c["mint"], e)
                if "budget" in str(e):
                    break
                continue
            moon = 1 if (c["px"] or 1) >= 10 else 0
            con.execute("INSERT OR REPLACE INTO trench_coins VALUES(?,?,?,?,?,?)",
                        (c["mint"], moon, c["px"], c["symbol"], now, len(buyers) if buyers is not None else -1))
            for w, ts in (buyers or {}).items():
                con.execute("INSERT OR IGNORE INTO early_buys VALUES(?,?,?,?,?)", (c["mint"], w, ts, moon, c["px"]))
            done += 1
            await asyncio.sleep(0.5)
        if done:
            self.rank()
        return done

    def rank(self):
        con = self.r.con
        n_coins = con.execute("SELECT COUNT(*) FROM trench_coins WHERE buyers>0").fetchone()[0] or 1
        rows = con.execute("SELECT wallet, COUNT(*) seen, SUM(moonshot) hits, AVG(CASE WHEN moonshot=1 THEN peak_x END) avg_x, "
                           "MAX(peak_x) best_x, GROUP_CONCAT(CASE WHEN moonshot=1 THEN mint END) ex FROM early_buys GROUP BY wallet").fetchall()
        ranked = []
        for r in rows:
            if (r["hits"] or 0) < 2 or r["seen"] > max(8, n_coins * 0.3):       # need 2+ moonshots; drop buy-everything bots
                continue
            rate = (r["hits"] + 1) / (r["seen"] + 3)                            # shrink small samples toward the base rate
            ranked.append((rate * (r["hits"] ** 0.5), r, (r["hits"]) / r["seen"]))
        ranked.sort(key=lambda x: -x[0])
        con.execute("UPDATE trench_wallets SET rank=NULL")
        now = int(time.time() * 1000)
        for i, (score, r, rate) in enumerate(ranked[:100]):
            syms = []
            for m in (r["ex"] or "").split(",")[:5]:
                s = con.execute("SELECT symbol FROM coins WHERE mint=?", (m,)).fetchone()
                syms.append(s["symbol"] if s and s["symbol"] else m[:6])
            con.execute("INSERT OR REPLACE INTO trench_wallets VALUES(?,?,?,?,?,?,?,?,?)",
                        (r["wallet"], r["hits"], r["seen"], round(rate * 100, 1), round(r["avg_x"] or 0, 1), round(r["best_x"] or 0, 1),
                         ", ".join(syms), i + 1, now))
        self.load()

    # ------------------------------------------------------------------ live
    async def on_txs(self, txs):
        """Parsed transactions (from the webhook or polling)."""
        now = int(time.time() * 1000)
        for tx in txs or []:
            for w, side, mint, sol in swaps_for(tx, self.wallets):
                ts = int((tx.get("timestamp") or now / 1000) * 1000)
                cur = self.r.con.execute("INSERT OR IGNORE INTO trench_events VALUES(?,?,?,?,?,?)",
                                         (tx.get("signature"), w, side, mint, round(sol, 4), ts))
                if not cur.rowcount or side != "buy":
                    continue
                self.r.track_coin(mint, "solana")
                fomo.ensure_schema(self.r.con)
                self.r.con.execute("INSERT INTO trader_calls(trader, token_address, symbol, chain, ts, usd) VALUES(?,?,?,?,?,?)",
                                   ("trench:" + short(w), mint, "", "solana", ts, 0))
                await self._cluster(mint, now)

    async def _cluster(self, mint, now):
        rows = self.r.con.execute("SELECT DISTINCT wallet FROM trench_events WHERE mint=? AND side='buy' AND ts>?",
                                  (mint, now - 30 * MIN)).fetchall()
        n = int(float(config.os.environ.get("GK_TRENCH_CLUSTER", "2")))
        if len(rows) < n or now - self.alerted.get(mint, 0) < 6 * 3600 * 1000:
            return
        self.alerted[mint] = now
        who = []
        for r in rows[:5]:
            w = self.wallets.get(r["wallet"], {})
            who.append("%s (%s moonshots, e.g. %s)" % (short(r["wallet"]), w.get("hits", "?"), (w.get("examples") or "")[:40]))
        rk = self.r.rug_risk(mint)[0]
        await notify.send(self.r.session, "⛏️ <b>Trench cluster: %d wallets that caught earlier moonshots just bought</b>\n%s%s\n%s\nWorth a look, not a buy signal." % (
            len(rows), html.escape("\n".join(who)), ("\nRug risk: %d%%" % rk) if rk is not None else "", notify.dex_link(mint, "solana")))

    async def poll(self, h):
        """Fallback when the webhook isn't available: check the top wallets for new trades."""
        new = []
        for w in list(self.wallets)[:15]:
            if not h.can_spend(50):
                break
            last = db.kv_get(self.r.con, "trench_last_" + w)
            sigs = await h.signatures(w, None, 10)
            fresh = []
            for s in sigs:
                if s["signature"] == last:
                    break
                if not s.get("err"):
                    fresh.append(s["signature"])
            if sigs:
                db.kv_set(self.r.con, "trench_last_" + w, sigs[0]["signature"])
            if last:
                new += fresh
            await asyncio.sleep(0.3)
        if new:
            await self.on_txs(await h.parse(new[:100]))

    def hook_secret(self):
        s = db.kv_get(self.r.con, "hook_secret")
        if not s:
            s = secrets.token_urlsafe(16)
            db.kv_set(self.r.con, "hook_secret", s)
        return s

    async def loop(self):
        if not key():
            log.info("No Helius key; trench wallets off")
            return
        await asyncio.sleep(120)
        from . import web
        h = Helius(self.r.session, self.r.con)
        last_disc, hooked, announced = 0, False, bool(self.wallets)
        while True:
            try:
                if time.time() - last_disc > 6 * 3600:
                    n = await self.discover()
                    last_disc = time.time()
                    if self.wallets:
                        url = (await web.public_url(self.r.session, self.r.con)).split("/?")[0] + "/hook/" + self.hook_secret()
                        hooked = bool(await h.set_webhook(url, list(self.wallets)))
                    if self.wallets and not announced:
                        announced = True
                        await notify.send(self.r.session, "⛏️ Found %d trench wallets that got into 2+ moonshots early. Watching them %s. Send /trench or open the Trench tab." % (
                            len(self.wallets), "live" if hooked else "every few minutes"))
                    elif n and not self.wallets:
                        log.info("Trench discovery: %d coins examined, no wallets with 2+ moonshots yet", n)
                if not hooked and self.wallets:
                    await self.poll(h)
            except Exception:  # noqa: BLE001
                log.exception("Trench loop")
            await asyncio.sleep(300)

    # ------------------------------------------------------------------ views
    def state(self):
        con = self.r.con
        fomo.ensure_schema(con)
        now = int(time.time() * 1000)
        wallets = [dict(r) for r in con.execute("SELECT * FROM trench_wallets WHERE rank IS NOT NULL ORDER BY rank LIMIT 40")]
        grades = {}
        for r in con.execute("SELECT trader, p0, p6h FROM trader_calls WHERE trader LIKE 'trench:%' AND p0>0 AND p6h>=0"):
            grades.setdefault(r["trader"][7:], []).append((r["p6h"] / r["p0"] - 1) * 100)
        for w in wallets:
            g = sorted(grades.get(short(w["wallet"]), []))
            w["live_buys"] = len(g)
            w["live_med6h"] = round(g[len(g) // 2], 1) if g else None
        feed = [dict(r) for r in con.execute("SELECT e.*, c.symbol FROM trench_events e LEFT JOIN coins c ON c.mint=e.mint ORDER BY e.ts DESC LIMIT 50")]
        hot = [dict(r) for r in con.execute(
            "SELECT e.mint, c.symbol, COUNT(DISTINCT e.wallet) n, SUM(CASE WHEN e.side='buy' THEN e.sol ELSE 0 END) sol_in, MAX(e.ts) last "
            "FROM trench_events e LEFT JOIN coins c ON c.mint=e.mint WHERE e.ts>? AND e.side='buy' GROUP BY e.mint ORDER BY n DESC, last DESC LIMIT 12",
            (now - 24 * 3600 * 1000,))]
        coins = con.execute("SELECT COUNT(*) n, SUM(moonshot) m FROM trench_coins WHERE buyers>=0").fetchone()
        moons = [dict(r) for r in con.execute(
            "SELECT t.mint, t.symbol, t.peak_x, t.buyers FROM trench_coins t WHERE t.moonshot=1 ORDER BY t.peak_x DESC LIMIT 12")]
        h = Helius(self.r.session, con)
        return {"has_key": bool(key()), "wallets": wallets, "feed": feed, "hot": hot, "moons": moons,
                "examined": coins["n"] or 0, "moonshots": coins["m"] or 0, "credits_today": h.used_today(),
                "credit_cap": int(float(config.os.environ.get("GK_HELIUS_DAILY_CREDITS", "25000")))}

    def text(self):
        s = self.state()
        if not s["has_key"]:
            return "Trench wallets need a Helius key (HELIUS_API_KEY)."
        L = ["⛏️ <b>Trench wallets</b>", "Coins examined: %d (%d moonshots) · Helius credits today: %s of %s" % (
            s["examined"], s["moonshots"], format(s["credits_today"], ","), format(s["credit_cap"], ","))]
        if not s["wallets"]:
            L.append("No wallets with 2+ early moonshot buys yet. Discovery runs every 6 hours.")
        for w in s["wallets"][:10]:
            L.append("#%d %s: early in %d moonshots of %d coins (%.0f%%) · avg %gx · e.g. %s%s" % (
                w["rank"], short(w["wallet"]), w["hits"], w["seen"], w["hit_rate"], w["avg_x"], html.escape(w["examples"] or ""),
                (" · live buys 6h median %+.0f%%" % w["live_med6h"]) if w["live_med6h"] is not None else ""))
        return "\n".join(L)
