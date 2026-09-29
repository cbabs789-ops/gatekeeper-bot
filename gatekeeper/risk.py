"""Rug-risk score, learned from the bot's own history.

Every coin the recorder watches gets a snapshot of its vital signs at 30, 60,
120 and 240 minutes old (pool, holders, insiders, LP lock, socials, dev history,
buying pressure, momentum). Over the next 12 hours the bot tracks what happened
next: the best price it reached and whether the pool drained.

Once enough coins have finished, the bot counts how often coins with each
vital sign drained, and combines those counts (naive Bayes) into one number:
"coins that looked like this drained X% of the time". Until there's enough
history it falls back to a rough rule-based estimate and says so.
"""
import json
import math
import time

from . import db

CHECKPOINTS = (30, 60, 120, 240)
LABEL_AFTER_MS = 12 * 3600 * 1000       # a row gets its outcome 12h after its checkpoint
DRAIN_FRAC = 0.2                        # pool fell below 20% of its size at the checkpoint = rugged
MIN_ROWS, MIN_RUGS = 300, 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS coin_features (
  mint TEXT, t_min INTEGER, ts INTEGER, price REAL, liq REAL, fdv REAL,
  buys5 INTEGER, sells5 INTEGER, buys1h INTEGER, sells1h INTEGER, vol1h REAL, pc1h REAL,
  top10 REAL, insiders INTEGER, lp_ok INTEGER, auth_ok INTEGER, danger INTEGER,
  socials TEXT, dev_prev INTEGER, dev_dead INTEGER,
  peak_x REAL DEFAULT 1, min_liq_frac REAL DEFAULT 1, dead INTEGER DEFAULT 0, min_x REAL DEFAULT 1,
  PRIMARY KEY (mint, t_min)
);
CREATE INDEX IF NOT EXISTS feat_ts ON coin_features(ts);
"""


def ensure(con):
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE coin_features ADD COLUMN min_x REAL DEFAULT 1")
    except Exception:  # noqa: BLE001
        pass  # already there


# ------------------------------------------------------------------ features
BUCKETS = {
    "liq": ((15000, 40000, 100000), ("pool under $15K", "pool $15-40K", "pool $40-100K", "pool over $100K")),
    "top10": ((15, 25, 35), ("top 10 under 15%", "top 10 15-25%", "top 10 25-35%", "top 10 over 35%")),
    "insiders": ((2, 5, 10), ("0-2 insiders", "3-5 insiders", "6-10 insiders", "over 10 insiders")),
    "buy_share": ((0.45, 0.55, 0.65), ("sellers winning (1h)", "even buying (1h)", "buyers ahead (1h)", "buyers dominant (1h)")),
    "pc1h": ((-20, 0, 50, 200), ("down 20%+ in 1h", "down 0-20% in 1h", "up 0-50% in 1h", "up 50-200% in 1h", "up 200%+ in 1h")),
    "dev_prev": ((0, 2), ("dev's first coin", "dev made 1-2 before", "dev made 3+ before")),
    "vol_to_liq": ((0.5, 2, 5), ("low volume vs pool", "normal volume", "high volume", "extreme volume vs pool")),
}


def _bucket(name, v):
    edges, labels = BUCKETS[name]
    if v is None:
        return name + " unknown"
    for i, e in enumerate(edges):
        if v <= e:
            return labels[i]
    return labels[len(edges)]


def features(row):
    """row: a coin_features dict (or the same fields from a live snapshot + safety check)."""
    b, s = row.get("buys1h") or 0, row.get("sells1h") or 0
    soc = (row.get("socials") or "").split(",")
    return {
        "age": "%s min old" % row.get("t_min"),
        "liq": _bucket("liq", row.get("liq")),
        "top10": _bucket("top10", row.get("top10")),
        "insiders": _bucket("insiders", row.get("insiders")),
        "lp": "LP locked" if row.get("lp_ok") else "LP not locked",
        "auth": "mint/freeze off" if row.get("auth_ok") else "mint or freeze still on",
        "danger": "RugCheck danger flag" if row.get("danger") else "no danger flag",
        "x": "has X account" if "x" in soc else "no X account",
        "web": "has website" if "web" in soc else "no website",
        "boost": "paid boost" if "boost" in soc else "no boost",
        "dev_prev": _bucket("dev_prev", row.get("dev_prev")),
        "buy_share": _bucket("buy_share", b / (b + s) if b + s else None),
        "pc1h": _bucket("pc1h", row.get("pc1h")),
        "vol_to_liq": _bucket("vol_to_liq", (row.get("vol1h") or 0) / row["liq"] if row.get("liq") else None),
    }


def live_row(snap, saf, socials, age_min):
    saf = saf or {}
    return {
        "t_min": min(CHECKPOINTS, key=lambda c: abs(c - age_min)), "price": snap.get("price"), "liq": snap.get("liq"),
        "fdv": snap.get("fdv"), "buys5": snap.get("buys_m5"), "sells5": snap.get("sells_m5"),
        "buys1h": snap.get("buys_h1"), "sells1h": snap.get("sells_h1"), "vol1h": snap.get("vol_h1"), "pc1h": snap.get("pc_h1"),
        "top10": saf.get("top10"), "insiders": saf.get("insiders"),
        "lp_ok": 1 if (saf.get("lp_na") or (saf.get("lp_locked") or 0) >= 90) else 0,
        "auth_ok": 1 if (saf.get("mint_revoked") and saf.get("freeze_revoked")) else 0,
        "danger": 1 if saf.get("danger") else 0, "socials": socials or "",
        "dev_prev": saf.get("creator_prev"), "dev_dead": saf.get("creator_dead"),
    }


def log(con, mint, snap, saf, socials, age_min, now):
    """Record vital signs the first time a coin passes each checkpoint age."""
    for c in CHECKPOINTS:
        if c <= age_min < c + 20:
            r = live_row(snap, saf, socials, c)
            r.update(mint=mint, ts=now, t_min=c)
            con.execute("INSERT OR IGNORE INTO coin_features(mint, t_min, ts, price, liq, fdv, buys5, sells5, buys1h, sells1h, "
                        "vol1h, pc1h, top10, insiders, lp_ok, auth_ok, danger, socials, dev_prev, dev_dead) VALUES "
                        "(:mint,:t_min,:ts,:price,:liq,:fdv,:buys5,:sells5,:buys1h,:sells1h,:vol1h,:pc1h,:top10,:insiders,"
                        ":lp_ok,:auth_ok,:danger,:socials,:dev_prev,:dev_dead)", r)


def update_outcomes(con, snaps, now):
    """snaps: {mint: (price, liq)} from this price check. Tracks best price and smallest pool after each checkpoint."""
    rows = [(p, l, m, now - LABEL_AFTER_MS) for m, (p, l) in snaps.items() if p and l is not None]
    rows = [(p, p, l, m, t) for p, l, m, t in rows]
    con.executemany("UPDATE coin_features SET peak_x=MAX(peak_x, ?/price), min_x=MIN(COALESCE(min_x,1), ?/price), "
                    "min_liq_frac=MIN(min_liq_frac, ?/liq) WHERE mint=? AND ts>? AND price>0 AND liq>0", rows)


def mark_dead(con, mint, now):
    con.execute("UPDATE coin_features SET dead=1, min_liq_frac=0 WHERE mint=? AND ts>?", (mint, now - LABEL_AFTER_MS))


def rugged(row):
    return bool(row["dead"]) or (row["min_liq_frac"] or 1) < DRAIN_FRAC


# ------------------------------------------------------------------ backfill
def backfill(con, days=14, progress=None):
    """Rebuild vital signs and outcomes for coins already recorded, so the model can learn right away.
    Safety fields use the stored check (usually taken early in the coin's life)."""
    ensure(con)
    now = int(time.time() * 1000)
    since = now - days * 86400000
    coins = con.execute("SELECT c.mint, c.graduated_at, c.status, c.socials FROM coins c "
                        "WHERE c.graduated_at>=? AND c.source IS NULL AND NOT EXISTS "
                        "(SELECT 1 FROM coin_features f WHERE f.mint=c.mint)", (since,)).fetchall()
    safety = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM safety")}
    done = 0
    for i, c in enumerate(coins):
        g = c["graduated_at"]
        rows = con.execute("SELECT ts, price, liq, fdv, buys_m5, sells_m5, buys_h1, sells_h1, vol_h1, pc_h1 FROM snapshots "
                           "WHERE mint=? AND ts>=? AND ts<=? ORDER BY ts", (c["mint"], g, g + (240 + 720) * 60000)).fetchall()
        if not rows:
            continue
        for cp in CHECKPOINTS:
            t = g + cp * 60000
            at = next((r for r in rows if r["ts"] >= t), None)
            if not at or at["ts"] > t + 20 * 60000 or not at["price"] or (at["liq"] or 0) < 1000:
                continue
            after = [r for r in rows if at["ts"] <= r["ts"] <= at["ts"] + LABEL_AFTER_MS]
            if after[-1]["ts"] < at["ts"] + LABEL_AFTER_MS - 60 * 60000 and c["status"] != "dead":
                continue                                    # not enough follow-up to know how it ended
            peak = max(r["price"] or 0 for r in after) / at["price"]
            minliq = min((r["liq"] or 0) for r in after) / at["liq"]
            snap = {"price": at["price"], "liq": at["liq"], "fdv": at["fdv"], "buys_m5": at["buys_m5"], "sells_m5": at["sells_m5"],
                    "buys_h1": at["buys_h1"], "sells_h1": at["sells_h1"], "vol_h1": at["vol_h1"], "pc_h1": at["pc_h1"]}
            r = live_row(snap, safety.get(c["mint"]), c["socials"], cp)
            r.update(mint=c["mint"], ts=at["ts"], t_min=cp, peak_x=peak, min_liq_frac=minliq,
                     dead=1 if c["status"] == "dead" and after[-1]["ts"] < at["ts"] + LABEL_AFTER_MS else 0)
            con.execute("INSERT OR IGNORE INTO coin_features(mint, t_min, ts, price, liq, fdv, buys5, sells5, buys1h, sells1h, "
                        "vol1h, pc1h, top10, insiders, lp_ok, auth_ok, danger, socials, dev_prev, dev_dead, peak_x, min_liq_frac, dead) "
                        "VALUES (:mint,:t_min,:ts,:price,:liq,:fdv,:buys5,:sells5,:buys1h,:sells1h,:vol1h,:pc1h,:top10,:insiders,"
                        ":lp_ok,:auth_ok,:danger,:socials,:dev_prev,:dev_dead,:peak_x,:min_liq_frac,:dead)", r)
            done += 1
        if progress and i and i % 1000 == 0:
            progress(i, len(coins))
    return done, len(coins)


# ------------------------------------------------------------------ model
def build(con, days=21):
    """Count drain rates per vital sign from finished rows. Returns the model dict (also saved in kv)."""
    ensure(con)
    now = int(time.time() * 1000)
    rows = [dict(r) for r in con.execute("SELECT * FROM coin_features WHERE ts<? AND ts>?",
                                         (now - LABEL_AFTER_MS, now - days * 86400000))]
    rugs = [r for r in rows if rugged(r)]
    model = {"n": len(rows), "rugs": len(rugs), "built": now, "prior": (len(rugs) + 1) / (len(rows) + 2), "counts": {}}
    if len(rows) >= MIN_ROWS and len(rugs) >= MIN_RUGS:
        counts = {}
        for r in rows:
            y = "r" if rugged(r) else "s"
            for k, v in features(r).items():
                c = counts.setdefault(k, {}).setdefault(v, {"r": 0, "s": 0})
                c[y] += 1
        model["counts"] = counts
        # the profit target that paid best for each rug-risk band (needs 100+ coins in the band)
        model["targets"] = {}
        for label, n, hit, drained, best in targets(con, model):
            # only use a data target when it actually made money; a "least bad" losing target is worse than trailing
            if best and n >= 100 and best[1] > 0:
                model["targets"][label] = best[0]
    db.kv_set(con, "rug_model", json.dumps(model))
    return model


def target_for(model, r):
    """Data-picked profit target (as a multiple, e.g. 1.4) for a rug-risk score, or None."""
    t = (model or {}).get("targets") or {}
    for lo, hi, label in RISK_BANDS:
        if lo <= r < hi:
            return t.get(label)
    return None


def load(con):
    try:
        return json.loads(db.kv_get(con, "rug_model") or "{}")
    except ValueError:
        return {}


def score(model, row):
    """Estimated chance (0-100) that a coin looking like `row` drains within 12h, and a label saying how it was made."""
    counts = (model or {}).get("counts") or {}
    if counts:
        n_r, n_s = model["rugs"], model["n"] - model["rugs"]
        logit = math.log(model["prior"] / (1 - model["prior"]))
        for k, v in features(row).items():
            c = counts.get(k, {}).get(v, {"r": 0, "s": 0})
            vals = max(2, len(counts.get(k, {})))
            p_r = (c["r"] + 1) / (n_r + vals)          # Laplace smoothing
            p_s = (c["s"] + 1) / (n_s + vals)
            logit += math.log(p_r / p_s)
        p = 1 / (1 + math.exp(-max(-30, min(30, logit))))
        return round(p * 100), "learned from %s coins" % format(model["n"], ",")
    # rough rules until there's history: each red flag adds risk
    p = 25
    if not row.get("lp_ok"): p += 20
    if not row.get("auth_ok"): p += 25
    if row.get("danger"): p += 15
    if (row.get("insiders") or 0) > 5: p += 10
    if (row.get("top10") or 0) > 25: p += 10
    b, s = row.get("buys1h") or 0, row.get("sells1h") or 0
    if b + s and b / (b + s) < 0.45: p += 10
    if "x" not in (row.get("socials") or ""): p += 5
    if (row.get("dev_prev") or 0) >= 3: p += 10
    return min(95, p), "early estimate (not enough history yet)"


TARGETS = (1.2, 1.4, 2, 3, 5, 8)
RISK_BANDS = ((0, 20, "under 20%"), (20, 35, "20-35%"), (35, 50, "35-50%"), (50, 60, "50-60%"), (60, 101, "60%+"))


def targets(con, model, stop=0.15, cost=0.06, days=21):
    """For each risk band: how often coins reached each profit target within 12h, how often they drained,
    and which target would have made the most (rough: assumes the peak came before any drain)."""
    now = int(time.time() * 1000)
    rows = [dict(r) for r in con.execute("SELECT * FROM coin_features WHERE ts<? AND ts>?",
                                         (now - LABEL_AFTER_MS, now - days * 86400000))]
    out = []
    for lo, hi, label in RISK_BANDS:
        band = [r for r in rows if lo <= score(model, r)[0] < hi]
        if len(band) < 30:
            out.append((label, len(band), None, None, None))
            continue
        hit = {t: sum(1 for r in band if (r["peak_x"] or 1) >= t) / len(band) for t in TARGETS}
        drained = sum(1 for r in band if rugged(r)) / len(band)
        best = None
        for t in TARGETS:
            # win: sell at the target. miss: rugged coins lose everything, the rest hit the stop
            miss = 1 - hit[t]
            ev = hit[t] * (t - 1) - max(0.0, miss - drained) * stop - min(drained, miss) * 1.0 - cost
            if best is None or ev > best[1]:
                best = (t, ev)
        out.append((label, len(band), hit, drained, best))
    return out


def targets_text(con, model):
    rows = targets(con, model)
    L = ["\n<b>How high coins went within 12h, by rug risk</b> (share that reached each target · drained)"]
    for label, n, hit, drained, best in rows:
        if hit is None:
            L.append("Risk %s: only %d coins, not enough yet" % (label, n))
            continue
        L.append("Risk %s (%s coins): %s · drained %.0f%% → best target by the data: <b>%gx</b> (%+.0f%% per $1 bet)" % (
            label, format(n, ","), " · ".join("%gx %.0f%%" % (t, hit[t] * 100) for t in TARGETS), drained * 100, best[0], best[1] * 100))
    L.append("Rough math: sells at the target if reached, otherwise a drained coin loses it all and the rest hit the stop; 6% trading costs included.")
    return "\n".join(L)


def report(con):
    m = load(con)
    if not m:
        return "Rug-risk model not built yet. It builds itself hourly once coins have 12 hours of history."
    L = ["🧯 <b>Rug-risk model</b>", "Coins studied: %s · drained within 12h: %s (%.0f%%)" % (
        format(m.get("n", 0), ","), format(m.get("rugs", 0), ","), (m.get("rugs", 0) / m["n"] * 100) if m.get("n") else 0)]
    if not m.get("counts"):
        L.append("Still collecting: needs %d finished coins and %d rugs before it learns. Scores are rough estimates until then." % (MIN_ROWS, MIN_RUGS))
        return "\n".join(L)
    base = m["rugs"] / m["n"] * 100
    L.append("\n<b>Biggest warning signs</b> (drain rate when present vs overall)")
    items = []
    for k, vals in m["counts"].items():
        if k == "age":
            continue
        for v, c in vals.items():
            tot = c["r"] + c["s"]
            if tot >= 30 and tot < m["n"] * 0.95 and c["r"] / tot * 100 >= base + 5:
                items.append((c["r"] / tot * 100, k, v, tot))
    for rate, k, v, tot in sorted(items, reverse=True)[:8]:
        L.append("%s: %.0f%% drained (%s coins)" % (v[0].upper() + v[1:], rate, format(tot, ",")))
    L.append("\nOverall: %.0f%%. Scores above 50%% mean the bot takes a quick profit instead of holding." % base)
    try:
        L.append(targets_text(con, m))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(L)
