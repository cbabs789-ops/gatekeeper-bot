"""The always-on service: recorder + live paper trader + Telegram."""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

import html
import re

from . import analyst, bounce, check, config, db, events, fomo, followtest, hold, notify, report, research, risk, stats, sweep, trench, trends, web, winmodel
from .sources import CHAINS, EVM_RE, SOL_RE, DexFailed, dexscreener_batch, pair_to_snapshot, pumpportal_stream, safety_check
from .strategy import MIN, PaperBroker, Position, Strategy

log = logging.getLogger("gatekeeper")
TZ = ZoneInfo(config.TIMEZONE)
POLL_SEC = 30
FAST_SEC = float(config.os.environ.get("GK_FAST_SEC", "5"))   # how often open paper trades are re-priced


def safety_worse(old, new):
    """Red flags that appeared since the last check on a coin we hold, or None."""
    out = []
    if old.get("mint_revoked") and not new.get("mint_revoked"): out.append("mint authority turned on")
    if old.get("freeze_revoked") and not new.get("freeze_revoked"): out.append("freeze or blacklist turned on")
    if new.get("danger") and new.get("danger") != old.get("danger"): out.append(str(new["danger"])[:80])
    # a number that is simply missing from one of the two reports is not a red flag: compare only real readings
    def both(k):
        return old.get(k) is not None and new.get(k) is not None
    if both("top10") and new["top10"] >= old["top10"] + 10: out.append("top 10 holders jumped to %.0f%%" % new["top10"])
    if not new.get("lp_na") and both("lp_locked") and old["lp_locked"] >= 90 and new["lp_locked"] < 50: out.append("LP unlocked")
    if both("insiders") and new["insiders"] >= old["insiders"] + 10: out.append("insider wallets jumped to %d" % new["insiders"])
    return "; ".join(out) or None


def now_ms():
    return int(time.time() * 1000)


def today_local():
    return datetime.now(TZ).strftime("%Y-%m-%d %H")   # hourly buckets for launch counts


class Runner:
    def __init__(self):
        self.con = db.connect()
        self.safety = {r["mint"]: dict(r) for r in self.con.execute("SELECT * FROM safety")}
        look = lambda m: self.safety.get(m)  # noqa: E731
        self.strats = {name: Strategy(config.strategy_params(preset=name), look) for name in config.ENABLED_PRESETS + config.SHADOW_PRESETS}
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
        self.sell_warned = {}
        self.trends = trends.Trends(self)
        self.trench = trench.Trench(self)
        for st in self.strats.values():
            st.insider_lookup, st.on_skip = self.trench.insiders_in, self.trench.log_skip
        self.events = events.Events(self)
        self.hold = hold.Hold(self)
        self.analyst = analyst.Analyst(self)
        self._load_routes()
        risk.ensure(self.con)
        self.rug_model = risk.load(self.con)
        self.win_model = winmodel.load(self.con)
        for st in self.strats.values():
            st.risk_model = self.rug_model or None
            st.win_model = self.win_model or None
        self.epochs = {}
        for name, st in self.strats.items():
            self._epoch(name, st)
        for name, st in self.strats.items():
            self._restore(name, st)
        self._replay = None
        self._close_orphans()

    def _load_routes(self):
        try:
            g = json.loads(db.kv_get(self.con, "tg_group") or "{}")
        except ValueError:
            g = {}
        notify.ROUTES.update(chat=g.get("chat"), threads=g.get("threads") or {})

    async def setup_topics(self, msg):
        """Run from inside a Telegram group: make one topic per kind of alert and send everything there from now on."""
        chat = msg.get("chat") or {}
        if chat.get("type") not in ("group", "supergroup"):
            await notify.send(self.session, "Send /setup inside a Telegram group (with Topics turned on), not here.")
            return
        cid = chat["id"]
        threads = {}
        if chat.get("is_forum"):
            try:
                old = json.loads(db.kv_get(self.con, "tg_group") or "{}")
            except ValueError:
                old = {}
            keep = old.get("threads", {}) if str(old.get("chat")) == str(cid) else {}
            try:
                for key, name in notify.TOPICS:
                    if keep.get(key):
                        threads[key] = keep[key]
                        continue
                    t = await notify.call(self.session, "createForumTopic", {"chat_id": cid, "name": name})
                    threads[key] = t["message_thread_id"]
            except Exception as e:  # noqa: BLE001
                await notify.send(self.session, "I couldn't create topics (%s). Make me an admin with the \"Manage topics\" permission, then send /setup again." % html.escape(str(e)[:150]), chat_id=cid)
                return
        db.kv_set(self.con, "tg_group", json.dumps({"chat": cid, "threads": threads}))
        self._load_routes()
        if threads:
            what = {"trades": "every paper buy, partial sell and sell", "fomo": "Fomo clusters, trending coins and your traders selling",
                    "news": "Trump's posts and trend picks", "events": "upcoming speeches and summits with coins to watch",
                    "trench": "trench wallet clusters", "reports": "daily and weekly summaries, test results, updates and stats"}
            for key, name in notify.TOPICS:
                notify.REPLY.set((cid, threads[key]))
                await notify.send(self.session, "%s: this topic gets %s." % (name, what[key]))
            notify.REPLY.set((cid, None))
            await notify.send(self.session, "✅ Set up. Alerts now go to their own topics in this group. Commands work in any topic and the answer comes back to that topic. Send /unsetup to go back to the private chat.", chat_id=cid)
        else:
            await notify.send(self.session, "✅ Linked this group. Turn on Topics in the group settings and send /setup again to sort alerts into separate topics.", chat_id=cid)

    def _close_orphans(self):
        """Paper trades left open by a strategy that's since been turned off: nothing manages them, so close them
        at the last price we recorded (with normal selling costs) instead of letting them sit open forever."""
        live = list(self.strats)
        rows = self.con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NULL AND COALESCE(run_id,'main') NOT IN (%s)"
                                % ",".join("?" * len(live)), live).fetchall()
        p = self.p
        for r in rows:
            snap = self.con.execute("SELECT ts, price, liq FROM snapshots WHERE mint=? AND price>0 AND liq>0 ORDER BY ts DESC LIMIT 1", (r["mint"],)).fetchone()
            legs = json.loads(r["legs"] or "[]")
            proceeds = sum(l.get("usd") or 0 for l in legs[1:] if l.get("side") == "sell")
            sold = sum(l.get("qty") or 0 for l in legs[1:] if l.get("side") == "sell")
            qty = max((r["qty"] or 0) - sold, 0)
            price, liq, ts = (snap["price"], snap["liq"], snap["ts"]) if snap and snap["price"] else (0, 0, now_ms())
            got = PaperBroker(p).sell(price, liq, qty) if price and liq else 0.0
            proceeds += got
            pnl = proceeds - (r["size_usd"] or 0)
            reason = "Closed: its strategy (%s) is turned off" % (r["run_id"] or "main")
            legs.append({"ts": ts, "side": "sell", "spot": price, "usd": got, "why": reason})
            self.con.execute("UPDATE trades SET closed_at=?, proceeds_usd=?, pnl_usd=?, pnl_pct=?, exit_reason=?, legs=? WHERE id=?",
                             (max(ts, r["opened_at"]), proceeds, pnl, pnl / r["size_usd"] * 100 if r["size_usd"] else 0, reason,
                              json.dumps(legs), r["id"]))
            log.info("Closed orphaned %s trade $%s: %+.2f", r["run_id"], r["symbol"], pnl)

    def _epoch(self, name, st):
        """When a strategy's rules last changed, so results can be shown for the current rules only."""
        cur = json.loads(json.dumps(st.p, sort_keys=True, default=str))
        h = hashlib.sha1(json.dumps(cur, sort_keys=True).encode()).hexdigest()[:12]
        old, ep = db.kv_get(self.con, "rules_hash_" + name), db.kv_get(self.con, "rules_since_" + name)
        # A rule only "changed" if a setting this bot already had now has a different value. A brand-new setting added by an
        # update (used only by some other bot) is not a change. Without the saved settings we cannot tell, so we assume no change.
        try:
            prev = json.loads(db.kv_get(self.con, "rules_params_" + name) or "null")
        except (TypeError, ValueError):
            prev = None
        changed = isinstance(prev, dict) and any(k in cur and cur[k] != v for k, v in prev.items())
        if db.kv_get(self.con, "rules_fix_oct7_" + name) is None:
            # One-time repair: the Oct 7 update added settings for the Strength bot, which wrongly restarted every bot's
            # "current rules" date. Main's rules last really changed on Oct 4; the others had no recorded change.
            rc = int(float(db.kv_get(self.con, "rules_changed_" + name) or 0))
            if rc >= 1791345600000:
                db.kv_set(self.con, "rules_changed_" + name, 1791145200000 if name == "main" else 0)
            db.kv_set(self.con, "rules_fix_oct7_" + name, 1)
        if old is None:
            # first run with this feature: rules customized in the config file count as changed now
            pre = "GK_" + ("%s_" % name.upper() if name != "main" else "")
            custom = any(config.os.environ.get(pre + k) not in (None, "") for k in config.STRATEGY_DEFAULTS)
            ep = now_ms() if custom else 0
        elif changed and config.os.environ.get("GK_AUTO_RESET", "0") == "1":
            ep = now_ms()                     # off by default: the count only restarts when you send /reset
        # Remember WHEN the rules last changed, so reports can say which numbers belong to the current rules.
        # (This does not reset the dashboard count; only /reset does that.)
        if changed:
            db.kv_set(self.con, "rules_changed_" + name, now_ms())
        elif name == "main" and db.kv_get(self.con, "rules_changed_main") is None:
            db.kv_set(self.con, "rules_changed_main", 1791145200000)       # Oct 4 2026: Main moved to skip rug risk 35%+
        db.kv_set(self.con, "rules_params_" + name, json.dumps(cur, sort_keys=True))
        db.kv_set(self.con, "rules_hash_" + name, h)
        if str(int(float(ep or 0))) != str(db.kv_get(self.con, "rules_since_" + name)):
            db.kv_set(self.con, "rules_since_prev_" + name, db.kv_get(self.con, "rules_since_" + name) or 0)
        db.kv_set(self.con, "rules_since_" + name, int(float(ep or 0)))
        self.epochs[name] = int(float(ep or 0))

    def any_open(self, mint):
        return any(mint in st.positions for st in self.strats.values())

    # ------------------------------------------------------------ restore after restart
    def _restore(self, name, strat):
        since = now_ms() - 6 * 3600 * 1000
        coins = {r["mint"]: dict(r) for r in self.con.execute("SELECT * FROM coins WHERE status='watching'")}
        if getattr(self, "_replay", None) is None:      # read once, replay into every strategy (was re-read per strategy)
            self._replay = self.con.execute("SELECT * FROM snapshots WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        rows = self._replay
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
        # coins this strategy decided to skip for good (too risky, insiders, low win score): a restart must not forget
        self.con.execute("CREATE TABLE IF NOT EXISTS skips (run_id TEXT, mint TEXT, ts INTEGER, PRIMARY KEY (run_id, mint))")
        self.con.execute("DELETE FROM skips WHERE ts<?", (now_ms() - 3 * 86400000,))
        for r in self.con.execute("SELECT mint FROM skips WHERE run_id=?", (name,)):
            strat.traded.add(r["mint"])
        strat.on_perm_skip = lambda mint, _n=name: self.con.execute(
            "INSERT OR IGNORE INTO skips VALUES(?,?,?)", (_n, mint, now_ms()))
        for r in self.con.execute("SELECT * FROM trades WHERE " + sel + " AND closed_at IS NULL", (name,)):
            legs = json.loads(r["legs"] or "[]")
            pos = Position(r["mint"], r["symbol"], r["opened_at"], r["entry_price"], legs[0]["spot"] if legs else r["entry_price"],
                           r["size_usd"], r["qty"], r["qty"], why=r["why_entered"] or "", legs=legs, trade_id=r["id"])
            for leg in legs[1:]:
                if leg["side"] == "sell":
                    pos.proceeds += leg["usd"]
                    pos.qty_open = pos.qty_open - leg["qty"] if leg.get("qty") else pos.qty_total / 2
                    pos.took_half = True
                    if "moonbag" in (leg.get("why") or ""):
                        pos.moon = True
            cs = strat.coins.get(r["mint"])
            # the highest price SINCE WE BOUGHT, not the coin's all-time high (that made restarts fake a "was up 30%" and sell)
            hi = self.con.execute("SELECT MAX(price) FROM snapshots WHERE mint=? AND ts>=? AND liq>0", (r["mint"], r["opened_at"])).fetchone()[0]
            pos.peak_after = max(pos.spot_at_entry, hi or 0)
            m = re.search(r"rug risk (\d+)%", pos.why or "")
            pos.risk = float(m.group(1)) if m else None
            m = re.search(r"data target ([\d.]+)x", pos.why or "")
            pos.target_x = float(m.group(1)) if m else None
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
        risk_snaps = {}
        feed_down = False
        for bi, (chain, batch) in enumerate(batches):
            pairs = await dexscreener_batch(self.session, batch, chain)
            if isinstance(pairs, DexFailed):
                seen.update(batch)             # the feed didn't answer: unknown, not dead
                feed_down = True
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
                                 "grad_price=COALESCE(grad_price,?), last_seen=?, socials=? WHERE mint=?",
                                 (s["_symbol"], s["_name"], s["_pair"], s["_dex"], s["price"], now, s["_socials"], mint))
                if not c.get("symbol"):
                    c["symbol"] = s["_symbol"]
                self.con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (mint, now, s["price"], s["liq"], s["fdv"], s["vol_m5"], s["vol_h1"], s["buys_m5"],
                                  s["sells_m5"], s["buys_h1"], s["sells_h1"], s["pc_m5"], s["pc_h1"]))
                grad_age = (now - (c.get("graduated_at") or now)) / MIN
                risk.log(self.con, mint, s, self.safety.get(mint), s.get("_socials"), grad_age, now)
                risk_snaps[mint] = (s["price"], s["liq"])
                for name, st in self.strats.items():
                    for a in st.on_snapshot(s, c):
                        await self.act(a, name)
                age_min = (now - (c.get("added_at") or c["graduated_at"])) / MIN
                if s["liq"] < p["DEAD_LIQ_USD"] and age_min > 30 and not self.any_open(mint):
                    self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
                    risk.mark_dead(self.con, mint, now)
            if bi + 1 < len(batches):
                await asyncio.sleep(1)
        risk.update_outcomes(self.con, risk_snaps, now)
        # coins DexScreener never listed, or that aged out
        surv_liq = float(config.os.environ.get("GK_SURVIVOR_LIQ_USD", "25000"))
        surv_hours = float(config.os.environ.get("GK_SURVIVOR_WATCH_HOURS", "48"))
        for mint, c in coins.items():
            age_min = (now - (c.get("added_at") or c["graduated_at"])) / MIN
            if self.any_open(mint):
                continue
            # coins that survived with a real pool keep being watched (up to 48h) for the Survivor strategy
            survivor = self.last_liq.get(mint, 0) >= surv_liq and age_min <= surv_hours * 60
            if age_min > p["WATCH_HOURS"] * 60 and not survivor:
                self.con.execute("UPDATE coins SET status='expired' WHERE mint=?", (mint,))
            elif mint not in seen and age_min > 30 and (now - (c["last_seen"] or 0)) / MIN > 20:
                self.con.execute("UPDATE coins SET status='dead' WHERE mint=?", (mint,))
                risk.mark_dead(self.con, mint, now)
        if self.tick % 60 == 0:         # every 30 minutes: relearn rug risk from finished coins
            try:
                self.rug_model = await asyncio.to_thread(lambda: risk.build(db.connect()))
                for st in self.strats.values():
                    st.risk_model = self.rug_model
            except Exception:  # noqa: BLE001
                log.exception("Rug model build failed")
            try:
                was = bool((self.win_model or {}).get("trusted"))
                self.win_model = await asyncio.to_thread(lambda: winmodel.build(db.connect()))
                for st in self.strats.values():
                    st.win_model = self.win_model
                if self.win_model.get("trusted") and not was:
                    await notify.send(self.session, "🎯 The win-score model passed its honest check and is now filtering trades for the win-score experiment bot. Send /winscore to see what it learned.")
            except Exception:  # noqa: BLE001
                log.exception("Win model build failed")
            try:
                await asyncio.to_thread(lambda: bounce.build(db.connect()))
            except Exception:  # noqa: BLE001
                log.exception("Bounce model build failed")
        for name, st in self.strats.items():
            # "no price for 5 minutes" only means the pool is gone if the feed was actually answering
            for a in ([] if feed_down else st.on_tick(now)):
                await self.act(a, name)
            if self.tick % 20 == 0:
                st.prune(now, 30 * MIN)
        db.kv_set(self.con, "last_poll", now)
        db.kv_set(self.con, "watching", len(mints))

    async def fast_loop(self):
        """Price coins with open paper trades every 5 seconds, so exits and live P/L are sharper."""
        while True:
            await asyncio.sleep(FAST_SEC)
            try:
                mints = {m for st in self.strats.values() for m in st.positions}
                if not mints:
                    continue
                now = now_ms()
                coins = {r["mint"]: dict(r) for r in self.con.execute(
                    "SELECT * FROM coins WHERE mint IN (%s)" % ",".join("?" * len(mints)), list(mints))}
                for chain in CHAINS:
                    batch = [m for m in mints if m in coins and (coins[m].get("chain") or "solana") == chain]
                    if not batch:
                        continue
                    pairs = {}
                    for i in range(0, len(batch), 30):
                        pairs.update(await dexscreener_batch(self.session, batch[i:i + 30], chain))
                    for mint, pair in pairs.items():
                        if mint not in coins:
                            continue
                        snap = pair_to_snapshot(mint, pair, now)
                        if not snap["price"]:
                            continue
                        self.last_liq[mint] = snap["liq"]
                        self.con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (mint, now, snap["price"], snap["liq"], snap["fdv"], snap["vol_m5"], snap["vol_h1"],
                                          snap["buys_m5"], snap["sells_m5"], snap["buys_h1"], snap["sells_h1"], snap["pc_m5"], snap["pc_h1"]))
                        for name, st in self.strats.items():
                            if mint in st.positions:
                                for a in st.on_snapshot(dict(snap), coins[mint]):
                                    await self.act(a, name)
                db.kv_set(self.con, "last_fast_poll", now)
            except Exception:  # noqa: BLE001
                log.exception("Fast price loop failed")

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
                    holding = self.any_open(r["mint"])
                    if have and now - have["checked_at"] < (10 if holding else 45) * MIN:
                        continue
                    res = await safety_check(self.session, r["chain"] or "solana", r["mint"])
                    if res:
                        res.update(mint=r["mint"], checked_at=now_ms())
                        why = safety_worse(have, res) if holding and have else None
                        self.safety[r["mint"]] = res
                        db.save_safety(self.con, res)
                        if why:
                            log.info("Safety changed on %s: %s", r["mint"], why)
                            for name, st in self.strats.items():
                                for a in st.safety_exit(r["mint"], why, now_ms()):
                                    await self.act(a, name)
                    await asyncio.sleep(2)
            except Exception:  # noqa: BLE001
                log.exception("Safety loop error")
            await asyncio.sleep(20)

    # ------------------------------------------------------------ actions -> db + telegram
    def rug_risk(self, mint):
        """(score 0-100, how it was made) for a coin right now, or (None, '') if there's no snapshot."""
        r = self.con.execute("SELECT * FROM snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        c = self.con.execute("SELECT graduated_at, socials FROM coins WHERE mint=?", (mint,)).fetchone()
        if not r or not c:
            return None, ""
        age = (now_ms() - (c["graduated_at"] or now_ms())) / MIN
        row = risk.live_row(dict(r), self.safety.get(mint), c["socials"], age)
        return risk.score(self.rug_model, row)

    def supply(self, mint):
        """Token supply (market cap / price) from the latest snapshot, for showing market caps."""
        r = self.con.execute("SELECT price, fdv FROM snapshots WHERE mint=? AND price>0 AND fdv>0 ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        return r["fdv"] / r["price"] if r else None

    def _bg(self, coro):
        """Send in the background: a slow Telegram call must never hold up the loop that watches prices and stops."""
        t = asyncio.create_task(coro)
        self._tasks = getattr(self, "_tasks", set())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def act(self, a, name="main"):
        pos = a["pos"]
        alert = name in config.ALERT_PRESETS
        tag = "" if name == "main" else "[%s] " % name.upper()
        if a["type"] == "buy":
            how = ("learned from %s coins" % format(self.rug_model.get("n", 0), ",")) if (self.rug_model or {}).get("counts") else "early estimate"
            if pos.risk is None:
                pos.risk, how = self.rug_risk(pos.mint)
            if pos.risk is not None:
                pos.why = (pos.why + "; " if pos.why else "") + "rug risk %d%% (%s)%s%s" % (
                    pos.risk, how, ", smaller bet: $%g" % pos.size_usd if pos.size_usd < self.strats[name].p["POSITION_USD"] else "",
                    ", data target %gx" % pos.target_x if pos.target_x else "")
            cur = self.con.execute(
                "INSERT INTO trades(mode, run_id, mint, symbol, opened_at, entry_price, size_usd, qty, why_entered, legs) "
                "VALUES('live',?,?,?,?,?,?,?,?,?)",
                (name, pos.mint, pos.symbol, pos.opened_at, pos.entry_price, pos.size_usd, pos.qty_total, pos.why, json.dumps(pos.legs)))
            pos.trade_id = cur.lastrowid
            if name == "main":                 # the AI trader gives its own verdict on everything Main buys
                asyncio.create_task(self.analyst.on_main_buy(pos))
            if alert:
                self._bg(notify.send(self.session, tag + notify.fmt_buy(pos, a["spot"], a["liq"], self.strats[name].p, self.supply(pos.mint))))
        elif a["type"] == "partial":
            self.con.execute("UPDATE trades SET legs=? WHERE id=?", (json.dumps(pos.legs), pos.trade_id))
            if alert:
                self._bg(notify.send(self.session, tag + notify.fmt_partial(pos, a["spot"], a["usd"], a.get("why"), self.supply(pos.mint))))
        elif a["type"] == "close":
            self.con.execute("UPDATE trades SET closed_at=?, proceeds_usd=?, pnl_usd=?, pnl_pct=?, exit_reason=?, legs=? WHERE id=?",
                             (pos.closed_at, pos.proceeds, pos.pnl_usd, pos.pnl_pct, pos.exit_reason, json.dumps(pos.legs), pos.trade_id))
            if alert:
                self._bg(notify.send(self.session, tag + notify.fmt_close(pos, a["spot"], a["reason"], self.supply(pos.mint))))
        try:
            web.HUB.publish({"type": a["type"], "strategy": name, "symbol": pos.symbol,
                             "usd": round(a.get("usd") or (pos.size_usd if a["type"] == "buy" else pos.proceeds), 2),
                             "pnl": round(pos.pnl_usd, 2) if a["type"] == "close" and pos.pnl_usd is not None else None,
                             "reason": a.get("reason") or a.get("why") or ""})
        except Exception:  # noqa: BLE001
            log.exception("dashboard push")

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
                    for a in st.on_snapshot(dict(snap), coin):     # a buy or exit triggered here must be recorded
                        await self.act(a, name)
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

    def _research(self):
        return research.text(db.connect())

    # ------------------------------------------------------------ real fills vs paper
    def record_fill(self, args):
        """/fill SYMBOL PRICE  (price per token, or market cap like 250k / 1.2m). First fill = your buy, next = your sell."""
        if len(args) < 2:
            return ("Send it like: /fill PIKASTR 0.00123 (price per coin) or /fill PIKASTR 250k (market cap).\n"
                    "Your first /fill for a coin is your buy, the next one is your sell.")
        sym = args[0].lstrip("$").upper()
        raw = args[1].lower().replace("$", "").replace(",", "")
        mult = 1e3 if raw.endswith("k") else 1e6 if raw.endswith("m") else 1
        try:
            val = float(raw.rstrip("km")) * mult
        except ValueError:
            return "Couldn't read %s as a number." % html.escape(args[1])
        t = self.con.execute("SELECT * FROM trades WHERE mode='live' AND upper(symbol)=? ORDER BY opened_at DESC LIMIT 1", (sym,)).fetchone()
        if not t:
            return "No paper trade found for $%s. Use the symbol from the alert." % html.escape(sym)
        snap = self.con.execute("SELECT price, fdv FROM snapshots WHERE mint=? ORDER BY ts DESC LIMIT 1", (t["mint"],)).fetchone()
        is_mc = mult > 1 or (snap and snap["price"] and val > snap["price"] * 1000)
        if is_mc:
            if not snap or not snap["price"] or not snap["fdv"]:
                return "Can't convert market cap for $%s (no recent price). Send the price per coin instead." % html.escape(sym)
            val = val * snap["price"] / snap["fdv"]
        self.con.execute("CREATE TABLE IF NOT EXISTS real_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, ts INTEGER, side TEXT, price REAL)")
        has_buy = self.con.execute("SELECT 1 FROM real_fills WHERE trade_id=? AND side='buy'", (t["id"],)).fetchone()
        side = "sell" if has_buy else "buy"
        self.con.execute("INSERT INTO real_fills(trade_id, ts, side, price) VALUES(?,?,?,?)", (t["id"], now_ms(), side, val))
        legs = json.loads(t["legs"] or "[]")
        if side == "buy":
            paper = legs[0]["spot"] if legs else t["entry_price"]
            diff = (val / paper - 1) * 100
            return ("✍️ Your buy of $%s logged. Paper bot bought at %s, you at %s: you paid %+.1f%% %s." % (
                html.escape(t["symbol"]), notify.price(paper), notify.price(val), diff, "more" if diff > 0 else "less")
                + "\nSend /fill %s PRICE again when you sell." % html.escape(t["symbol"]))
        sells = [g for g in legs if g.get("side") == "sell"]
        if not sells:
            return "✍️ Your sell of $%s logged. The paper bot is still holding, so there's nothing to compare yet." % html.escape(t["symbol"])
        paper = sells[-1]["spot"]
        diff = (val / paper - 1) * 100
        return "✍️ Your sell of $%s logged. Paper bot sold at %s, you at %s: you got %+.1f%% %s." % (
            html.escape(t["symbol"]), notify.price(paper), notify.price(val), diff, "more" if diff > 0 else "less")

    def fills_text(self):
        try:
            rows = self.con.execute("SELECT f.side, f.price, t.symbol, t.legs FROM real_fills f JOIN trades t ON t.id=f.trade_id").fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            return "No real fills logged yet. After a real trade, send /fill SYMBOL PRICE."
        buys, sells = [], []
        for r in rows:
            legs = json.loads(r["legs"] or "[]")
            if r["side"] == "buy" and legs:
                buys.append((r["price"] / legs[0]["spot"] - 1) * 100)
            elif r["side"] == "sell":
                s = [g for g in legs if g.get("side") == "sell"]
                if s:
                    sells.append((r["price"] / s[-1]["spot"] - 1) * 100)
        avg = lambda v: sum(v) / len(v) if v else 0  # noqa: E731
        return ("📏 <b>Real vs paper</b>\nBuys: %d, you paid %+.1f%% vs the bot on average (positive = worse)\n"
                "Sells: %d, you got %+.1f%% vs the bot on average (negative = worse)\n"
                "Rough real-money edge vs paper per round trip: %+.1f%%") % (
            len(buys), avg(buys), len(sells), avg(sells), avg(sells) - avg(buys))

    async def on_trader_sell(self, trader, addr, chain, usd, symbol):
        """A trader on your list sold. Exit paper trades that follow them (if set), and warn you if you
        were alerted on this coin or a paper trade holds it."""
        now = now_ms()
        held = []
        for name, st in self.strats.items():
            pos = st.positions.get(addr)
            if not pos:
                continue
            is_leader = trader.lower() in Strategy.leaders(pos)
            acts = st.leader_sold(addr, trader, now)
            for a in acts:
                await self.act(a, name)
            if is_leader or name in config.ALERT_PRESETS:
                held.append("%s%s" % (report.LABEL.get(name, name).split(" (")[0], " (sold with them)" if acts else ""))
        alerted = self.con.execute("SELECT kind FROM fomo_alerts WHERE (token_address=? OR (? AND lower(token_address)=?)) AND ts>? "
                                   "ORDER BY ts DESC LIMIT 1", (addr, addr.startswith("0x"), addr, now - 12 * 3600 * 1000)).fetchone()
        if not held and not alerted:
            return
        k = (trader.lower(), addr)
        if now - self.sell_warned.get(k, 0) < 6 * 3600 * 1000:
            return
        self.sell_warned[k] = now
        lines = ["🟠 <b>@%s is selling $%s</b> (%s)" % (html.escape(trader), html.escape(symbol or addr[:6]), notify.money(usd))]
        if alerted:
            lines.append("You got a %s alert on this coin in the last 12 hours." % ("cluster" if alerted["kind"] == "cluster" else "trending"))
        if held:
            lines.append("Paper trades in it: " + ", ".join(held))
        lines.append(notify.dex_link(addr, chain))
        lines.append("If you copied them in, this is your cue to check the chart.")
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
        db.kv_set(self.con, "busy_until", now_ms() + 3 * 3600 * 1000)
        await notify.send(self.session, "🧪 Testing %d Follow rules on the last 7 days of Fomo trades. Takes a minute or two." % len(followtest.VARIANTS))
        try:
            res = await asyncio.to_thread(followtest.run, 7)
            await notify.send(self.session, followtest.text(res))
        except Exception as e:  # noqa: BLE001
            log.exception("Follow test failed")
            await notify.send(self.session, "Follow test failed: %s" % html.escape(str(e)[:300]))
        finally:
            self.sweeping = False
            db.kv_set(self.con, "busy_until", 0)

    async def risk_backfill(self):
        """Once: rebuild rug-risk history from coins already recorded, so scores are learned from day one."""
        if db.kv_get(self.con, "rug_backfill_done"):
            return
        await asyncio.sleep(60)
        try:
            def work():
                c = db.connect()
                n, coins = risk.backfill(c)
                return n, coins, risk.build(c)
            n, coins, model = await asyncio.to_thread(work)
            self.rug_model = model
            for st in self.strats.values():
                st.risk_model = model
            db.kv_set(self.con, "rug_backfill_done", 1)
            await notify.send(self.session, "🧯 Rug-risk model trained on %s checkpoints from %s past coins (%s drained). Send /risk to see what it learned." % (
                format(n, ","), format(coins, ","), format(model.get("rugs", 0), ",")))
        except Exception:  # noqa: BLE001
            log.exception("Rug backfill failed")

    async def scorecard_loop(self):
        while True:
            try:
                if fomo.key():
                    await fomo.price_calls(self.session, self.con, dexscreener_batch, pair_to_snapshot)
            except Exception:  # noqa: BLE001
                log.exception("Scorecard pricing failed")
            await asyncio.sleep(60)

    async def run_sweep(self, days=7):
        if self.sweeping:
            await notify.send(self.session, "A rule test is already running. Results will show up here when it's done.")
            return
        self.sweeping = True
        db.kv_set(self.con, "busy_until", now_ms() + 3 * 3600 * 1000)
        await notify.send(self.session, "🧪 Testing %d rule variations on the last %s of recorded coins. Big tests take 30 to 60 minutes; I'll post progress. "
                          "Alerts keep working, but updating or restarting the bot cancels the test." % (
                              len(sweep.VARIANTS), "day" if days == 1 else "%g days" % days))
        try:
            # its own low-priority process: the live bot keeps its speed, and each progress step keeps updates away
            import sys
            proc = await asyncio.create_subprocess_exec(
                "nice", "-n", "10", sys.executable, "-m", "gatekeeper", "sweep-worker", "--days", str(days),
                stdout=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
            res, got = None, False
            async for line in proc.stdout:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if "p" in m:
                    db.kv_set(self.con, "busy_until", now_ms() + 2 * 3600 * 1000)
                    if m["p"] < 100:
                        await notify.send(self.session, "🧪 Rule test %d%% done..." % m["p"])
                elif "res" in m:
                    res, got = m["res"], True
            await proc.wait()
            if not got:
                raise RuntimeError("the test stopped early (exit code %s)" % proc.returncode)
            await notify.send(self.session, sweep.text(res))
        except Exception as e:  # noqa: BLE001
            log.exception("Sweep failed")
            await notify.send(self.session, "Rule test failed: %s" % html.escape(str(e)[:300]))
        finally:
            self.sweeping = False
            db.kv_set(self.con, "busy_until", 0)

    async def run_check(self, arg):
        try:
            await notify.send(self.session, await check.run(self, arg))
        except Exception as e:  # noqa: BLE001
            log.exception("/check failed")
            await notify.send(self.session, "🔎 Couldn't check that coin: %s" % html.escape(str(e)[:200]))

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
                    chat = str((msg.get("chat") or {}).get("id"))
                    owner = str((msg.get("from") or {}).get("id")) == str(config.TELEGRAM_CHAT_ID)
                    cmd = (msg.get("text") or "").strip().split()[0].lower().split("@")[0] if msg.get("text") else ""
                    if chat != str(config.TELEGRAM_CHAT_ID):
                        # a group: only you can command the bot there, and only /setup works before it's linked
                        if not owner or (chat != str(notify.ROUTES["chat"]) and cmd != "/setup"):
                            continue
                    notify.REPLY.set((chat, msg.get("message_thread_id") if msg.get("is_topic_message") else None))
                    text = (msg.get("text") or "").strip()
                    if cmd in ("/check", "check", "/c"):
                        asyncio.create_task(self.run_check(text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""))
                    elif not text.startswith("/") and cmd != "pick" and len(text.split()) <= 2 and check.find_address(text)[0]:
                        asyncio.create_task(self.run_check(text))      # a pasted contract address or coin link on its own
                    elif cmd == "/setup":
                        await self.setup_topics(msg)
                    elif cmd == "/unsetup":
                        db.kv_set(self.con, "tg_group", "")
                        notify.ROUTES.update(chat=None, threads={})
                        await notify.send(self.session, "Alerts are back in your private chat with the bot.", chat_id=config.TELEGRAM_CHAT_ID)
                    elif cmd in ("/status", "status"):
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
                    elif cmd in ("/trends", "trends", "/picks"):
                        st = self.trends.state
                        if not st.get("updated"):
                            await notify.send(self.session, "Trends are still loading. Try again in a few minutes.")
                        else:
                            L = ["📰 <b>Trends right now</b>", "Hot words in the news: " + ", ".join(st["words"][:12])]
                            if st["suggestions"]:
                                L.append("\n<b>Worth a look</b> (not buy signals)")
                                for c in st["suggestions"]:
                                    L.append("$%s: %s" % (html.escape(c["symbol"]), html.escape(" · ".join(c["reasons"]))))
                            if st["trump"]:
                                L.append("\n<b>Trump's latest</b>: " + html.escape((st["trump"][0]["title"] or st["trump"][0]["text"])[:200]))
                            L.append("\nFull page with pictures and articles: /site → Trends tab")
                            await notify.send(self.session, "\n".join(L))
                    elif cmd in ("/publish", "publish"):
                        try:
                            when = await stats.publish(self, full=True)
                            await notify.send(self.session, "📤 Stats published (%s)." % when)
                        except Exception as e:  # noqa: BLE001
                            await notify.send(self.session, "📤 Publish failed: %s" % html.escape(str(e)[:200]))
                    elif cmd in ("/bounce", "bounce"):
                        await notify.send(self.session, bounce.report(self.con))
                    elif cmd in ("/winscore", "winscore", "/win"):
                        await notify.send(self.session, winmodel.report(self.con))
                    elif cmd in ("/experiments", "experiments", "/x"):
                        await notify.send(self.session, report.experiments_text(self.con))
                    elif cmd in ("/ai", "ai", "/aitrader"):
                        await notify.send(self.session, self.analyst.text())
                    elif cmd in ("/pick", "pick"):
                        async def _pick(arg=text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""):
                            await notify.send(self.session, await self.analyst.pick(arg))
                        asyncio.create_task(_pick())
                    elif cmd in ("/hold", "hold", "/holdbot"):
                        await notify.send(self.session, self.hold.text())
                    elif cmd in ("/events", "events", "/speeches"):
                        await notify.send(self.session, self.events.text())
                    elif cmd in ("/trench", "trench", "/wallets"):
                        await notify.send(self.session, self.trench.text())
                    elif cmd in ("/moonshots", "moonshots", "/50x"):
                        await notify.send(self.session, await asyncio.to_thread(lambda: risk.moonshots_text(db.connect())))
                    elif cmd in ("/exits", "exits"):
                        await notify.send(self.session, report.exits_text(self.con))
                    elif cmd in ("/risk", "risk", "/rug"):
                        await notify.send(self.session, risk.report(self.con))
                    elif cmd in ("/research", "research"):
                        await notify.send(self.session, await asyncio.to_thread(self._research))
                    elif cmd in ("/fill", "fill"):
                        await notify.send(self.session, self.record_fill((msg.get("text") or "").split()[1:]))
                    elif cmd in ("/fills", "fills"):
                        await notify.send(self.session, self.fills_text())
                    elif cmd in ("/reset", "reset"):
                        parts = (msg.get("text") or "").split()
                        which = [w.lower() for w in parts[1:] if w.lower() in self.strats] or list(self.strats)
                        for w in which:
                            self.epochs[w] = now_ms()
                            db.kv_set(self.con, "rules_since_" + w, self.epochs[w])
                        await notify.send(self.session, "🔄 Fresh start for %s. Dashboard P/L now counts from this moment. All-time history is kept." % ", ".join(which))
                    elif cmd in ("/site", "site", "/dashboard", "/live"):
                        url = await web.public_url(self.session, self.con)
                        await notify.send(self.session, "📺 Live dashboard (keep this link private):\n%s\n\nIf it won't load, the server firewall may block port %d. Run in the console: ufw allow %d" % (html.escape(url), web.PORT, web.PORT))
                    elif cmd in ("/testfollow", "testfollow", "/followtest", "/test_follow", "/follow"):
                        asyncio.create_task(self.run_followtest())
                    elif cmd in ("/sweep", "sweep", "/test", "test"):
                        parts = (msg.get("text") or "").split()
                        try:
                            days = min(14.0, max(0.5, float(parts[1]))) if len(parts) > 1 else 7
                        except ValueError:
                            days = 7
                        asyncio.create_task(self.run_sweep(days))
                    elif cmd in ("/help", "/start", "help"):
                        await notify.send(self.session, "Commands:\n/status: feed health and open trades\n/today: last 24 hours\n/week: last 7 days\n/all: since the start\n/fomo: what Fomo traders bought in the last 24h (free)\n/scan: full trend scan of your Fomo traders (uses credits)\n/traders: scorecard of your Fomo traders' buys\n/test: replay recorded coins through every rule variation (30 to 60 min; /test 1 = last day only, much faster)\n/testfollow: test the Follow rules on your traders' buys (1 to 2 min)\n/site: link to the live dashboard\n/reset main: restart the website's since-last-reset P/L count for a strategy (history is kept; Telegram reports are not affected)\n/research: what the week's data says about themes, socials and safety\n/fill SYMBOL PRICE: log a real trade to compare with paper (/fills for the summary)\n/check COIN: the bot's verdict on any coin (paste its contract address, a Fomo or DexScreener link, or $TICKER)\n/risk: what the rug-risk model has learned\n/exits: which exit rules sell too early and which save us\n/trends: news, Trump's posts and coins riding them\n/events: upcoming speeches, summits and signings, and the coins that could move\n/experiments: each test copy of Main against Main over the same days\n/winscore: what the win-score model has learned (chance of +40% before -30%)\n/ai: the AI trader: judges coins like a person, with its reasons and results\n/pick COIN: have the AI trader judge one of your own ideas (it buys on paper if it agrees)\n/hold: the Hold bot: $300 paper bets on established coins, held for days\n/bounce: can the bot tell a shake-out from a real dump when the stop loss hits\n/moonshots: what coins that went 10x-50x looked like early\n/trench: on-chain wallets that keep catching moonshots early\n/publish: push a stats snapshot to GitHub now\n/setup: (send inside a Telegram group with Topics on) sort alerts into topics\n/unsetup: move alerts back to this private chat")
                    elif cmd.startswith("/"):
                        await notify.send(self.session, "I don't know %s. Send /help for the list. (If a new command doesn't work, run: gatekeeper update)" % html.escape(cmd[:40]))
                notify.REPLY.set(None)
            except Exception as e:  # noqa: BLE001
                notify.REPLY.set(None)
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
                        await notify.send(self.session, await asyncio.to_thread(self._research))
                        await self.run_followtest()
                        await self.run_scan()
            if now.hour == 4 and db.kv_get(self.con, "pruned") != key:
                keep = int(float(__import__("os").environ.get("GK_KEEP_DAYS", "14")))
                self.con.execute("DELETE FROM snapshots WHERE ts < ?", (now_ms() - keep * 86400000,))
                fomo.ensure_schema(self.con)
                self.con.execute("DELETE FROM fomo_events WHERE ts < ?", (now_ms() - 30 * 86400000,))
                self.con.execute("DELETE FROM trader_calls WHERE ts < ?", (now_ms() - 30 * 86400000,))
                self.con.execute("DELETE FROM coin_features WHERE ts < ?", (now_ms() - 45 * 86400000,))
                db.kv_set(self.con, "pruned", key)
            await asyncio.sleep(60)

    async def run(self):
        async with aiohttp.ClientSession(headers={"User-Agent": "gatekeeper-bot/1.0"}) as session:
            self.session = session
            db.kv_set(self.con, "busy_until", 0)
            ver = db.kv_get(self.con, "installed_version")
            try:
                import subprocess
                cur = subprocess.run(["git", "-C", str(__import__("pathlib").Path(__file__).resolve().parents[1]), "log", "-1", "--format=%h %s"],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:  # noqa: BLE001
                cur = ""
            if cur and cur != ver:
                db.kv_set(self.con, "installed_version", cur)
                if ver:
                    await notify.send(session, "⬆️ <b>Updated</b>: %s" % html.escape(cur[:180]))
            await notify.send(session, "🤖 Gatekeeper bot started. Strategies: %s. %d open paper trades. Send /help for commands."
                              % (", ".join(self.strats), sum(len(s.positions) for s in self.strats.values())))
            try:
                await web.serve(self)
            except Exception:  # noqa: BLE001
                log.exception("Dashboard failed to start")
            await asyncio.gather(
                fomo.Feed(self.con, self.on_fomo_alert, self.track_coin, self.on_trader_sell).run(),
                pumpportal_stream(self.on_event, config.PUMPPORTAL_API_KEY),
                self.poll_loop(), self.fast_loop(), self.safety_loop(), self.risk_backfill(), self.trends.loop(), self.events.loop(), self.hold.loop(), self.analyst.loop(), self.trench.loop(), stats.loop(self), self.telegram_loop(), self.daily_loop(), self.scorecard_loop())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Runner().run())
