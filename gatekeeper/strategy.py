"""The bot's brain and its paper broker.

Pure logic, no network. The live runner and the backtester both feed it the
same snapshot rows, so a backtest result is what the live bot would have done.
"""
import math
import re
from collections import deque
from dataclasses import dataclass, field

MIN = 60_000


# ---------------------------------------------------------------- broker
class PaperBroker:
    """Fills fake orders against a constant-product pool (how PumpSwap and
    Raydium pools price trades), plus fees and a penalty for slow fills."""

    def __init__(self, p):
        self.p = p

    def _cost(self, panic=False):
        c = (self.p["FEE_PCT"] + self.p["PENALTY_PCT"]) / 100
        if panic:
            c += self.p["PANIC_PENALTY_PCT"] / 100
        return c

    def buy(self, price, liq, usd):
        q = max(liq / 2, 1.0)                     # USD side of the pool
        fill = price * (1 + usd / q)              # price impact of our own buy
        fill *= 1 + self._cost()
        return usd / fill, fill                   # tokens, avg fill price

    def sell(self, price, liq, qty, panic=False):
        q = max(liq / 2, 1.0)
        v = qty * price                           # value at spot
        got = v * q / (q + v)                     # price impact of our own sell
        got *= 1 - self._cost(panic)
        return max(got, 0.0)


# ---------------------------------------------------------------- state
@dataclass
class CoinState:
    mint: str
    symbol: str
    graduated_at: int
    grad_price: float = 0.0
    hist: deque = field(default_factory=lambda: deque(maxlen=240))  # (ts, price, liq)
    peak_price: float = 0.0
    peak_ts: int = 0
    last_ts: int = 0
    last_price: float = 0.0
    last_liq: float = 0.0
    socials: str = ""
    last_snap: dict = None

    def at_or_after(self, ts):
        for t, pr, lq in self.hist:
            if t >= ts:
                return t, pr, lq
        return None


@dataclass
class Position:
    mint: str
    symbol: str
    opened_at: int
    entry_price: float           # average fill incl. costs
    spot_at_entry: float
    size_usd: float
    qty_total: float
    qty_open: float
    proceeds: float = 0.0
    took_half: bool = False
    peak_after: float = 0.0
    why: str = ""
    legs: list = field(default_factory=list)
    trade_id: int = None
    max_liq: float = 0.0
    moon: bool = False
    risk: float = None                 # rug-risk score at entry (0-100)
    below_since: int = 0               # when price first fell below the stop (for STOP_CONFIRM_SEC)
    target_x: float = None             # data-picked profit target for this trade (ADAPTIVE_TARGETS)


class Strategy:
    def __init__(self, params, safety_lookup):
        self.p = params
        self.broker = PaperBroker(params)
        self.safety_lookup = safety_lookup   # mint -> dict or None
        self.coins = {}
        self.positions = {}
        self.traded = set()
        self.closed = []                     # finished Position objects
        self.risk_model = None               # rug-risk model (set by the runner and the rule test)

    # ---- feed
    def on_snapshot(self, s, coin):
        """s: snapshot dict (mint, ts, price, liq, buys_m5, sells_m5, pc_h1...).
        coin: dict with symbol, graduated_at, grad_price."""
        if not s.get("price") or not s.get("liq"):
            return []
        cs = self.coins.get(s["mint"])
        if cs is not None and s["ts"] <= cs.last_ts:
            return []                        # older than what we already have (two price loops overlapping)
        if cs is None:
            cs = CoinState(s["mint"], coin.get("symbol") or s["mint"][:6], coin.get("graduated_at") or s["ts"],
                           coin.get("grad_price") or s["price"])
            self.coins[s["mint"]] = cs
        cs.hist.append((s["ts"], s["price"], s["liq"]))
        cs.last_ts, cs.last_price, cs.last_liq = s["ts"], s["price"], s["liq"]
        cs.last_snap = s
        if coin.get("socials") is not None:
            cs.socials = coin.get("socials") or ""
        if s["price"] > cs.peak_price:
            cs.peak_price, cs.peak_ts = s["price"], s["ts"]
        acts = []
        if s["mint"] in self.positions:
            acts += self._manage(self.positions[s["mint"]], cs, s)
        else:
            a = self._maybe_enter(cs, s)
            if a:
                acts.append(a)
        return acts

    def prune(self, now, idle_ms=60 * MIN):
        """Forget coins that stopped reporting (keeps memory small on long replays)."""
        for m in [m for m, cs in self.coins.items() if now - cs.last_ts > idle_ms and m not in self.positions]:
            del self.coins[m]

    def on_tick(self, now):
        """Called once per poll cycle: closes positions whose coin stopped reporting."""
        acts = []
        for mint, pos in list(self.positions.items()):
            cs = self.coins.get(mint)
            if cs and now - cs.last_ts > 5 * MIN:
                acts += self._close(pos, cs, now, cs.last_price, cs.last_liq, "No data for 5 min (pool likely gone)", panic=True)
        return acts

    # ---- entry
    def entry_check(self, cs, s):
        """Returns (ok, reasons list, fails list). Exposed so reports can explain near-misses."""
        p, fails, why = self.p, [], []
        age = (s["ts"] - cs.graduated_at) / MIN
        if age < p["MIN_AGE_MIN"]:
            fails.append("too early (%.0f min)" % age)
        if age > p["MAX_AGE_MIN"]:
            fails.append("too old")
        if s["liq"] < p["MIN_LIQ_USD"]:
            fails.append("liquidity $%.0f" % s["liq"])
        saf = self.safety_lookup(cs.mint)
        if not saf:
            fails.append("safety not checked")
        else:
            if not saf.get("mint_revoked"): fails.append("mint authority active")
            if not saf.get("freeze_revoked"): fails.append("freeze authority active")
            if not saf.get("lp_na") and (saf.get("lp_locked") or 0) < p["MIN_LP_LOCKED_PCT"]:
                fails.append("LP %.0f%% locked" % (saf.get("lp_locked") or 0))
            if saf.get("top10") is not None and saf["top10"] > p["MAX_TOP10_PCT"]: fails.append("top 10 hold %.0f%%" % saf["top10"])
            if saf.get("insiders") is not None and saf["insiders"] > p["MAX_INSIDERS"]: fails.append("%d insiders" % saf["insiders"])
            if saf.get("danger"): fails.append("RugCheck: " + saf["danger"])
            if saf.get("creator_prev") is not None and saf["creator_prev"] > p["MAX_DEV_PREV_COINS"]:
                fails.append("dev launched %d coins before" % saf["creator_prev"])
            if saf.get("creator_dead") is not None and saf["creator_dead"] > p["MAX_DEV_DEAD_COINS"]:
                fails.append("dev has %d dead coins" % saf["creator_dead"])
        runup = cs.peak_price / cs.grad_price if cs.grad_price else 0
        pull = (1 - s["price"] / cs.peak_price) * 100 if cs.peak_price else 0
        b, se = s.get("buys_m5") or 0, s.get("sells_m5") or 0
        mom = None
        if p.get("ENTRY_MODE") == "survivor":
            ref60 = cs.at_or_after(s["ts"] - 60 * MIN)
            if not ref60 or ref60[0] > s["ts"] - 50 * MIN or not ref60[1]:
                fails.append("not enough price history yet")
            else:
                mom = (s["price"] / ref60[1] - 1) * 100
                if not (p["SURV_MIN_PCT"] <= mom <= p["SURV_MAX_PCT"]):
                    fails.append("not a steady climb (%+.0f%% in 1h)" % mom)
                if ref60[2] and s["liq"] < ref60[2] * p["LIQ_HOLD_PCT"] / 100:
                    fails.append("pool shrinking over the hour")
            bh, sh = s.get("buys_h1") or 0, s.get("sells_h1") or 0
            if bh < sh:
                fails.append("sellers winning over the hour (%d/%d)" % (bh, sh))
            if b < se:
                fails.append("sellers winning (%d/%d)" % (b, se))
        elif p.get("ENTRY_MODE") == "momentum":
            ref5 = cs.at_or_after(s["ts"] - 5 * MIN)
            if not ref5 or ref5[0] > s["ts"] - 4 * MIN or not ref5[1]:
                fails.append("not enough price history yet")
            else:
                mom = (s["price"] / ref5[1] - 1) * 100
                if mom < p["MOM_MIN_PCT"]:
                    fails.append("no momentum (%+.0f%% in 5m)" % mom)
                elif mom > p["MOM_MAX_PCT"]:
                    fails.append("too vertical (%+.0f%% in 5m)" % mom)
            if pull > p["MOM_NEAR_HIGH_PCT"]:
                fails.append("%.0f%% off its high" % pull)
            if b < se * p["MOM_BUY_RATIO"]:
                fails.append("buyers not dominant (%d/%d)" % (b, se))
        else:
            if runup < p["MIN_RUNUP_X"]:
                fails.append("hasn't run (%.1fx)" % runup)
            if not (p["PULLBACK_MIN_PCT"] <= pull <= p["PULLBACK_MAX_PCT"]):
                fails.append("pullback %.0f%%" % pull)
            if (s["ts"] - cs.peak_ts) / MIN > p["PEAK_WITHIN_MIN"]:
                fails.append("peak is stale")
            if b < se:
                fails.append("sellers winning (%d/%d)" % (b, se))
        if b + se < p["MIN_M5_TXNS"]:
            fails.append("quiet (%d trades in 5m)" % (b + se))
        ref = cs.at_or_after(s["ts"] - 10 * MIN)
        if ref and ref[2] > 0 and s["liq"] < ref[2] * p["LIQ_HOLD_PCT"] / 100:
            fails.append("liquidity falling")
        if p.get("ENTRY_MODE") == "momentum" and mom is not None:
            move = "breaking out: %+.0f%% in 5 min, %.0f%% off its high" % (mom, pull)
        elif p.get("ENTRY_MODE") == "survivor" and mom is not None:
            move = "survived %.1fh, climbing steadily: %+.0f%% over the last hour" % (age / 60, mom)
        else:
            move = "ran %.1fx, now %.0f%% off the peak" % (runup, pull)
        why = ["%.0f min since graduation" % age, move,
               "%d buys vs %d sells in 5m" % (b, se), "liquidity $%s" % format(int(s["liq"]), ",")]
        if saf:
            why.append("top 10 hold %s%%, %s insiders, LP %s%% locked" % (
                "%.0f" % saf["top10"] if saf.get("top10") is not None else "?",
                saf.get("insiders") if saf.get("insiders") is not None else "?",
                "%.0f" % (saf.get("lp_locked") or 0)))
        return (not fails), why, fails

    def safety_fails(self, mint, liq):
        """Just the safety gates (used for signal entries such as Fomo clusters)."""
        p, fails = self.p, []
        saf = self.safety_lookup(mint)
        if not saf:
            return ["safety not checked"]
        if not saf.get("mint_revoked"): fails.append("mint authority active")
        if not saf.get("freeze_revoked"): fails.append("freeze or blacklist active")
        if not saf.get("lp_na") and (saf.get("lp_locked") or 0) < p["MIN_LP_LOCKED_PCT"]:
            fails.append("LP %.0f%% locked" % (saf.get("lp_locked") or 0))
        if saf.get("top10") is not None and saf["top10"] > p["MAX_TOP10_PCT"]: fails.append("top 10 hold %.0f%%" % saf["top10"])
        if saf.get("insiders") is not None and saf["insiders"] > p["MAX_INSIDERS"]: fails.append("%d insiders" % saf["insiders"])
        if saf.get("danger"): fails.append(saf["danger"])
        if saf.get("creator_prev") is not None and saf["creator_prev"] > p["MAX_DEV_PREV_COINS"]:
            fails.append("dev launched %d coins before" % saf["creator_prev"])
        if saf.get("creator_dead") is not None and saf["creator_dead"] > p["MAX_DEV_DEAD_COINS"]:
            fails.append("dev has %d dead coins" % saf["creator_dead"])
        if liq < p["MIN_LIQ_USD"]: fails.append("pool $%.0f" % liq)
        return fails

    def signal_enter(self, mint, why):
        """Buy on an outside signal (a Fomo cluster) if the coin passes safety."""
        cs = self.coins.get(mint)
        if not cs or mint in self.traded or mint in self.positions or len(self.positions) >= self.p["MAX_OPEN"]:
            return None
        if self.safety_fails(mint, cs.last_liq):
            return None
        rk, usd = self.risk_and_size(cs)
        if not usd:
            return None
        qty, fill = self.broker.buy(cs.last_price, cs.last_liq, usd)
        pos = Position(mint, cs.symbol, cs.last_ts, fill, cs.last_price, usd, qty, qty, peak_after=cs.last_price, why=why)
        pos.risk = rk
        pos.target_x = self._target(rk)
        pos.legs.append({"ts": cs.last_ts, "side": "buy", "spot": cs.last_price, "usd": usd})
        self.positions[mint] = pos
        self.traded.add(mint)
        return {"type": "buy", "pos": pos, "spot": cs.last_price, "liq": cs.last_liq}

    def safety_exit(self, mint, reason, ts):
        """Live re-check found new red flags while holding."""
        pos, cs = self.positions.get(mint), self.coins.get(mint)
        if not pos or not cs or not self.p.get("RECHECK_EXIT"):
            return []
        return self._close(pos, cs, max(ts, cs.last_ts), cs.last_price, cs.last_liq, "Safety changed: " + reason, panic=True)

    def risk_and_size(self, cs):
        """(rug-risk score or None, dollars to put in, or 0 to skip)."""
        usd = self.p["POSITION_USD"]
        if not self.risk_model or not cs.last_snap:
            return None, usd
        from . import risk
        age = (cs.last_ts - cs.graduated_at) / MIN
        r = risk.score(self.risk_model, risk.live_row(cs.last_snap, self.safety_lookup(cs.mint), cs.socials, age))[0]
        if r >= self.p.get("RISK_SKIP", 100):
            return r, 0
        if self.p.get("RISK_SIZING"):
            usd *= 1.0 if r < 20 else 0.75 if r < 35 else 0.5 if r < 50 else 0.3
        return r, round(usd, 2)

    def _target(self, rk):
        if rk is None or not self.p.get("ADAPTIVE_TARGETS") or not self.risk_model:
            return None
        from . import risk
        return risk.target_for(self.risk_model, rk)

    @staticmethod
    def leaders(pos):
        """Traders named in a position's reason, e.g. 'Fomo cluster: @a, @b' -> {'a', 'b'}."""
        return {h.lower() for h in re.findall(r"@([A-Za-z0-9_.-]+)", pos.why or "")}

    def leader_sold(self, mint, trader, ts):
        """A trader who got us into this coin sold. Exits only if SELL_WITH_TRADERS is on."""
        pos = self.positions.get(mint)
        cs = self.coins.get(mint)
        if not pos or not cs or not self.p.get("SELL_WITH_TRADERS") or trader.lower() not in self.leaders(pos):
            return []
        return self._close(pos, cs, max(ts, cs.last_ts), cs.last_price, cs.last_liq, "Trader sold (@%s)" % trader)

    def _maybe_enter(self, cs, s):
        if self.p.get("ENTRY_MODE") == "signal":
            return None
        if cs.mint in self.traded or len(self.positions) >= self.p["MAX_OPEN"]:
            return None
        ok, why, _ = self.entry_check(cs, s)
        if not ok:
            return None
        rk, usd = self.risk_and_size(cs)
        if not usd:
            self.traded.add(cs.mint)          # too risky: skip this coin for good
            return None
        qty, fill = self.broker.buy(s["price"], s["liq"], usd)
        pos = Position(cs.mint, cs.symbol, s["ts"], fill, s["price"], usd, qty, qty, peak_after=s["price"], why="; ".join(why))
        pos.risk = rk
        pos.target_x = self._target(rk)
        pos.legs.append({"ts": s["ts"], "side": "buy", "spot": s["price"], "usd": usd})
        self.positions[cs.mint] = pos
        self.traded.add(cs.mint)
        return {"type": "buy", "pos": pos, "spot": s["price"], "liq": s["liq"]}

    # ---- exits
    def _manage(self, pos, cs, s):
        p, price, liq, ts = self.p, s["price"], s["liq"], s["ts"]
        pos.peak_after = max(pos.peak_after, price)
        # A pool's dollar value falls on its own when the price falls (about with the square root of price).
        # Only count it as liquidity being pulled when it fell MORE than the price move explains.
        ref = cs.at_or_after(ts - 5 * MIN)
        if ref and ref[0] < ts and ref[2] > 0 and ref[1] > 0 and price > 0:
            depth_drop = 1 - (liq / math.sqrt(price)) / (ref[2] / math.sqrt(ref[1]))
            if depth_drop > p["LIQ_PULL_PCT"] / 100:
                return self._close(pos, cs, ts, price, liq, "Liquidity pulled (-%.0f%% of pool depth in 5 min)" % (depth_drop * 100), panic=True)
        gain = (price / pos.spot_at_entry - 1) * 100 if pos.spot_at_entry else 0
        if not pos.moon and pos.target_x and price >= pos.spot_at_entry * pos.target_x:
            return self._core_exit(pos, cs, ts, price, liq, "Hit data target %gx (rug risk %.0f%%)" % (pos.target_x, pos.risk or 0))
        if not pos.moon and not pos.target_x and p.get("RUG_TP_PCT") and pos.risk is not None and pos.risk >= p["RUG_RISK_MIN"] and gain >= p["RUG_TP_PCT"]:
            return self._close(pos, cs, ts, price, liq, "Quick profit: rug risk %.0f%%, took +%.0f%% and left" % (pos.risk, gain))
        if not pos.moon and p.get("TAKE_PROFIT_PCT") and gain >= p["TAKE_PROFIT_PCT"]:
            return self._close(pos, cs, ts, price, liq, "Took profit (+%.0f%%)" % gain)
        if pos.moon:
            if price <= pos.peak_after * (1 - p["MOON_TRAIL_PCT"] / 100):
                return self._close(pos, cs, ts, price, liq, "Moonbag trailing stop (peak was %.1fx)" % (pos.peak_after / pos.spot_at_entry))
            if (ts - pos.opened_at) / MIN >= p["MOON_MAX_HOURS"] * 60:
                return self._close(pos, cs, ts, price, liq, "Moonbag time limit (%.1fx)" % (price / pos.spot_at_entry))
            return []
        if price <= pos.spot_at_entry * (1 - p["STOP_LOSS_PCT"] / 100):
            wait = p.get("STOP_CONFIRM_SEC", 0) * 1000
            if not pos.below_since:
                pos.below_since = ts
            # a brief wick below the stop gets a moment to recover; a real breakdown (or -50%) sells right away
            if ts - pos.below_since >= wait or price <= pos.spot_at_entry * 0.5:
                return self._close(pos, cs, ts, price, liq, "Stop loss")
        else:
            pos.below_since = 0
        if (ts - pos.opened_at) / MIN >= p["MAX_HOLD_MIN"]:
            return self._core_exit(pos, cs, ts, price, liq, "Time limit")
        # --- in-trade protection ---
        entry = pos.spot_at_entry
        best = pos.peak_after / entry - 1 if entry else 0
        depth = liq / math.sqrt(price) if price > 0 else 0
        pos.max_liq = max(pos.max_liq, depth)       # deepest the pool has been since entry (price-adjusted)
        if p.get("LIQ_DRAIN_PCT") and pos.max_liq and depth < pos.max_liq * (1 - p["LIQ_DRAIN_PCT"] / 100):
            return self._close(pos, cs, ts, price, liq, "Pool draining (-%.0f%% of its depth)" % ((1 - depth / pos.max_liq) * 100), panic=True)
        if (p.get("SELL_PRESSURE_EXIT") and (s.get("sells_m5") or 0) >= 15 and (s.get("sells_m5") or 0) >= 2 * (s.get("buys_m5") or 0)
                and price < pos.peak_after * 0.9):
            return self._close(pos, cs, ts, price, liq, "Heavy selling (%s sells vs %s buys in 5 min)" % (s.get("sells_m5"), s.get("buys_m5")))
        if p.get("BREAKEVEN_AT_PCT") and best * 100 >= p["BREAKEVEN_AT_PCT"]:
            floor = entry * (1 + 2 * (p["FEE_PCT"] + p["PENALTY_PCT"]) / 100)
            if price <= floor:
                return self._core_exit(pos, cs, ts, price, liq, "Protected gain (was up %.0f%%)" % (best * 100))
        if p.get("LOCK_START_PCT") and best * 100 >= p["LOCK_START_PCT"] and price <= pos.peak_after * (1 - p["LOCK_TRAIL_PCT"] / 100):
            return self._core_exit(pos, cs, ts, price, liq, "Locked profit (was up %.0f%%)" % (best * 100))
        acts = []
        if not pos.took_half and price >= pos.spot_at_entry * p["TAKE_HALF_X"]:
            q = pos.qty_open / 2
            got = self.broker.sell(price, liq, q)
            pos.qty_open -= q
            pos.proceeds += got
            pos.took_half = True
            pos.legs.append({"ts": ts, "side": "sell", "spot": price, "usd": got, "qty": q, "why": "Took half at %.1fx" % p["TAKE_HALF_X"]})
            acts.append({"type": "partial", "pos": pos, "spot": price, "usd": got, "why": "Took half at %.1fx" % p["TAKE_HALF_X"]})
        if pos.took_half:
            if price <= pos.peak_after * (1 - p["TRAIL_PCT"] / 100):
                acts += self._core_exit(pos, cs, ts, price, liq, "Trailing stop")
            elif price <= pos.spot_at_entry:
                acts += self._close(pos, cs, ts, price, liq, "Back to entry after taking half")
        return acts

    def _core_exit(self, pos, cs, ts, price, liq, reason):
        """Sell the position, but keep a moonbag if that's on and the trade is already in profit."""
        keep = pos.qty_total * self.p.get("MOONBAG_PCT", 0) / 100
        if not keep or pos.moon or not pos.took_half or price <= pos.spot_at_entry or keep >= pos.qty_open:
            return self._close(pos, cs, ts, price, liq, reason)
        q = pos.qty_open - keep
        got = self.broker.sell(price, liq, q)
        pos.qty_open = keep
        pos.proceeds += got
        pos.moon = True
        why = "%s · kept a %.0f%% moonbag" % (reason, self.p["MOONBAG_PCT"])
        pos.legs.append({"ts": ts, "side": "sell", "spot": price, "usd": got, "qty": q, "why": why})
        return [{"type": "partial", "pos": pos, "spot": price, "usd": got, "why": why}]

    def _close(self, pos, cs, ts, price, liq, reason, panic=False):
        got = self.broker.sell(price, liq, pos.qty_open, panic=panic) if pos.qty_open > 0 else 0.0
        pos.proceeds += got
        pos.qty_open = 0
        pos.legs.append({"ts": ts, "side": "sell", "spot": price, "usd": got, "why": reason})
        pos.closed_at = ts
        pos.exit_reason = reason
        pos.pnl_usd = pos.proceeds - pos.size_usd
        pos.pnl_pct = pos.pnl_usd / pos.size_usd * 100
        self.positions.pop(pos.mint, None)
        self.closed.append(pos)
        return [{"type": "close", "pos": pos, "spot": price, "reason": reason}]


# ---------------------------------------------------------------- stats
def summarize(closed):
    n = len(closed)
    if not n:
        return {"trades": 0}
    pnl = [c.pnl_usd for c in closed]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    eq, peak, dd, streak, worst_streak = 0.0, 0.0, 0.0, 0, 0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if x <= 0 else 0
        worst_streak = max(worst_streak, streak)
    by = {}
    for c in closed:
        r = c.exit_reason.split(" (")[0]
        b = by.setdefault(r, [0, 0.0])
        b[0] += 1
        b[1] += c.pnl_usd
    return {
        "trades": n, "wins": len(wins), "win_rate": len(wins) / n * 100,
        "total_pnl": sum(pnl), "avg_pnl": sum(pnl) / n,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "best": max(pnl), "worst": min(pnl),
        "max_drawdown": dd, "worst_losing_streak": worst_streak,
        "invested": sum(c.size_usd for c in closed),
        "by_exit": by,
    }
