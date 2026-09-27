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
]


def run(days=7, progress=None):
    con = db.connect()
    safety = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM safety")}
    coins = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM coins")}
    look = lambda m: safety.get(m)  # noqa: E731
    strats = []
    for v in VARIANTS:
        label, ov, preset = v if len(v) == 3 else (v[0], v[1], "main")
        p = config.strategy_params(ov, preset)
        p["MAX_OPEN"] = 1000          # don't let open slots decide which trades a variant takes
        strats.append((label, Strategy(p, look)))
    since = int(time.time() * 1000) - days * 86400000
    lo_hi = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM snapshots WHERE ts>=?", (since,)).fetchone()
    if not lo_hi["a"]:
        return None
    mid = lo_hi["a"] + (lo_hi["b"] - lo_hi["a"]) // 2
    # coins that can't pass basic safety never get traded by any variant, so skip them
    q = ("SELECT * FROM snapshots WHERE ts>=? AND mint IN (SELECT mint FROM safety WHERE mint_revoked=1 "
         "AND freeze_revoked=1 AND danger IS NULL) ORDER BY ts")
    last, n, ticks = None, 0, 0
    for r in con.execute(q, (since,)):
        s = dict(r)
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
        if progress and n % 500000 == 0:
            progress(n)
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
