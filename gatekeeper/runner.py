"""The always-on service: recorder + live paper trader + Telegram."""
import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

import html
import re

from . import config, db, fomo, followtest, notify, report, sweep
from .sources import CHAINS, EVM_RE, SOL_RE, dexscreener_batch, pair_to_snapshot, pumpportal_stream, safety_check
from .strategy import MIN, Position, Strategy

log = logging.getLogger("gatekeeper")
TZ = ZoneInfo(config.TIMEZONE)
POLL_SEC = 30


def now_ms():
    return int(time.time() * 1000)


def today_local():
    return datetime.now(TZ).strftime("%Y-%m-%d %H")   # hourly buckets for launch counts


class Runner:
    def __init__(self):
        self.con = db.connect()
        self.safety = {r["mint"]: dict(r) for r in self.con.execute("SELECT * FROM safety")}
        look = lambda m: self.safety.get(m)  # noqa: E731
        self.strats = {name: Strategy(config.strategy_params(preset=name), look) for name in config.ENABLED_PRESETS}
        self.strat = self.strats.get("main") or next(iter(self.strats.values()))
        self.p = self.strat.p
        # safety checks must cover the loosest strategy
        rule_based = [s for s in self.strats.values() if s.p.get("ENTRY_MODE") != "signal"] or list(self.strats.values())
        self.safety_min_age = min(s.p["MIN_AGE_MIN"] for s in rule_based)
        self.safety_min_liq = min(s.p["MIN_LIQ_USD"] for s in self.strats.values())
        self.session = None
        self.tick = 0
        self.last_liq = {}
        self.sweeping = False
        self.tracking = set()
        for name, st in self.strats.items():
            self._restore(name, st)

    def any_open(self, mint):
        return any(mint in st.positions for st in self.strats.values())

    # ------------------------------------------------------------ restore after restart
    def _restore(self, name, strat):
        since = now_ms() - 6 * 3600 * 1000
        coins = {r["mint"]: dict(r) for r in self.con.execute("SELECT * FROM coins WHERE status='watching'")}
        rows = self.con.execute("SELECT * FROM snapshots WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        saved_max = strat.p["MAX_OPEN"]
        strat.p["MAX_OPEN"] = 0                      # rebuild peaks and history without trading
        for r in rows:
            c = coins.get(r["mint"])
            if c:
                strat.on_snapshot(dict(r), c)
        strat.p["MAX_OPEN"] = saved_max
        sel = "mode='live' AND COALESCE(run_id,'main')=?"
        for r in self.con.execute("SELECT mint FROM trades WHERE " + sel, (name,)):
            strat.traded.add(r["mint"])
        for r in self.con.execute("SELECT * FROM trades WHERE " + sel + " AND closed_at IS NULL", (name,)):
            legs = json.loads(r["legs"] or "[]")
            pos = Position(r["mint"], r["symbol"], r["opened_at"], r["entry_price"], legs[0]["spot"] if legs else r["entry_price"],
                           r["size_usd"], r["qty"], r["qty"], why=r["why_entered"] or "", legs=legs, trade_id=r["id"])
            for leg in legs[1:]:
                if leg["side"] == "sell":
                    pos.proceeds += leg["usd"]
                    pos.qty_open = pos.qty_total / 2
                    pos.took_half = True
            cs = strat.coins.get(r["mint"])
            pos.peak_after = cs.peak_price if cs else pos.spot_at_entry
            strat.positions[r["mint"]] = pos
        log.info("[%s] restored %d coins, %d open positions", name, len(strat.coins), len(strat.positions))

    # ------------------------------------------------------------ PumpPortal events
    async def on_event(self, kind, mint, symbol, name, raw):
        day = today_local()
        self.con.execute("INSERT OR IGNORE INTO launches(day) VALUES(?)", (day,))
        if kind == "create":
            self.con.execute("UPDATE launches SET created=created+1 WHERE day=?", (day,))
            return
        exists = self.con.execute("SELECT 1 FROM coins WHERE mint=?", (mint,)).fetchone()
        if exists:
            return
        self.con.execute("UPDATE launches SET graduated=graduated+1 WHERE day=?", (day,))
        self.con.execute("INSERT INTO coins(mint, symbol, name, graduated_at, last_seen, status) VALUES(?,?,?,?,?, 'watching')",
                         (mint, symbol, name, now_ms(), now_ms()))
        log.info("Graduated: %s %s", symbol or "", mint)

    # ------------------------------------------------------------ polling loop
    async def poll_loop(self):
        while True:
            started = time.time()
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Poll cycle failed")
            await asyncio.sleep(max(1, POLL_SEC - (time.time() - started)))

    async def poll_once(self):
        now = now_ms()
        p = self.p
        coins = {r["mint"]: dict(r) for r in self.con.execute("SELECT * FROM coins WHERE status='watching'")}
        self.tick += 1
        slow_liq = float(config.os.environ.get("GK_SLOW_LIQ_USD", "7000"))
        # coins with small pools and no open trade get checked every 5 minutes instead of every 30 seconds
        # (saves disk on the small server; they can't be traded until their pool grows anyway)
        def quiet(m):
            c = coins[m]
            return (not self.any_open(m) and self.last_liq.get(m, 1e12) < slow_liq
                    and (now - (c.get("added_at") or c["graduated_at"])) / MIN > 45)
        mints = [m for m in coins if self.tick % 10 == 0 or not quiet(m)]
        seen = set(m for m in coins if m not in mints)   # skipped this round, not missing
        batches = []
        for chain in CHAINS:
            cm = [m for m in mints if (coins[m].get("chain") or "solana") == chain]
            batches += [(chain, cm[i:i + 30]) for i in range(0, len(cm), 30)]
        for bi, (chain, batch) in enumerate(batches):
            pairs = await dexscreener_batch(self.session, batch, chain)
            for mint, pair in pairs.items():
                if mint not in coins:
                    continue
                s = pair_to_snapshot(mint, pair, now)
                if not s["price"]:
                    continue
                seen.add(mint)
                self.last_liq[mint] = s["liq"]
                c = coins[mint]
                if c.get("grad_price") is None:
                    c["grad_price"] = s["price"]
                self.con.execute("UPDATE coins SET symbol=COALESCE(symbol,?), name=COALESCE(name,?), pair=?, dex=?, "
                                 "grad_price=COALESCE(grad_price,?), last_seen=? WHERE mint=?",
                                 (s["_symbol"], s["_name"], s["_pair"], s["_dex"], s["price"], now, mint))
                if not c.get("symbol"):
                    c["symbol"] = s["_symbol"]
                self.con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (mint, now, s["price"], s["liq"], s["fdv"], s["vol_m5"], s["vol_h1"], s["buys_m5"],
                                  s["sells_m5"], s["buys_h1"], s["sells_h1"], s["pc_m5"], s["pc_h1"]))
                for name, st in self.strats.items():
                    for a in st.on_snapshot(s, c):
                        await self.act(a, name)
                age_min = (now - (c.get("added_at") or c["graduated_at"])) / MIN
                if s["liq"] < p["DEAD_LIQ_USD"] and age_min > 30 and not self.any_open(mint):
                    self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
            if bi + 1 < len(batches):
                await asyncio.sleep(1)
        # coins DexScreener never listed, or that aged out
        for mint, c in coins.items():
            age_min = (now - (c.get("added_at") or c["graduated_at"])) / MIN
            if self.any_open(mint):
                continue
            if age_min > p["WATCH_HOURS"] * 60:
                self.con.execute("UPDATE coins SET status='expired' WHERE mint=?", (mint,))
            elif mint not in seen and age_min > 30 and (now - (c["last_seen"] or 0)) / MIN > 20:
                self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
        for name, st in self.strats.items():
            for a in st.on_tick(now):
                await self.act(a, name)
            if self.tick % 20 == 0:
                st.prune(now, 30 * MIN)
        db.kv_set(self.con, "last_poll", now)
        db.kv_set(self.con, "watching", len(mints))

    # ------------------------------------------------------------ safety checks
    async def safety_loop(self):
        while True:
            try:
                now = now_ms()
                rows = self.con.execute(
                    "SELECT c.mint, c.graduated_at, c.chain, c.source, "
                    "(SELECT liq FROM snapshots s WHERE s.mint=c.mint ORDER BY ts DESC LIMIT 1) AS liq "
                    "FROM coins c WHERE c.status='watching'").fetchall()
                for r in rows:
                    age = (now - r["graduated_at"]) / MIN
                    if (r["source"] != "fomo" and age < self.safety_min_age - 8) or (r["liq"] or 0) < self.safety_min_liq * 0.7:
                        continue
                    have = self.safety.get(r["mint"])
                    if have and now - have["checked_at"] < 45 * MIN:
                        continue
                    res = await safety_check(self.session, r["chain"] or "solana", r["mint"])
                    if res:
                        res.update(mint=r["mint"], checked_at=now_ms())
                        self.safety[r["mint"]] = res
                        db.save_safety(self.con, res)
                    await asyncio.sleep(2)
            except Exception:  # noqa: BLE001
                log.exception("Safety loop error")
            await asyncio.sleep(20)

    # ------------------------------------------------------------ actions -> db + telegram
    async def act(self, a, name="main"):
        pos = a["pos"]
        alert = name in config.ALERT_PRESETS
        tag = "" if name == "main" else "[%s] " % name.upper()
        if a["type"] == "buy":
            cur = self.con.execute(
                "INSERT INTO trades(mode, run_id, mint, symbol, opened_at, entry_price, size_usd, qty, why_entered, legs) "
                "VALUES('live',?,?,?,?,?,?,?,?,?)",
                (name, pos.mint, pos.symbol, pos.opened_at, pos.entry_price, pos.size_usd, pos.qty_total, pos.why, json.dumps(pos.legs)))
            pos.trade_id = cur.lastrowid
            if alert:
                await notify.send(self.session, tag + notify.fmt_buy(pos, a["spot"], a["liq"], self.strats[name].p))
        elif a["type"] == "partial":
            self.con.execute("UPDATE trades SET legs=? WHERE id=?", (json.dumps(pos.legs), pos.trade_id))
            if alert:
                await notify.send(self.session, tag + notify.fmt_partial(pos, a["spot"], a["usd"]))
        elif a["type"] == "close":
            self.con.execute("UPDATE trades SET closed_at=?, proceeds_usd=?, pnl_usd=?, pnl_pct=?, exit_reason=?, legs=? WHERE id=?",
                             (pos.closed_at, pos.proceeds, pos.pnl_usd, pos.pnl_pct, pos.exit_reason, json.dumps(pos.legs), pos.trade_id))
            if alert:
                await notify.send(self.session, tag + notify.fmt_close(pos, a["spot"], a["reason"]))

    # ------------------------------------------------------------ Fomo alerts
    async def on_fomo_alert(self, kind, info):
        addr, chain = info.get("address") or "", (info.get("chain") or "").lower()
        buyers = sorted(info["buyers"].items(), key=lambda kv: -kv[1])
        who = ", ".join("@%s (%s)" % (html.escape(h), notify.money(v)) for h, v in buyers[:6])
        more = " +%d more" % (len(buyers) - 6) if len(buyers) > 6 else ""
        if kind == "cluster":
            head = "🔵 <b>FOMO CLUSTER: %d traders on your list bought $%s</b> in the last 30 min" % (len(buyers), html.escape(str(info["token"])))
        else:
            head = "🔥 <b>TRENDING ON FOMO: %d traders bought $%s</b> in the last 15 min (%s total)" % (
                len(buyers), html.escape(str(info["token"])), notify.money(sum(v for _, v in buyers)))
        lines = [head, "Buyers: " + who + more]
        if chain in ("sol", ""):
            chain = "solana" if re.fullmatch(SOL_RE, addr or "") else chain
        if chain == "robinhood" and re.fullmatch(EVM_RE, addr or ""):
            addr = addr.lower()
        ok_addr = (chain == "solana" and re.fullmatch(SOL_RE, addr or "")) or (chain == "robinhood" and re.fullmatch(EVM_RE, addr or ""))
        if chain in CHAINS and ok_addr:
            saf = await safety_check(self.session, chain, addr)
            pairs = await dexscreener_batch(self.session, [addr], chain)
            pair = pairs.get(addr)
            snap = pair_to_snapshot(addr, pair, now_ms()) if pair else None
            if saf:
                saf = dict(saf, mint=addr, checked_at=now_ms())
                self.safety[addr] = saf
                db.save_safety(self.con, saf)
            fails = self.strat.safety_fails(addr, snap["liq"] if snap else 0) if saf else ["safety check unavailable"]
            if snap:
                lines.append("Pool %s · mkt cap %s · 1h %s · %s" % (
                    notify.money(snap["liq"]), notify.money(snap["fdv"]),
                    "%+.0f%%" % snap["pc_h1"] if snap["pc_h1"] is not None else "n/a", chain.title()))
                # record it so the paper traders and backtests see it too
                if not self.con.execute("SELECT 1 FROM coins WHERE mint=?", (addr,)).fetchone():
                    created = pair.get("pairCreatedAt") or now_ms()
                    self.con.execute("INSERT INTO coins(mint, symbol, name, graduated_at, grad_price, last_seen, status, chain, added_at, source) "
                                     "VALUES(?,?,?,?,?,?,'watching',?,?,'fomo')",
                                     (addr, snap["_symbol"], snap["_name"], int(created), snap["price"], now_ms(), chain, now_ms()))
                coin = dict(self.con.execute("SELECT * FROM coins WHERE mint=?", (addr,)).fetchone())
                for name, st in self.strats.items():
                    st.on_snapshot(dict(snap), coin)
                    if kind == "cluster" and st.p.get("ENTRY_MODE") == "signal":
                        a = st.signal_enter(addr, "Fomo cluster: " + ", ".join("@" + h for h, _ in buyers[:4]))
                        if a:
                            await self.act(a, name)
            if saf and saf.get("lp_na") and not fails:
                lines.append("✅ Passes safety (Uniswap v4 pool, so LP lock doesn't apply)")
            else:
                lines.append("✅ Passes safety" if not fails else "⛔ Fails safety: " + "; ".join(fails))
            lines.append(notify.dex_link(addr, chain))
        else:
            lines.append("Chain: %s (safety checks cover Solana and Robinhood Chain)" % html.escape(chain or "unknown"))
        lines.append("Following a crowd means you're buying after them. Check the chart before acting.")
        await notify.send(self.session, "\n".join(lines))

    def track_coin(self, addr, chain):
        """Start recording prices for a coin one of your list traders bought (feeds the Follow test)."""
        m = followtest.norm(addr, chain)
        if not m or m in self.tracking:
            return
        self.tracking.add(m)
        if self.con.execute("SELECT 1 FROM coins WHERE mint=?", (m,)).fetchone():
            return
        asyncio.create_task(self._track(m, "robinhood" if m.startswith("0x") else "solana"))

    async def _track(self, m, chain):
        try:
            pair = (await dexscreener_batch(self.session, [m], chain)).get(m)
            if not pair or self.con.execute("SELECT 1 FROM coins WHERE mint=?", (m,)).fetchone():
                return
            snap = pair_to_snapshot(m, pair, now_ms())
            self.con.execute("INSERT INTO coins(mint, symbol, name, graduated_at, grad_price, last_seen, status, chain, added_at, source) "
                             "VALUES(?,?,?,?,?,?,'watching',?,?,'fomo')",
                             (m, snap["_symbol"], snap["_name"], int(pair.get("pairCreatedAt") or now_ms()), snap["price"],
                              now_ms(), chain, now_ms()))
        except Exception:  # noqa: BLE001
            log.exception("Tracking %s failed", m)

    async def run_followtest(self):
        if self.sweeping:
            await notify.send(self.session, "A test is already running. Results will show up here when it's done.")
            return
        self.sweeping = True
        await notify.send(self.session, "🧪 Testing %d Follow rules on the last 7 days of Fomo trades. Takes a minute or two." % len(followtest.VARIANTS))
        try:
            res = await asyncio.to_thread(followtest.run, 7)
            await notify.send(self.session, followtest.text(res))
        except Exception as e:  # noqa: BLE001
            log.exception("Follow test failed")
            await notify.send(self.session, "Follow test failed: %s" % html.escape(str(e)[:300]))
        finally:
            self.sweeping = False

    async def scorecard_loop(self):
        while True:
            try:
                if fomo.key():
                    await fomo.price_calls(self.session, self.con, dexscreener_batch, pair_to_snapshot)
            except Exception:  # noqa: BLE001
                log.exception("Scorecard pricing failed")
            await asyncio.sleep(60)

    async def run_sweep(self):
        if self.sweeping:
            await notify.send(self.session, "A rule test is already running. Results will show up here when it's done.")
            return
        self.sweeping = True
        await notify.send(self.session, "🧪 Testing 17 rule variations on the last 7 days of recorded coins. This takes 10 to 30 minutes on the small server; alerts keep working meanwhile.")
        try:
            res = await asyncio.to_thread(sweep.run, 7)
            await notify.send(self.session, sweep.text(res))
        except Exception as e:  # noqa: BLE001
            log.exception("Sweep failed")
            await notify.send(self.session, "Rule test failed: %s" % html.escape(str(e)[:300]))
        finally:
            self.sweeping = False

    async def run_scan(self):
        await notify.send(self.session, "🔎 Running the Fomo trend scan (about 40 traders, roughly 10,000 of your 250,000 monthly credits). Takes a minute or two.")
        try:
            res = await fomo.trend_scan(self.session, self.con)
            await notify.send(self.session, fomo.scan_text(res))
        except Exception as e:  # noqa: BLE001
            await notify.send(self.session, "Fomo scan failed: %s" % html.escape(str(e)[:300]))

    # ------------------------------------------------------------ telegram commands + daily summary
    async def telegram_loop(self):
        if not config.TELEGRAM_BOT_TOKEN:
            return
        offset = None
        while True:
            try:
                for u in await notify.get_updates(self.session, offset):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str((msg.get("chat") or {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                        continue
                    cmd = (msg.get("text") or "").strip().split()[0].lower() if msg.get("text") else ""
                    if cmd in ("/status", "status"):
                        await notify.send(self.session, report.status_text(self.con, self.strats))
                    elif cmd in ("/today", "today"):
                        await notify.send(self.session, report.period_text(self.con, hours=24))
                    elif cmd in ("/week", "week"):
                        await notify.send(self.session, report.period_text(self.con, hours=24 * 7))
                    elif cmd in ("/all", "all"):
                        await notify.send(self.session, report.period_text(self.con, hours=24 * 3650))
                    elif cmd in ("/fomo", "fomo"):
                        await notify.send(self.session, fomo.feed_report(self.con, 24))
                    elif cmd in ("/scan", "scan"):
                        asyncio.create_task(self.run_scan())
                    elif cmd in ("/traders", "traders", "/scorecard"):
                        await notify.send(self.session, fomo.scorecard_text(self.con))
                    elif cmd in ("/testfollow", "testfollow", "/followtest", "/test_follow", "/follow"):
                        asyncio.create_task(self.run_followtest())
                    elif cmd in ("/sweep", "sweep", "/test", "test"):
                        asyncio.create_task(self.run_sweep())
                    elif cmd in ("/help", "/start", "help"):
                        await notify.send(self.session, "Commands:\n/status: feed health and open trades\n/today: last 24 hours\n/week: last 7 days\n/all: since the start\n/fomo: what Fomo traders bought in the last 24h (free)\n/scan: full trend scan of your Fomo traders (uses credits)\n/traders: scorecard of your Fomo traders' buys\n/test: replay recorded coins through 17 rule variations (10 to 30 min)\n/testfollow: test the Follow rules on your traders' buys (1 to 2 min)")
                    elif cmd.startswith("/"):
                        await notify.send(self.session, "I don't know %s. Send /help for the list. (If a new command doesn't work, run: gatekeeper update)" % html.escape(cmd[:40]))
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram poll error: %s", e)
                await asyncio.sleep(10)

    async def daily_loop(self):
        while True:
            now = datetime.now(TZ)
            key = now.strftime("%Y-%m-%d")
            if now.hour == 21 and db.kv_get(self.con, "summary_sent") != key:
                await notify.send(self.session, "📊 <b>Daily summary</b>\n" + report.period_text(self.con, hours=24, header=False))
                db.kv_set(self.con, "summary_sent", key)
                if fomo.key():
                    await notify.send(self.session, fomo.feed_report(self.con, 24))
                if now.weekday() == 6:
                    await notify.send(self.session, "🗓 <b>Weekly recap</b>\n" + report.period_text(self.con, hours=24 * 7, header=False))
                    if fomo.key():
                        await notify.send(self.session, fomo.scorecard_text(self.con))
                    if fomo.key():
                        await self.run_followtest()
                        await self.run_scan()
            if now.hour == 4 and db.kv_get(self.con, "pruned") != key:
                keep = int(float(__import__("os").environ.get("GK_KEEP_DAYS", "14")))
                self.con.execute("DELETE FROM snapshots WHERE ts < ?", (now_ms() - keep * 86400000,))
                fomo.ensure_schema(self.con)
                self.con.execute("DELETE FROM fomo_events WHERE ts < ?", (now_ms() - 30 * 86400000,))
                db.kv_set(self.con, "pruned", key)
            await asyncio.sleep(60)

    async def run(self):
        async with aiohttp.ClientSession(headers={"User-Agent": "gatekeeper-bot/1.0"}) as session:
            self.session = session
            await notify.send(session, "🤖 Gatekeeper bot started. Strategies: %s. %d open paper trades. Send /help for commands."
                              % (", ".join(self.strats), sum(len(s.positions) for s in self.strats.values())))
            await asyncio.gather(
                fomo.Feed(self.con, self.on_fomo_alert, self.track_coin).run(),
                pumpportal_stream(self.on_event, config.PUMPPORTAL_API_KEY),
                self.poll_loop(), self.safety_loop(), self.telegram_loop(), self.daily_loop(), self.scorecard_loop())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Runner().run())
