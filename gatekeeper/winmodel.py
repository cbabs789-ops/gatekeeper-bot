"""Win score: the chance a coin makes the trade Main is trying to make.

The rug model answers "will this coin drain?". This one answers "will it reach +40% before it falls 30%?",
learned from every coin the bot has recorded:

  1. Label: for each recorded checkpoint (a coin's vital signs at 2h and 4h old), replay its prices for the
     next 12 hours. Reached +40% first = win. Fell 30% first, or did neither = not a win.
  2. Learn: count how often coins with each vital sign won, and combine the counts (naive Bayes, the same
     method as the rug score).
  3. Check honestly: train on the older 60% of coins, then score the newer 40% it has never seen. The model is
     only "trusted" if its top-scoring coins there won clearly more often than average. Until then it is shown
     but never used to filter trades.
"""
import json
import math
import time

from . import db, risk

WIN_X, LOSE_X = 1.40, 0.70
HORIZON_MS = 12 * 3600 * 1000
AGES = (120, 240)                 # the checkpoints that match when Main buys
MIN_LIQ = 15000
MIN_ROWS, MIN_WINS = 300, 30
EXTRA = {
    "buy5": ((0.45, 0.55, 0.65), ("sellers winning (5m)", "even buying (5m)", "buyers ahead (5m)", "buyers dominant (5m)")),
    "mc": ((50000, 150000, 500000), ("market cap under $50K", "market cap $50-150K", "market cap $150-500K", "market cap over $500K")),
    "txn5": ((10, 30, 80), ("under 10 trades in 5m", "10-30 trades in 5m", "30-80 trades in 5m", "80+ trades in 5m")),
}


def ensure(con):
    risk.ensure(con)
    try:
        con.execute("ALTER TABLE coin_features ADD COLUMN win INTEGER")
    except Exception:  # noqa: BLE001
        pass  # already there


def _b(name, v):
    edges, labels = EXTRA[name]
    if v is None:
        return name + " unknown"
    for i, e in enumerate(edges):
        if v <= e:
            return labels[i]
    return labels[len(edges)]


def features(row):
    f = risk.features(row)
    f.pop("age", None)        # trained only on 2h and 4h coins: a younger coin's age must not count as a signal
    b, s = row.get("buys5") or 0, row.get("sells5") or 0
    f["buy5"] = _b("buy5", b / (b + s) if b + s else None)
    f["mc"] = _b("mc", row.get("fdv"))
    f["txn5"] = _b("txn5", b + s)
    return f


def label(con, max_mints=100000):
    """Fill in win / not-a-win for finished checkpoints by replaying the recorded prices. Returns rows labeled.
    Reads first, then writes in short bursts, so the live bot's own writes are never held up."""
    ensure(con)
    now = int(time.time() * 1000)
    todo = {}
    for r in con.execute("SELECT mint, t_min, ts, price FROM coin_features WHERE win IS NULL AND ts<? AND price>0 AND liq>=? "
                         "AND t_min IN (%s)" % ",".join(str(a) for a in AGES), (now - HORIZON_MS, MIN_LIQ)):
        todo.setdefault(r["mint"], []).append((r["t_min"], r["ts"], r["price"]))
    out = []
    for i, (mint, rows) in enumerate(todo.items()):
        if i >= max_mints:
            break
        t0, t1 = min(r[1] for r in rows), max(r[1] for r in rows) + HORIZON_MS
        snaps = con.execute("SELECT ts, price FROM snapshots WHERE mint=? AND ts>? AND ts<=? ORDER BY ts", (mint, t0, t1)).fetchall()
        for t_min, ts, p0 in rows:
            win = -1                                      # -1 = no price history left to judge it
            for s in snaps:
                if s["ts"] <= ts or s["ts"] > ts + HORIZON_MS or not s["price"]:
                    continue
                if win == -1:
                    win = 0
                if s["price"] >= p0 * WIN_X:
                    win = 1
                    break
                if s["price"] <= p0 * LOSE_X:
                    win = 0
                    break
            out.append((win, mint, t_min))
    for i in range(0, len(out), 200):
        con.executemany("UPDATE coin_features SET win=? WHERE mint=? AND t_min=?", out[i:i + 200])
        time.sleep(0.02)
    return len(out)


def _fit(rows):
    wins = sum(1 for r in rows if r["win"] == 1)
    m = {"n": len(rows), "wins": wins, "prior": (wins + 1) / (len(rows) + 2), "counts": {}}
    for r in rows:
        y = "w" if r["win"] == 1 else "l"
        for k, v in features(r).items():
            c = m["counts"].setdefault(k, {}).setdefault(v, {"w": 0, "l": 0})
            c[y] += 1
    return m


def score(model, row):
    """Chance (0-100) that a coin looking like `row` reaches +40% before -30%. None if the model isn't built."""
    counts = (model or {}).get("counts") or {}
    if not counts:
        return None
    n_w, n_l = model["wins"], model["n"] - model["wins"]
    logit = math.log(model["prior"] / (1 - model["prior"]))
    for k, v in features(row).items():
        if v not in counts.get(k, {}):
            continue                                   # a reading the model never saw says nothing either way
        c = counts[k][v]
        vals = max(2, len(counts.get(k, {})))
        logit += math.log(((c["w"] + 1) / (n_w + vals)) / ((c["l"] + 1) / (n_l + vals)))
    return round(100 / (1 + math.exp(-max(-30, min(30, logit)))))


def build(con, days=30):
    """Label what's finished, check the model on coins it hasn't seen, then train on everything."""
    label(con)
    now = int(time.time() * 1000)
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM coin_features WHERE win IN (0,1) AND ts>? AND t_min IN (%s) AND liq>=? AND auth_ok=1 ORDER BY ts"
        % ",".join(str(a) for a in AGES), (now - days * 86400000, MIN_LIQ))]
    wins = sum(1 for r in rows if r["win"] == 1)
    model = {"built": now, "n": len(rows), "wins": wins, "counts": {}, "trusted": False, "check": None, "cut": None}
    if len(rows) >= MIN_ROWS and wins >= MIN_WINS:
        k = int(len(rows) * 0.6)
        old, new = rows[:k], rows[k:]
        m0 = _fit(old)
        scored = sorted(((score(m0, r), r["win"]) for r in new), key=lambda x: -x[0])
        base = sum(w for _, w in scored) / len(scored)
        top = scored[:max(1, len(scored) * 2 // 5)]            # the top 40% by score
        rest = scored[len(top):]
        top_rate = sum(w for _, w in top) / len(top)
        rest_rate = sum(w for _, w in rest) / len(rest) if rest else 0
        model["check"] = {"n": len(scored), "base": round(base * 100, 1), "top": round(top_rate * 100, 1),
                          "rest": round(rest_rate * 100, 1), "top_n": len(top)}
        # trusted = on unseen coins, its top picks won at least a quarter more often than average, with a real sample
        model["trusted"] = bool(len(top) >= 60 and base > 0 and top_rate >= base * 1.25 and top_rate > rest_rate)
        full = _fit(rows)
        model.update(counts=full["counts"], prior=full["prior"])
        all_scores = sorted(score(model, r) for r in rows)
        model["cut"] = all_scores[len(all_scores) * 3 // 5]     # top 40% of scores clear this line
    db.kv_set(con, "win_model", json.dumps(model))
    return model


def load(con):
    try:
        return json.loads(db.kv_get(con, "win_model") or "{}")
    except ValueError:
        return {}


def report(con):
    m = load(con)
    if not m:
        return "🎯 Win score: not built yet. It builds itself within 30 minutes of the bot starting."
    L = ["🎯 <b>Win score</b> (0-100): how much a coin looks like past coins that reached +40% before falling 30%",
         "Learned from %s coins (2h and 4h old, real pool) · %s of them won (%.0f%%)" % (
             format(m.get("n", 0), ","), format(m.get("wins", 0), ","), (m["wins"] / m["n"] * 100) if m.get("n") else 0)]
    if not m.get("counts"):
        L.append("Still collecting: needs %d finished coins and %d winners before it can learn." % (MIN_ROWS, MIN_WINS))
        return "\n".join(L)
    c = m.get("check") or {}
    L.append("\n<b>Honest check</b> (trained on older coins, tested on %s newer ones it never saw)" % format(c.get("n", 0), ","))
    L.append("Its top 40%% picks won %.0f%% of the time · the rest won %.0f%% · average %.0f%%" % (c.get("top", 0), c.get("rest", 0), c.get("base", 0)))
    L.append("Verdict: <b>%s</b>" % ("trusted. The win-score experiment bot only buys coins scoring %d or higher (its top 40%%)." % m["cut"] if m.get("trusted")
                                     else "not trusted yet. Its picks didn't clearly beat average on unseen coins, so it isn't filtering any trades."))
    base = m["wins"] / m["n"] * 100
    good, bad = [], []
    for k, vals in m["counts"].items():
        if k == "age":
            continue
        for v, cnt in vals.items():
            tot = cnt["w"] + cnt["l"]
            if tot >= 40 and tot < m["n"] * 0.95:
                rate = cnt["w"] / tot * 100
                (good if rate >= base * 1.25 else bad if rate <= base * 0.75 else []).append((rate, v, tot))
    if good:
        L.append("\n<b>Signs of a winner</b> (win rate when present · average is %.0f%%)" % base)
        for rate, v, tot in sorted(good, reverse=True)[:6]:
            L.append("%s: %.0f%% (%s coins)" % (v[0].upper() + v[1:], rate, format(tot, ",")))
    if bad:
        L.append("\n<b>Signs of a dud</b>")
        for rate, v, tot in sorted(bad)[:6]:
            L.append("%s: %.0f%% (%s coins)" % (v[0].upper() + v[1:], rate, format(tot, ",")))
    L.append("\nThe score ranks coins; it isn't an exact probability. Retrained every 30 minutes. A good model shifts the odds; it can't see the future.")
    return "\n".join(L)
