"""Test many rule variations against recorded history in one pass.

Every variation sees the same coins in the same order. Results are split by
when each trade opened: the first half is where you'd tune, the second half
is the honest test. A variation only counts if it holds up in both halves.
"""
import time

from . import config, db
from .strategy import Strategy, summarize

PROTECT = {"LOCK_START_PCT": 20, "LOCK_TRAIL_PCT": 20, "BREAKEVEN_AT_PCT": 25, "SELL_PRESSURE_EXIT": 1, "LIQ_DRAIN_PCT": 15}

VARIANTS = [
    ("Current Main rules without the rug score (reference)", {}),
    ("Current Main + rug score (as set now)", {}, "main", True),
    ("Main + rug score, skip 60%+ (the old setting)", {"RISK_SKIP": 60}, "main", True),
    # --- the winner, loosened to find more trades (it only made 7 in 5 days) ---
    ("Main + rug score, 60+ min old", {"MIN_AGE_MIN": 60}, "main", True),
    ("Main + rug score, 90+ min old", {"MIN_AGE_MIN": 90}, "main", True),
    ("Main + rug score, up to 24h old", {"MAX_AGE_MIN": 1440}, "main", True),
    ("Main + rug score, pool $30K+ (the old setting)", {"MIN_LIQ_USD": 30000}, "main", True),
    ("Main + rug score, pool $15K+", {"MIN_LIQ_USD": 15000}, "main", True),
    ("Main + rug score, top 10 at 25%", {"MAX_TOP10_PCT": 25}, "main", True),
    ("Main + rug score, skip 35%+", {"RISK_SKIP": 35}, "main", True),
    # --- winners give back too much: bank part of the spike instead of trailing it ---
    ("Main + rug score, sell half at +40% (Main until Oct 2)", {"TAKE_PROFIT_PCT": 0}, "main", True),
    ("Main + rug score, sell half at 2x (the old setting)", {"TAKE_HALF_X": 2.0, "TAKE_PROFIT_PCT": 0}, "main", True),
    ("Main + rug score, sell half at +60%", {"TAKE_HALF_X": 1.6, "TAKE_PROFIT_PCT": 0}, "main", True),
    ("Main + rug score, quick scalp: sell all at +15%", {"TAKE_PROFIT_PCT": 15}, "main", True),
    ("Main + rug score, quick scalp: sell all at +20%", {"TAKE_PROFIT_PCT": 20}, "main", True),
    ("Main + rug score, sell all at +40%", {"TAKE_PROFIT_PCT": 40}, "main", True),
    ("Main + rug score, sell all at +60%", {"TAKE_PROFIT_PCT": 60}, "main", True),
    ("Main + rug score, sell all at +30% with a tighter lock (trail 10%)", {"LOCK_TRAIL_PCT": 10}, "main", True),
    ("Main + rug score, half at +40% then trail 10%", {"TAKE_HALF_X": 1.4, "LOCK_TRAIL_PCT": 10, "TAKE_PROFIT_PCT": 0}, "main", True),
    ("Smart aggressive (up to 24h old, sell all +30%, 10% trail, buys winners again)",
     {"MAX_AGE_MIN": 1440, "TAKE_PROFIT_PCT": 30, "LOCK_TRAIL_PCT": 10, "MAX_HOLD_MIN": 240, "REENTER_MIN": 20, "REENTER_MAX": 2}, "main", True),
    ("Smart aggressive without buying again", {"MAX_AGE_MIN": 1440, "TAKE_PROFIT_PCT": 30, "LOCK_TRAIL_PCT": 10, "MAX_HOLD_MIN": 240}, "main", True),
    # --- the stop loss: live stop-outs filled at -40% or worse on a -30% stop ---
    ("Main + rug score, stop loss at -20%", {"STOP_LOSS_PCT": 20}, "main", True),
    ("Main + rug score, stop loss at -40%", {"STOP_LOSS_PCT": 40}, "main", True),
    ("Main + rug score, stop waits 60 seconds before selling", {"STOP_CONFIRM_SEC": 60}, "main", True),
    ("Main + rug score, stop waits 3 minutes before selling", {"STOP_CONFIRM_SEC": 180}, "main", True),
    # --- combos of Oct 1's winners ---
    ("Main + rug score, skip 35%+ and sell all at +40%", {"RISK_SKIP": 35, "TAKE_PROFIT_PCT": 40}, "main", True),
    ("Main + rug score, skip 35%+ and trail 10%", {"RISK_SKIP": 35, "LOCK_TRAIL_PCT": 10}, "main", True),
]


def run(days=7, progress=None):
    con = db.connect()
    safety = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM safety")}
    coins = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM coins")}
    look = lambda m: safety.get(m)  # noqa: E731
    strats = []
    from . import risk
    model = risk.load(con) or None
    for v in VARIANTS:
        label, ov = v[0], v[1]
        preset = v[2] if len(v) > 2 else "main"
        use_risk = len(v) > 3 and v[3]
        p = config.strategy_params(ov, preset)
        p["MAX_OPEN"] = 1000          # don't let open slots decide which trades a variant takes
        st = Strategy(p, look)
        if use_risk and model and model.get("counts"):
            st.risk_model = model
        elif use_risk:
            label += " (no rug model yet)"
        strats.append((label, st))
    since = int(time.time() * 1000) - days * 86400000
    lo_hi = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM snapshots WHERE ts>=?", (since,)).fetchone()
    if not lo_hi["a"]:
        return None
    mid = lo_hi["a"] + (lo_hi["b"] - lo_hi["a"]) // 2
    # coins that can't pass basic safety never get traded by any variant, so skip them
    q = ("SELECT * FROM snapshots WHERE ts>=? AND mint IN (SELECT mint FROM safety WHERE mint_revoked=1 "
         "AND freeze_revoked=1 AND danger IS NULL) ORDER BY ts")
    last, n, ticks = None, 0, 0
    span = max(1, lo_hi["b"] - lo_hi["a"])
    next_mark = 10
    for r in con.execute(q, (since,)):
        s = dict(r)
        if progress and (s["ts"] - lo_hi["a"]) * 100 / span >= next_mark:
            progress(next_mark)
            next_mark += 10
        if last is not None and s["ts"] != last:
            ticks += 1
            for _, st in strats:
                st.on_tick(last)
                if ticks % 20 == 0:
                    st.prune(last)
        last = s["ts"]
        c = coins.get(s["mint"])
        if not c:
            continue
        for _, st in strats:
            st.on_snapshot(s, c)
        n += 1
    out = []
    for label, st in strats:
        for mint, pos in list(st.positions.items()):
            cs = st.coins[mint]
            st._close(pos, cs, cs.last_ts, cs.last_price, cs.last_liq, "Still open at the end")
        first = [c for c in st.closed if c.opened_at < mid]
        second = [c for c in st.closed if c.opened_at >= mid]
        out.append((label, summarize(st.closed), summarize(first), summarize(second)))
    return {"rows": n, "from": lo_hi["a"], "to": lo_hi["b"], "results": out}


def text(res):
    if not res:
        return "Not enough recorded data to test yet."
    hours = (res["to"] - res["from"]) / 3600000
    L = ["🧪 <b>Rule test</b> on %.1f days of recorded coins (%s snapshots)" % (hours / 24, format(res["rows"], ",")),
         "Each line: trades · win rate · total profit · first half / second half",
         "A rule is only real if both halves are positive.\n"]
    ranked = sorted(res["results"], key=lambda r: (r[3].get("total_pnl", 0) if r[3].get("trades") else -1e9), reverse=True)
    for label, all_, a, b in ranked:
        if not all_.get("trades"):
            L.append("<b>%s</b>: no trades" % label)
            continue
        both = a.get("total_pnl", 0) > 0 and b.get("total_pnl", 0) > 0
        L.append("%s<b>%s</b>: %d · %.0f%% · $%s · $%s / $%s" % (
            "✅ " if both else "", label, all_["trades"], all_["win_rate"], format(int(all_["total_pnl"]), ","),
            format(int(a.get("total_pnl", 0)), ","), format(int(b.get("total_pnl", 0)), ",")))
    L.append("\nEach trade is $100 of fake money. Nothing changes until you pick a rule.")
    return "\n".join(L)
