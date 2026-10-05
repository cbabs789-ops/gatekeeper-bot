"""Bounce score: when a coin hits the stop loss, is it a shake-out or a real dump?

The stop loss sells the moment a coin is 30% under the entry price. Many of those coins bounce right back; many
keep falling. This learns to tell them apart from every recorded coin, not only the ones the bot traded:

  1. Find stop moments: take each coin at the ages Main buys (2h and 4h old, real pool) as a pretend entry, and
     replay its prices. The first time it is 30% under that entry is a stop moment.
  2. Label it by what happened in the next hour: rose 20% first = bounce, fell another 20% first = dump,
     neither = flat.
  3. Learn which chart readings at the stop moment went with bounces: how fast it fell, whether buyers were
     coming back, whether the pool held, how far past the stop it gapped.
  4. Check honestly: train on the older 60%, score the newer 40% it has never seen. It is only "trusted" if its
     top picks there bounced clearly more often than average.

A report only. It changes no trades until it is trusted and a rule built on it passes /test.
"""
import json
import math
import time

from . import db

STOP_X = 0.70                      # the stop: 30% under the entry
UP_X, DOWN_X = 1.20, 0.80          # bounce = +20% from the stop price first; dump = another -20% first
HORIZON_MS = 12 * 3600 * 1000      # how long after the entry a stop moment can happen
AFTER_MS = 60 * 60 * 1000          # the hour after the stop that decides bounce or dump
AGES = (120, 240)
MIN_LIQ = 15000
MIN_ROWS, MIN_POS = 150, 30
MIN = 60000

SCHEMA = """
CREATE TABLE IF NOT EXISTS bounce_events (
  mint TEXT, t_min INTEGER, ts INTEGER, feat TEXT, bounce INTEGER,
  PRIMARY KEY (mint, t_min)
);
"""
# bounce: 1 = bounced, 0 = dumped, 2 = flat, -1 = never hit the stop (or no prices left to judge)


def ensure(con):
    con.executescript(SCHEMA)


def _bucket(v, edges, labels):
    if v is None:
        return labels[0].split(":")[0] + ": unknown"
    for i, e in enumerate(edges):
        if v <= e:
            return labels[i]
    return labels[len(edges)]


def features(entry_ts, p0, entry_liq, snaps, i):
    """Chart readings at the stop moment. snaps[i] is the first snapshot at or under the stop."""
    s = snaps[i]
    f = {}
    # how fast it fell: time since it was last within 15% of the entry
    t_ok = entry_ts
    for j in range(i - 1, -1, -1):
        if snaps[j]["price"] and snaps[j]["price"] >= p0 * 0.85:
            t_ok = snaps[j]["ts"]
            break
    f["speed"] = _bucket((s["ts"] - t_ok) / MIN, (2, 10, 30),
                         ("fell in under 2 min", "fell over 2-10 min", "fell over 10-30 min", "slow bleed (30+ min)"))
    f["gap"] = _bucket(-(s["price"] / p0 - 1) * 100, (35, 45),
                       ("stopped near -30%", "gapped to -35/-45%", "gapped past -45%"))
    b, se = s["buys_m5"] or 0, s["sells_m5"] or 0
    f["buy5"] = _bucket(b / (b + se) if b + se else None, (0.35, 0.5, 0.6),
                        ("buyers (5m): sellers swamping", "buyers (5m): sellers ahead", "buyers (5m): about even", "buyers (5m): buyers back in front"))
    f["txn5"] = _bucket(b + se, (10, 30, 80),
                        ("under 10 trades in 5m", "10-30 trades in 5m", "30-80 trades in 5m", "80+ trades in 5m"))
    bh, sh = s["buys_h1"] or 0, s["sells_h1"] or 0
    f["buy1h"] = _bucket(bh / (bh + sh) if bh + sh else None, (0.45, 0.55),
                         ("buyers (1h): sellers ahead", "buyers (1h): about even", "buyers (1h): buyers ahead"))
    # a pool's dollar value falls with price on its own; depth (liq / sqrt(price)) shows liquidity really leaving
    depth = None
    if entry_liq and p0 > 0 and s["price"] and s["liq"]:
        depth = (s["liq"] / math.sqrt(s["price"])) / (entry_liq / math.sqrt(p0))
    f["pool"] = _bucket(depth, (0.8, 0.95), ("pool: being pulled", "pool: down a little", "pool: held"))
    f["liq"] = _bucket(s["liq"], (15000, 30000), ("pool under $15K", "pool $15-30K", "pool over $30K"))
    f["held"] = _bucket((s["ts"] - entry_ts) / MIN, (15, 60, 240),
                        ("stopped within 15 min of entry", "stopped 15-60 min in", "stopped 1-4h in", "stopped 4h+ in"))
    return f


def collect(con, max_mints=400):
    """Find and label new stop moments. Reads first, then writes in short bursts. Returns rows written."""
    ensure(con)
    now = int(time.time() * 1000)
    todo = {}
    q = ("SELECT f.mint, f.t_min, f.ts, f.price, f.liq FROM coin_features f "
         "LEFT JOIN bounce_events b ON b.mint=f.mint AND b.t_min=f.t_min "
         "WHERE b.mint IS NULL AND f.ts<? AND f.price>0 AND f.liq>=? AND f.auth_ok=1 AND f.t_min IN (%s)"
         % ",".join(str(a) for a in AGES))
    for r in con.execute(q, (now - HORIZON_MS - AFTER_MS, MIN_LIQ)):
        todo.setdefault(r["mint"], []).append((r["t_min"], r["ts"], r["price"], r["liq"]))
    out = []
    for n, (mint, rows) in enumerate(todo.items()):
        if n >= max_mints:
            break
        t0 = min(r[1] for r in rows)
        t1 = max(r[1] for r in rows) + HORIZON_MS + AFTER_MS
        snaps = [dict(s) for s in con.execute(
            "SELECT ts, price, liq, buys_m5, sells_m5, buys_h1, sells_h1 FROM snapshots WHERE mint=? AND ts>? AND ts<=? ORDER BY ts",
            (mint, t0, t1))]
        for t_min, ts, p0, liq0 in rows:
            mine = [s for s in snaps if s["ts"] > ts and s["price"]]
            i = next((k for k, s in enumerate(mine) if s["ts"] <= ts + HORIZON_MS and s["price"] <= p0 * STOP_X), None)
            if i is None:
                out.append((mint, t_min, ts, None, -1))
                continue
            stop = mine[i]
            label = -1                                    # no prices after the stop: can't judge
            for s in mine[i + 1:]:
                if s["ts"] > stop["ts"] + AFTER_MS:
                    break
                label = 2
                if s["price"] >= stop["price"] * UP_X:
                    label = 1
                    break
                if s["price"] <= stop["price"] * DOWN_X:
                    label = 0
                    break
            feat = json.dumps(features(ts, p0, liq0, mine, i)) if label != -1 else None
            out.append((mint, t_min, stop["ts"], feat, label))
    for k in range(0, len(out), 200):
        con.executemany("INSERT OR REPLACE INTO bounce_events VALUES(?,?,?,?,?)", out[k:k + 200])
        time.sleep(0.02)
    return len(out)


def _fit(rows):
    pos = sum(1 for r in rows if r["y"])
    m = {"n": len(rows), "pos": pos, "prior": (pos + 1) / (len(rows) + 2), "counts": {}}
    for r in rows:
        y = "w" if r["y"] else "l"
        for k, v in r["f"].items():
            m["counts"].setdefault(k, {}).setdefault(v, {"w": 0, "l": 0})[y] += 1
    return m


def score(model, feat):
    """Chance (0-100) that a stop moment with these readings bounces 20% before falling another 20%."""
    counts = (model or {}).get("counts") or {}
    if not counts:
        return None
    n_w, n_l = model["pos"], model["n"] - model["pos"]
    logit = math.log(model["prior"] / (1 - model["prior"]))
    for k, v in feat.items():
        c = counts.get(k, {}).get(v, {"w": 0, "l": 0})
        vals = max(2, len(counts.get(k, {})))
        logit += math.log(((c["w"] + 1) / (n_w + vals)) / ((c["l"] + 1) / (n_l + vals)))
    return round(100 / (1 + math.exp(-max(-30, min(30, logit)))))


def build(con, days=30):
    collect(con)
    now = int(time.time() * 1000)
    rows = []
    for r in con.execute("SELECT ts, feat, bounce FROM bounce_events WHERE bounce IN (0,1,2) AND feat IS NOT NULL AND ts>? ORDER BY ts",
                         (now - days * 86400000,)):
        rows.append({"ts": r["ts"], "f": json.loads(r["feat"]), "y": r["bounce"] == 1, "b": r["bounce"]})
    never = con.execute("SELECT COUNT(*) FROM bounce_events WHERE bounce=-1 AND feat IS NULL AND ts>?", (now - days * 86400000,)).fetchone()[0]
    pos = sum(1 for r in rows if r["y"])
    model = {"built": now, "n": len(rows), "pos": pos, "dump": sum(1 for r in rows if r["b"] == 0),
             "flat": sum(1 for r in rows if r["b"] == 2), "never": never,
             "counts": {}, "trusted": False, "check": None, "cut": None}
    if len(rows) >= MIN_ROWS and pos >= MIN_POS:
        k = int(len(rows) * 0.6)
        old, new = rows[:k], rows[k:]
        m0 = _fit(old)
        scored = sorted(((score(m0, r["f"]), r["y"], r["b"]) for r in new), key=lambda x: -x[0])
        base = sum(1 for x in scored if x[1]) / len(scored)
        top = scored[:max(1, len(scored) * 2 // 5)]
        rest = scored[len(top):]
        top_rate = sum(1 for x in top if x[1]) / len(top)
        rest_rate = sum(1 for x in rest if x[1]) / len(rest) if rest else 0
        model["check"] = {"n": len(scored), "base": round(base * 100, 1), "top": round(top_rate * 100, 1),
                          "rest": round(rest_rate * 100, 1), "top_n": len(top),
                          "top_dump": round(sum(1 for x in top if x[2] == 0) / len(top) * 100, 1)}
        model["trusted"] = bool(len(top) >= 60 and base > 0 and top_rate >= base * 1.25 and top_rate > rest_rate)
        full = _fit(rows)
        model.update(counts=full["counts"], prior=full["prior"])
        all_scores = sorted(score(model, r["f"]) for r in rows)
        model["cut"] = all_scores[len(all_scores) * 3 // 5]
    db.kv_set(con, "bounce_model", json.dumps(model))
    return model


def load(con):
    try:
        return json.loads(db.kv_get(con, "bounce_model") or "{}")
    except ValueError:
        return {}


def report(con):
    m = load(con)
    if not m:
        return "🏀 Bounce score: not built yet. It builds itself within 30 minutes of the bot starting."
    n = m.get("n", 0)
    L = ["🏀 <b>Bounce score</b>: when a coin hits the -30% stop, is it a shake-out or a real dump?",
         "Learned from %s stop moments on recorded coins (2h and 4h old, real pool)" % format(n, ",")]
    if n:
        L.append("In the next hour: %.0f%% bounced 20%% first · %.0f%% fell another 20%% first · %.0f%% did neither" % (
            m["pos"] / n * 100, m.get("dump", 0) / n * 100, m.get("flat", 0) / n * 100))
    if not m.get("counts"):
        L.append("Still collecting: needs %d stop moments and %d bounces before it can learn." % (MIN_ROWS, MIN_POS))
        return "\n".join(L)
    c = m.get("check") or {}
    L.append("\n<b>Honest check</b> (trained on older stop moments, tested on %s newer ones it never saw)" % format(c.get("n", 0), ","))
    L.append("Its top 40%% picks bounced %.0f%% of the time (and %.0f%% of them dumped) · the rest bounced %.0f%% · average %.0f%%" % (
        c.get("top", 0), c.get("top_dump", 0), c.get("rest", 0), c.get("base", 0)))
    L.append("Verdict: <b>%s</b>" % (
        "trusted. It can tell likely bounces apart. Next step: test a rule that holds through the stop when the score is %d or higher." % m["cut"]
        if m.get("trusted") else
        "promising, but not trusted yet. Its top picks did beat average, on too few unseen stop moments to count (needs 60, has %d)." % c.get("top_n", 0)
        if c.get("top_n", 0) < 60 and c.get("base") and c.get("top", 0) >= c["base"] * 1.25 else
        "not trusted yet. Its picks didn't clearly beat average on unseen coins, so a bounce and a dump still look alike to it."))
    base = m["pos"] / n * 100
    good, bad = [], []
    for k, vals in m["counts"].items():
        for v, cnt in vals.items():
            tot = cnt["w"] + cnt["l"]
            if tot >= 30 and tot < n * 0.95:
                rate = cnt["w"] / tot * 100
                (good if rate >= base * 1.25 else bad if rate <= base * 0.75 else []).append((rate, v, tot))
    if good:
        L.append("\n<b>Signs of a bounce</b> (bounce rate when present · average is %.0f%%)" % base)
        for rate, v, tot in sorted(good, reverse=True)[:6]:
            L.append("%s: %.0f%% (%s stops)" % (v[0].upper() + v[1:], rate, format(tot, ",")))
    if bad:
        L.append("\n<b>Signs of a real dump</b>")
        for rate, v, tot in sorted(bad)[:6]:
            L.append("%s: %.0f%% (%s stops)" % (v[0].upper() + v[1:], rate, format(tot, ",")))
    L.append("\nA report only: it changes no trades. Retrained every 30 minutes.")
    return "\n".join(L)
