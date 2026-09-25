"""The always-on service: recorder + live paper trader + Telegram."""
import asyncio
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from . import config, db, notify, report
from .sources import dexscreener_batch, pair_to_snapshot, pumpportal_stream, rugcheck
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
        self.safety_min_age = min(s.p["MIN_AGE_MIN"] for s in self.strats.values())
        self.safety_min_liq = min(s.p["MIN_LIQ_USD"] for s in self.strats.values())
        self.session = None
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
        mints = list(coins)
        seen = set()
        for i in range(0, len(mints), 30):
            batch = mints[i:i + 30]
            pairs = await dexscreener_batch(self.session, batch)
            for mint, pair in pairs.items():
                if mint not in coins:
                    continue
                s = pair_to_snapshot(mint, pair, now)
                if not s["price"]:
                    continue
                seen.add(mint)
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
                age_min = (now - c["graduated_at"]) / MIN
                if s["liq"] < p["DEAD_LIQ_USD"] and age_min > 30 and not self.any_open(mint):
                    self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
            if i + 30 < len(mints):
                await asyncio.sleep(1)
        # coins DexScreener never listed, or that aged out
        for mint, c in coins.items():
            age_min = (now - c["graduated_at"]) / MIN
            if self.any_open(mint):
                continue
            if age_min > p["WATCH_HOURS"] * 60:
                self.con.execute("UPDATE coins SET status='expired' WHERE mint=?", (mint,))
            elif mint not in seen and age_min > 30 and (now - (c["last_seen"] or 0)) / MIN > 20:
                self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
        for name, st in self.strats.items():
            for a in st.on_tick(now):
                await self.act(a, name)
        db.kv_set(self.con, "last_poll", now)
        db.kv_set(self.con, "watching", len(mints))

    # ------------------------------------------------------------ safety checks
    async def safety_loop(self):
        while True:
            try:
                now = now_ms()
                rows = self.con.execute(
                    "SELECT c.mint, c.graduated_at, "
                    "(SELECT liq FROM snapshots s WHERE s.mint=c.mint ORDER BY ts DESC LIMIT 1) AS liq "
                    "FROM coins c WHERE c.status='watching'").fetchall()
                for r in rows:
                    age = (now - r["graduated_at"]) / MIN
                    if age < self.safety_min_age - 8 or (r["liq"] or 0) < self.safety_min_liq * 0.7:
                        continue
                    have = self.safety.get(r["mint"])
                    if have and now - have["checked_at"] < 45 * MIN:
                        continue
                    res = await rugcheck(self.session, r["mint"])
                    if res:
                        res.update(mint=r["mint"], checked_at=now_ms())
                        self.safety[r["mint"]] = res
                        self.con.execute(
                            "INSERT OR REPLACE INTO safety VALUES(:mint,:checked_at,:mint_revoked,:freeze_revoked,"
                            ":lp_locked,:top10,:insiders,:danger,:rc_score)", res)
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
                    elif cmd in ("/help", "/start", "help"):
                        await notify.send(self.session, "Commands:\n/status: feed health and open trades\n/today: last 24 hours\n/week: last 7 days\n/all: since the start")
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
                if now.weekday() == 6:
                    await notify.send(self.session, "🗓 <b>Weekly recap</b>\n" + report.period_text(self.con, hours=24 * 7, header=False))
            if now.hour == 4 and db.kv_get(self.con, "pruned") != key:
                keep = int(float(__import__("os").environ.get("GK_KEEP_DAYS", "90")))
                self.con.execute("DELETE FROM snapshots WHERE ts < ?", (now_ms() - keep * 86400000,))
                db.kv_set(self.con, "pruned", key)
            await asyncio.sleep(60)

    async def run(self):
        async with aiohttp.ClientSession(headers={"User-Agent": "gatekeeper-bot/1.0"}) as session:
            self.session = session
            await notify.send(session, "🤖 Gatekeeper bot started. Strategies: %s. %d open paper trades. Send /help for commands."
                              % (", ".join(self.strats), sum(len(s.positions) for s in self.strats.values())))
            await asyncio.gather(
                pumpportal_stream(self.on_event, config.PUMPPORTAL_API_KEY),
                self.poll_loop(), self.safety_loop(), self.telegram_loop(), self.daily_loop())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Runner().run())
