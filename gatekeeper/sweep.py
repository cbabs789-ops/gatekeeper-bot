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
    ("Current Main rules", {}),
    ("Only coins 60+ min old", {"MIN_AGE_MIN": 60}),
    ("Only coins 2h+ old", {"MIN_AGE_MIN": 120, "MAX_AGE_MIN": 720}),
    ("Pool $30K+", {"MIN_LIQ_USD": 30000}),
    ("Pool $50K+", {"MIN_LIQ_USD": 50000}),
    ("Ran 2x+ first", {"MIN_RUNUP_X": 2.0}),
    ("Shallow dips only (10-25%)", {"PULLBACK_MIN_PCT": 10, "PULLBACK_MAX_PCT": 25}),
    ("Top 10 hold 20% or less", {"MAX_TOP10_PCT": 20}),
    ("Looser liquidity exit (35%)", {"LIQ_PULL_PCT": 35}),
    ("Tighter stop (-20%)", {"STOP_LOSS_PCT": 20}),
    ("Take half at 1.5x", {"TAKE_HALF_X": 1.5}),
    ("Combo: 60+ min, $30K+ pool, 35% liq exit", {"MIN_AGE_MIN": 60, "MIN_LIQ_USD": 30000, "LIQ_PULL_PCT": 35}),
    ("Combo + shallow dips", {"MIN_AGE_MIN": 60, "MIN_LIQ_USD": 30000, "LIQ_PULL_PCT": 35, "PULLBACK_MIN_PCT": 10, "PULLBACK_MAX_PCT": 25}),
    ("Combo + 2h old + top 10 at 20%", {"MIN_AGE_MIN": 120, "MAX_AGE_MIN": 720, "MIN_LIQ_USD": 30000, "LIQ_PULL_PCT": 35, "MAX_TOP10_PCT": 20}),
    ("Skip devs with 3+ earlier coins", {"MAX_DEV_PREV_COINS": 2}),
    ("Skip devs with any dead earlier coin", {"MAX_DEV_DEAD_COINS": 0}),
    ("Current rules with zero trading costs (reference only)", {"FEE_PCT": 0, "PENALTY_PCT": 0, "PANIC_PENALTY_PCT": 0}),
    # --- in-trade protection, tested on newer coins (the old Wide entries, the biggest sample) ---
    ("New coins, old exits", {}, "wide"),
    ("New coins + rug score", {}, "wide", True),
    ("New coins + lock gains (trail 20% once up 20%)", {"LOCK_START_PCT": 20, "LOCK_TRAIL_PCT": 20}, "wide"),
    ("New coins + lock gains (trail 15% once up 30%)", {"LOCK_START_PCT": 30, "LOCK_TRAIL_PCT": 15}, "wide"),
    ("New coins + never give back a 25% gain", {"BREAKEVEN_AT_PCT": 25}, "wide"),
    ("New coins + exit on heavy selling", {"SELL_PRESSURE_EXIT": 1}, "wide"),
    ("New coins + exit if pool drains 15%", {"LIQ_DRAIN_PCT": 15}, "wide"),
    ("New coins + all protections", PROTECT, "wide"),
    ("Current Main + all protections", PROTECT),
    # --- moonbag: keep a slice after taking profit to catch the rare huge runner ---
    ("New coins + 20% moonbag", {"MOONBAG_PCT": 20}, "wide"),
    ("New coins + all protections + 20% moonbag", dict(PROTECT, MOONBAG_PCT=20), "wide"),
    # --- survivors: coins 4h+ old that lived through the dangerous hours, climbing steadily ---
    ("Survivor (4h+ old, climbing, sell all at +30%)", {}, "survivor"),
    ("Survivor, sell all at +20%", {"TAKE_PROFIT_PCT": 20}, "survivor"),
    ("Survivor, pool $50K+", {"MIN_LIQ_USD": 50000}, "survivor"),
    ("Survivor, 2h+ old instead of 4h+", {"MIN_AGE_MIN": 120}, "survivor"),
    ("Survivor + rug score (skip 60%+, smaller bets on risky coins)", {"RISK_SKIP": 60}, "survivor", True),
    ("Survivor + rug score, skip 40%+", {"RISK_SKIP": 40}, "survivor", True),
    ("Survivor + rug score + stop waits 60s to confirm", {"STOP_CONFIRM_SEC": 60}, "survivor", True),
    ("Survivor + rug score + wider stop (-25%)", {"STOP_LOSS_PCT": 25}, "survivor", True),
    ("Survivor + rug score + no heavy-selling exit", {"SELL_PRESSURE_EXIT": 0}, "survivor", True),
    ("Current Main + rug score (skip 45%+, half at +40%)", {}, "main", True),
    ("Main + rug score, skip 60%+ (the old setting)", {"RISK_SKIP": 60}, "main", True),
    # --- the winner, loosened to find more trades (it only made 7 in 5 days) ---
    ("Main + rug score, 60+ min old", {"MIN_AGE_MIN": 60}, "main", True),
    ("Main + rug score, 90+ min old", {"MIN_AGE_MIN": 90}, "main", True),
    ("Main + rug score, up to 24h old", {"MAX_AGE_MIN": 1440}, "main", True),
    ("Main + rug score, pool $20K+", {"MIN_LIQ_USD": 20000}, "main", True),
    ("Main + rug score, top 10 at 25%", {"MAX_TOP10_PCT": 25}, "main", True),
    ("Main + rug score, skip 35%+", {"RISK_SKIP": 35}, "main", True),
    # --- winners give back too much: bank part of the spike instead of trailing it ---
    ("Main + rug score, sell half at 2x (the old setting)", {"TAKE_HALF_X": 2.0}, "main", True),
    ("Main + rug score, sell half at +60%", {"TAKE_HALF_X": 1.6}, "main", True),
    ("Main + rug score, sell all at +40%", {"TAKE_PROFIT_PCT": 40}, "main", True),
    ("Main + rug score, sell all at +60%", {"TAKE_PROFIT_PCT": 60}, "main", True),
    ("Main + rug score, tighter lock (trail 10% once up 20%)", {"LOCK_TRAIL_PCT": 10}, "main", True),
    ("Main + rug score, half at +40% then trail 10%", {"TAKE_HALF_X": 1.4, "LOCK_TRAIL_PCT": 10}, "main", True),
    # --- moonshot hunter: $20 bets on young coins with early 10x signs (dev's first coin, has X) ---
    ("Moonshot Hunter (20-60 min, dev's first coin, has X, half at 3x)", {}, "moonshot"),
    ("Moonshot, dev's first coin only (X not required)", {"MOON_NEED_X": 0}, "moonshot"),
    ("Moonshot, has X only (any dev)", {"MOON_NEED_FIRST_DEV": 0}, "moonshot"),
    ("Moonshot, 10-40 min old", {"MIN_AGE_MIN": 10, "MAX_AGE_MIN": 40}, "moonshot"),
    ("Moonshot, half at 5x", {"TAKE_HALF_X": 5.0}, "moonshot"),
    ("Moonshot, tighter stop (-30%)", {"STOP_LOSS_PCT": 30}, "moonshot"),
    ("Moonshot + rug score (skip 60%+)", {"RISK_SKIP": 60}, "moonshot", True),
    # --- momentum (day-trader style): buy breakouts, quick profits, tight stops ---
    ("Momentum (as set)", {}, "momentum"),
    ("Momentum, stronger breakouts only (+25% in 5 min)", {"MOM_MIN_PCT": 25}, "momentum"),
    ("Momentum, pool $25K+", {"MIN_LIQ_USD": 25000}, "momentum"),
    ("Momentum, take half at 1.25x", {"TAKE_HALF_X": 1.25}, "momentum"),
    ("Momentum, no moonbag", {"MOONBAG_PCT": 0}, "momentum"),
    ("Momentum, wider stop (-25%)", {"STOP_LOSS_PCT": 25}, "momentum"),
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
    next_mark = 25
    for r in con.execute(q, (since,)):
        s = dict(r)
        if progress and (s["ts"] - lo_hi["a"]) * 100 / span >= next_mark:
            progress(next_mark)
            next_mark += 25
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
         "A rule is only real if both halves are positive. Moonshot lines use $20 bets.\n"]
    ranked = sorted(res["results"], key=lambda r: (r[3].get("total_pnl", 0) if r[3].get("trades") else -1e9), reverse=True)
    for label, all_, a, b in ranked:
        if not all_.get("trades"):
            L.append("<b>%s</b>: no trades" % label)
            continue
        both = a.get("total_pnl", 0) > 0 and b.get("total_pnl", 0) > 0
        L.append("%s<b>%s</b>: %d · %.0f%% · $%s · $%s / $%s" % (
            "✅ " if both else "", label, all_["trades"], all_["win_rate"], format(int(all_["total_pnl"]), ","),
            format(int(a.get("total_pnl", 0)), ","), format(int(b.get("total_pnl", 0)), ",")))
    L.append("\nEach trade is $100 of fake money ($20 for Moonshot). Nothing changes until you pick a rule.")
    return "\n".join(L)
