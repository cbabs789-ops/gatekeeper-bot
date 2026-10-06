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
    ("Main + rug score, pool $30K+ (the old setting)", {"MIN_LIQ_USD": 30000}, "main", True),
    ("Main + rug score, pool $15K+", {"MIN_LIQ_USD": 15000}, "main", True),
    ("Main + rug score, top 10 at 25%", {"MAX_TOP10_PCT": 25}, "main", True),
    ("Main + rug score, skip 45%+ (Main until Oct 4)", {"RISK_SKIP": 45}, "main", True),
    ("Main + rug score, skip 25%+", {"RISK_SKIP": 25}, "main", True),
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
    # --- the stop loss: live stop-outs filled at -40% or worse on a -30% stop ---
    ("Main + rug score, stop loss at -20%", {"STOP_LOSS_PCT": 20}, "main", True),
    ("Main + rug score, stop loss at -40%", {"STOP_LOSS_PCT": 40}, "main", True),
    ("Main + rug score, stop waits 60 seconds before selling", {"STOP_CONFIRM_SEC": 60}, "main", True),
    ("Main + rug score, stop waits 3 minutes before selling", {"STOP_CONFIRM_SEC": 180}, "main", True),
    ("Best of: stop loss at -40% and pool $15K+", {"STOP_LOSS_PCT": 40, "MIN_LIQ_USD": 15000}, "main", True),
    # --- combos of Oct 1's winners ---
    ("Main + rug score, skip 35%+ and sell all at +40%", {"RISK_SKIP": 35, "TAKE_PROFIT_PCT": 40}, "main", True),
    ("Main + rug score, skip 35%+ and trail 10%", {"RISK_SKIP": 35, "LOCK_TRAIL_PCT": 10}, "main", True),
]


def safety_timeline(con):
    """Safety as it was known AT THE TIME, per coin: [(ts, check), ...] oldest first.
    The safety table only keeps each coin's latest check, which can include a danger flag or a holder jump that
    appeared after the bot would have bought. So this uses the check log (kept since Oct 6) and the vital signs
    recorded at 30, 60, 120 and 240 minutes old."""
    con.executescript(db.SAFETY_LOG)
    timeline = {}
    for r in con.execute("SELECT * FROM safety_log ORDER BY checked_at"):
        timeline.setdefault(r["mint"], []).append((r["checked_at"], dict(r)))
    for r in con.execute("SELECT mint, ts, top10, insiders, lp_ok, auth_ok, danger, dev_prev, dev_dead, socials FROM coin_features ORDER BY ts"):
        if r["top10"] is None and r["insiders"] is None and not r["auth_ok"]:
            continue                      # no safety check had come back yet at this checkpoint: unknown, not "failed"
        timeline.setdefault(r["mint"], []).append((r["ts"], {
            "_socials": r["socials"],
            "mint": r["mint"], "checked_at": r["ts"], "mint_revoked": r["auth_ok"], "freeze_revoked": r["auth_ok"], "lp_na": 0,
            "lp_locked": 100 if r["lp_ok"] else 0, "top10": r["top10"], "insiders": r["insiders"],
            "danger": "flagged" if r["danger"] else None, "creator_prev": r["dev_prev"], "creator_dead": r["dev_dead"]}))
    for m in timeline:
        timeline[m].sort(key=lambda x: x[0])
    return timeline


class Known:
    """What each coin's safety check said as of the replay's current moment."""

    def __init__(self, timeline):
        self.tl, self.now, self.upto = timeline, {}, {}

    def get(self, mint):
        return self.now.get(mint)

    def advance(self, mint, ts, coin=None):
        tl = self.tl.get(mint)
        if not tl:
            return
        i = self.upto.get(mint, 0)
        while i < len(tl) and tl[i][0] <= ts:
            self.now[mint] = tl[i][1]
            if coin is not None and tl[i][1].get("_socials") is not None:
                coin["socials"] = tl[i][1]["_socials"]      # socials as they were then, not as they are today
            i += 1
        self.upto[mint] = i


class TestStrategy(Strategy):
    """The live bot keeps recording any coin it holds. The recorded history does not: a coin stops being recorded
    when it dies, or at 12 hours old. So when a test trade's data runs out, say honestly what that means."""
    coin_status = None

    def on_tick(self, now):
        acts = []
        for mint, pos in list(self.positions.items()):
            cs = self.coins.get(mint)
            if not cs or now - cs.last_ts <= 5 * 60000:
                continue
            if ((self.coin_status or {}).get(mint) or {}).get("status") == "dead":
                acts += self._close(pos, cs, now, 0.0, 0.0, "Pool gone (counted as a total loss)")
            else:
                acts += self._close(pos, cs, now, cs.last_price, cs.last_liq, "Recording ended (result unknown, left out)")
        return acts


def run(days=7, progress=None):
    con = db.connect()
    coins = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM coins")}
    known = Known(safety_timeline(con))
    look = known.get
    strats = []
    from . import risk
    risky = []
    for v in VARIANTS:
        label, ov = v[0], v[1]
        preset = v[2] if len(v) > 2 else "main"
        use_risk = len(v) > 3 and v[3]
        p = config.strategy_params(ov, preset)
        st = TestStrategy(p, look)    # keeps the preset's own limit on open trades, the same as live
        st.coin_status = coins
        if use_risk:
            risky.append(st)
        strats.append((label, st))
    since = int(time.time() * 1000) - days * 86400000
    lo_hi = con.execute("SELECT MIN(ts) a, MAX(ts) b FROM snapshots WHERE ts>=?", (since,)).fetchone()
    if not lo_hi["a"]:
        return None
    mid = lo_hi["a"] + (lo_hi["b"] - lo_hi["a"]) // 2
    # The rug score must not know the future. Learn it only from coins that finished BEFORE the second half starts,
    # so the second half is a true test. (Until Oct 6 this used today's model, which had already seen every coin
    # in the test: that made every "rug score" line look better than it could be live.)
    model = risk.build(con, until=mid, save=False)
    honest = bool(model.get("counts"))
    if not honest:
        model = risk.load(con) or None
    for st in risky:
        st.risk_model = model
    # coins that can't pass basic safety never get traded by any variant, so skip them
    # (authorities are never handed back once revoked, so this filter can't hide a coin the bot could have bought;
    #  the old filter also dropped coins that were flagged dangerous LATER, which the live bot had no way to know)
    q = ("SELECT * FROM snapshots WHERE ts>=? AND mint IN (SELECT mint FROM safety WHERE mint_revoked=1 "
         "AND freeze_revoked=1) ORDER BY ts")
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
        known.advance(s["mint"], s["ts"], c)
        for _, st in strats:
            st.on_snapshot(s, c)
        n += 1
    out, unknown = [], 0
    for label, st in strats:
        for mint, pos in list(st.positions.items()):
            cs = st.coins[mint]
            st._close(pos, cs, cs.last_ts, cs.last_price, cs.last_liq, "Still open at the end")
        real = [c for c in st.closed if not (c.exit_reason or "").startswith("Recording ended")]
        unknown += len(st.closed) - len(real) if label.startswith("Current Main + rug") else 0
        first = [c for c in real if c.opened_at < mid]
        second = [c for c in real if c.opened_at >= mid]
        out.append((label, summarize(real), summarize(first), summarize(second)))
    return {"rows": n, "from": lo_hi["a"], "to": lo_hi["b"], "results": out, "unknown": unknown, "honest": honest, "model_n": (model or {}).get("n", 0)}


def text(res):
    if not res:
        return "Not enough recorded data to test yet."
    hours = (res["to"] - res["from"]) / 3600000
    L = ["🧪 <b>Rule test</b> on %.1f days of recorded coins (%s snapshots)" % (hours / 24, format(res["rows"], ",")),
         "Each line: trades · win rate · total profit · first half / second half",
         "A rule is only real if both halves are positive.",
         ("The second half is the honest one: the rug score was learned only from %s coins that finished before it began.\n" % format(res.get("model_n", 0), ","))
         if res.get("honest") else "Not enough older coins yet for an honest rug score, so these results flatter the rug-score lines.\n",
         "Safety checks are replayed as they were known at the time, not as they look today.",
         "Not in this test (live only): the insider-wallet skip and the safety re-check exit."
         + (" %d trades left out because recording stopped before they finished." % res["unknown"] if res.get("unknown") else "") + "\n"]
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
