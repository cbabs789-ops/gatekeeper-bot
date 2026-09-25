"""The bot's brain and its paper broker.

Pure logic, no network. The live runner and the backtester both feed it the
same snapshot rows, so a backtest result is what the live bot would have done.
"""
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


class Strategy:
    def __init__(self, params, safety_lookup):
        self.p = params
        self.broker = PaperBroker(params)
        self.safety_lookup = safety_lookup   # mint -> dict or None
        self.coins = {}
        self.positions = {}
        self.traded = set()
        self.closed = []                     # finished Position objects

    # ---- feed
    def on_snapshot(self, s, coin):
        """s: snapshot dict (mint, ts, price, liq, buys_m5, sells_m5, pc_h1...).
        coin: dict with symbol, graduated_at, grad_price."""
        if not s.get("price") or not s.get("liq"):
            return []
        cs = self.coins.get(s["mint"])
        if cs is None:
            cs = CoinState(s["mint"], coin.get("symbol") or s["mint"][:6], coin.get("graduated_at") or s["ts"],
                           coin.get("grad_price") or s["price"])
            self.coins[s["mint"]] = cs
        cs.hist.append((s["ts"], s["price"], s["liq"]))
        cs.last_ts, cs.last_price, cs.last_liq = s["ts"], s["price"], s["liq"]
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
            if (saf.get("lp_locked") or 0) < p["MIN_LP_LOCKED_PCT"]: fails.append("LP %.0f%% locked" % (saf.get("lp_locked") or 0))
            if saf.get("top10") is not None and saf["top10"] > p["MAX_TOP10_PCT"]: fails.append("top 10 hold %.0f%%" % saf["top10"])
            if saf.get("insiders") is not None and saf["insiders"] > p["MAX_INSIDERS"]: fails.append("%d insiders" % saf["insiders"])
            if saf.get("danger"): fails.append("RugCheck: " + saf["danger"])
        runup = cs.peak_price / cs.grad_price if cs.grad_price else 0
        if runup < p["MIN_RUNUP_X"]:
            fails.append("hasn't run (%.1fx)" % runup)
        pull = (1 - s["price"] / cs.peak_price) * 100 if cs.peak_price else 0
        if not (p["PULLBACK_MIN_PCT"] <= pull <= p["PULLBACK_MAX_PCT"]):
            fails.append("pullback %.0f%%" % pull)
        if (s["ts"] - cs.peak_ts) / MIN > p["PEAK_WITHIN_MIN"]:
            fails.append("peak is stale")
        b, se = s.get("buys_m5") or 0, s.get("sells_m5") or 0
        if b + se < p["MIN_M5_TXNS"]:
            fails.append("quiet (%d trades in 5m)" % (b + se))
        if b < se:
            fails.append("sellers winning (%d/%d)" % (b, se))
        ref = cs.at_or_after(s["ts"] - 10 * MIN)
        if ref and ref[2] > 0 and s["liq"] < ref[2] * p["LIQ_HOLD_PCT"] / 100:
            fails.append("liquidity falling")
        why = ["%.0f min since graduation" % age, "ran %.1fx, now %.0f%% off the peak" % (runup, pull),
               "%d buys vs %d sells in 5m" % (b, se), "liquidity $%s" % format(int(s["liq"]), ",")]
        if saf:
            why.append("top 10 hold %s%%, %s insiders, LP %s%% locked" % (
                "%.0f" % saf["top10"] if saf.get("top10") is not None else "?",
                saf.get("insiders") if saf.get("insiders") is not None else "?",
                "%.0f" % (saf.get("lp_locked") or 0)))
        return (not fails), why, fails

    def _maybe_enter(self, cs, s):
        if cs.mint in self.traded or len(self.positions) >= self.p["MAX_OPEN"]:
            return None
        ok, why, _ = self.entry_check(cs, s)
        if not ok:
            return None
        usd = self.p["POSITION_USD"]
        qty, fill = self.broker.buy(s["price"], s["liq"], usd)
        pos = Position(cs.mint, cs.symbol, s["ts"], fill, s["price"], usd, qty, qty, peak_after=s["price"], why="; ".join(why))
        pos.legs.append({"ts": s["ts"], "side": "buy", "spot": s["price"], "usd": usd})
        self.positions[cs.mint] = pos
        self.traded.add(cs.mint)
        return {"type": "buy", "pos": pos, "spot": s["price"], "liq": s["liq"]}

    # ---- exits
    def _manage(self, pos, cs, s):
        p, price, liq, ts = self.p, s["price"], s["liq"], s["ts"]
        pos.peak_after = max(pos.peak_after, price)
        ref = cs.at_or_after(ts - 5 * MIN)
        if ref and ref[0] < ts and ref[2] > 0 and liq < ref[2] * (1 - p["LIQ_PULL_PCT"] / 100):
            return self._close(pos, cs, ts, price, liq, "Liquidity pulled (-%.0f%% in 5 min)" % ((1 - liq / ref[2]) * 100), panic=True)
        if price <= pos.spot_at_entry * (1 - p["STOP_LOSS_PCT"] / 100):
            return self._close(pos, cs, ts, price, liq, "Stop loss")
        if (ts - pos.opened_at) / MIN >= p["MAX_HOLD_MIN"]:
            return self._close(pos, cs, ts, price, liq, "Time limit")
        acts = []
        if not pos.took_half and price >= pos.spot_at_entry * p["TAKE_HALF_X"]:
            q = pos.qty_open / 2
            got = self.broker.sell(price, liq, q)
            pos.qty_open -= q
            pos.proceeds += got
            pos.took_half = True
            pos.legs.append({"ts": ts, "side": "sell", "spot": price, "usd": got, "why": "Took half at %.1fx" % p["TAKE_HALF_X"]})
            acts.append({"type": "partial", "pos": pos, "spot": price, "usd": got})
        if pos.took_half:
            if price <= pos.peak_after * (1 - p["TRAIL_PCT"] / 100):
                acts += self._close(pos, cs, ts, price, liq, "Trailing stop")
            elif price <= pos.spot_at_entry:
                acts += self._close(pos, cs, ts, price, liq, "Back to entry after taking half")
        return acts

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
