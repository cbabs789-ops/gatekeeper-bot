"""Trader playbook: how the Fomo traders who actually make money trade, learned from every buy AND sell on the feed.

The scorecard only asks "did the coin go up after they bought". This asks what the trader did:
how much they put in, how fast they sold, in how many pieces, how much cash came back, and whether
the traders who look best keep being best later (skill) or not (luck).

A "position" is one trader in one coin: their buys, then their sells. What it was worth:
  sold some or all  -> the stake plus the profit or loss locked in (the feed's dollar amount on a sell is the
                       realized profit, not the proceeds; anything left unsold counts at cost)
  never sold        -> what the buy was worth 24h later (so a rug they could not sell counts as a loss)
Only positions that have been quiet for 24h are counted, and only on chains the bot can price.
"""
import json
import time
from collections import defaultdict

from . import db, fomo

H = 3600 * 1000
DAY = 24 * H
DAYS = 21
GAP = 6 * H               # a buy this long after the last trade, once they have sold, starts a new position
MIN_IN = 50               # ignore dust positions
CAP_X = 10                # one position counts for at most 10x its cost when ranking, so one lucky hit can't crown a trader
MIN_HALF = 5              # finished positions needed in the older half to be ranked for the honest check
MIN_ALL = 10              # ...and overall to be described in the playbook
COST = 0.94               # the paper broker's costs: about 3% each way
MIN_LIQ = 10000           # copy test: skip pools too small to really sell into


def _med(v):
    v = sorted(x for x in v if x is not None)
    return v[len(v) // 2] if v else None


def _positions(con, since, now):
    """Walk the feed in time order and fold it into positions. Returns a list of dicts."""
    cur, done = {}, []
    q = con.execute("SELECT ts, trader, side, token, token_address, chain, usd FROM fomo_events WHERE ts>=? ORDER BY ts", (since,))
    for r in q:
        trader = r["trader"] or ""
        addr, chain = fomo.covered(r["token_address"], r["chain"])
        if not trader or not addr or r["side"] not in ("buy", "sell"):
            continue
        k = (trader, addr)
        p = cur.get(k)
        usd = r["usd"] or 0
        if r["side"] == "buy":
            if p is not None and (p["n_sell"] or p["pre"]) and r["ts"] - p["last"] > GAP:
                done.append(p)
                p = None
            if p is None:
                p = cur[k] = {"trader": trader, "addr": addr, "sym": r["token"] or "", "t0": r["ts"], "last": r["ts"], "in": 0.0,
                              "out": 0.0, "n_buy": 0, "n_sell": 0, "s0": None, "s1": None, "pre": False, "bad": False}
            p["in"] += usd
            p["n_buy"] += 1
        else:
            if p is None:
                # a sell with no buy on record: bought before we were recording, so its result can't be known
                p = cur[k] = {"trader": trader, "addr": addr, "sym": r["token"] or "", "t0": r["ts"], "last": r["ts"], "in": 0.0,
                              "out": 0.0, "n_buy": 0, "n_sell": 0, "s0": None, "s1": None, "pre": True, "bad": False}
            # the feed reports a sell's dollar amount as the profit or loss it locked in, not the sale proceeds
            p["out"] += usd
            p["n_sell"] += 1
            if p["s0"] is None:
                p["s0"] = r["ts"]
            p["s1"] = r["ts"]
        if r["side"] == "buy" and usd <= 0:
            p["bad"] = True          # the feed's dollar amount was missing or impossible
        p["last"] = r["ts"]
    done += list(cur.values())
    return done


def _calls(con, since):
    """(trader, coin) -> the scorecard's priced buys, oldest first."""
    out = defaultdict(list)
    for r in con.execute("SELECT trader, token_address, ts, p0, liq0, p1h, p6h, p24h FROM trader_calls WHERE ts>=? ORDER BY ts", (since - H,)):
        out[(r["trader"], r["token_address"])].append(r)
    return out


def _ratio(p0, p):
    """Price multiple, or None when there is no reading (-1 means the checkpoint was missed)."""
    if not p0 or p0 <= 0 or p is None or p < 0:
        return None
    return p / p0


def _finish(p, calls, ages, now):
    """Fill in value and the facts used by the playbook. Returns False if the position can't be judged."""
    if p["pre"] or p["bad"] or p["in"] < MIN_IN or now - p["last"] < DAY:
        return False
    c = None
    for x in calls.get((p["trader"], p["addr"]), ()):
        if p["t0"] - 60000 <= x["ts"] <= p["t0"] + GAP:
            c = x
            break
    p["liq0"] = c["liq0"] if c is not None and c["p0"] and c["p0"] > 0 else None
    p["x1"], p["x6"], p["x24"] = (_ratio(c["p0"], c[k]) for k in ("p1h", "p6h", "p24h")) if c is not None else (None, None, None)
    if p["n_sell"]:
        p["value"] = max(0.0, p["in"] + p["out"])      # stake plus the profit or loss locked in (anything unsold counted at cost)
    elif p["x24"] is not None:
        p["value"] = p["in"] * p["x24"]
    else:
        return False                 # never sold and no price 24h later: unknown, so left out rather than guessed
    g = ages.get(p["addr"])
    p["age"] = (p["t0"] - g) / 60000 if g and p["t0"] >= g else None
    return True


def _trader_table(pos):
    by = defaultdict(lambda: {"n": 0, "in": 0.0, "val": 0.0, "capped": 0.0, "wins": 0})
    for p in pos:
        t = by[p["trader"]]
        t["n"] += 1
        t["in"] += p["in"]
        t["val"] += p["value"]
        t["capped"] += min(p["value"], p["in"] * CAP_X)
        t["wins"] += p["value"] > p["in"]
    for t in by.values():
        t["x"] = t["capped"] / t["in"] if t["in"] else 0
    return by


def _pool(pos):
    """Money back per $1 in, pooled over positions (each capped at CAP_X), and share of positions that made money."""
    i = sum(p["in"] for p in pos)
    if not pos or not i:
        return None
    return {"n": len(pos), "traders": len({p["trader"] for p in pos}),
            "x": round(sum(min(p["value"], p["in"] * CAP_X) for p in pos) / i, 3),
            "win": round(sum(1 for p in pos if p["value"] > p["in"]) / len(pos) * 100, 1)}


def _top(table, min_n):
    """Names of the best fifth of traders with enough positions, by money back per $1."""
    ok = sorted(((t["x"], name) for name, t in table.items() if t["n"] >= min_n), reverse=True)
    return [name for _, name in ok[:max(1, len(ok) // 5)]], len(ok)


def _habits(pos):
    if not pos:
        return None
    sold = [p for p in pos if p["n_sell"]]
    first = [(p["s0"] - p["t0"]) / 60000 for p in sold]
    def share(mins):
        return round(sum(1 for x in first if x <= mins) / len(pos) * 100, 1)
    both = [p for p in pos if p["n_sell"] and p["x6"] is not None and p["x24"] is not None]
    i = sum(p["in"] for p in both)
    return {"n": len(pos), "size": _med([p["in"] for p in pos]), "buys": round(sum(p["n_buy"] for p in pos) / len(pos), 2),
            "first_sell": _med(first), "last_sell": _med([(p["s1"] - p["t0"]) / 60000 for p in sold]),
            "sells": round(sum(p["n_sell"] for p in sold) / len(sold), 2) if sold else None,
            "in1h": share(60), "in6h": share(360), "in24h": share(1440),
            "never": round((len(pos) - len(sold)) / len(pos) * 100, 1),
            "age": _med([p["age"] for p in pos]), "age_n": sum(1 for p in pos if p["age"] is not None),
            "liq": _med([p["liq0"] for p in pos]), "liq_n": sum(1 for p in pos if p["liq0"]),
            # same positions, three ways: what they cashed out vs holding their buy for 6h or 24h
            "exit": {"n": len(both), "got": round(sum(min(p["value"], p["in"] * CAP_X) for p in both) / i, 3),
                     "hold6": round(sum(p["in"] * min(p["x6"], CAP_X) for p in both) / i, 3),
                     "hold24": round(sum(p["in"] * min(p["x24"], CAP_X) for p in both) / i, 3)} if len(both) >= 20 and i else None}


def _copy(pos):
    """Copying these buys with $100 and selling on a clock, after the paper broker's costs. One buy per coin per 6 hours."""
    seen, rows = {}, []
    for p in sorted(pos, key=lambda p: p["t0"]):
        if not p.get("liq0") or p["liq0"] < MIN_LIQ or None in (p["x1"], p["x6"], p["x24"]):
            continue
        if p["addr"] in seen and p["t0"] - seen[p["addr"]] < GAP:
            continue
        seen[p["addr"]] = p["t0"]
        rows.append(p)
    if not rows:
        return {"n": 0}
    out = {"n": len(rows)}
    for k in ("x1", "x6", "x24"):
        net = [(min(p[k], CAP_X) * COST - 1) * 100 for p in rows]
        out[k] = {"avg": round(sum(net) / len(net), 1), "win": round(sum(1 for x in net if x > 0) / len(net) * 100, 1)}
    return out


def build(con, days=DAYS):
    fomo.ensure_schema(con)
    now = int(time.time() * 1000)
    since = now - days * DAY
    calls = _calls(con, since)
    ages = {r["mint"]: r["graduated_at"] for r in con.execute("SELECT mint, graduated_at FROM coins WHERE graduated_at IS NOT NULL")}
    raw = _positions(con, since, now)
    seen = len(raw)
    pos = [p for p in raw if _finish(p, calls, ages, now)]
    del raw
    res = {"built": now, "days": days, "seen": seen, "n": len(pos), "traders": len({p["trader"] for p in pos}),
           "first": min((p["t0"] for p in pos), default=None)}
    try:
        r = con.execute("SELECT raw FROM fomo_events ORDER BY ts DESC LIMIT 1").fetchone()
        res["fields"] = sorted(json.loads(r["raw"]).keys())[:30] if r and r["raw"] else []
    except Exception:  # noqa: BLE001
        res["fields"] = []
    if pos:
        res["all"] = _pool(pos)
        # honest check: pick the best traders using only the older half, then see what the SAME traders did afterwards
        lo, hi = min(p["t0"] for p in pos), max(p["t0"] for p in pos)
        mid = (lo + hi) // 2
        old, new = [p for p in pos if p["t0"] < mid], [p for p in pos if p["t0"] >= mid]
        top_old, ranked = _top(_trader_table(old), MIN_HALF)
        s = set(top_old)
        res["check"] = {"mid": mid, "ranked": ranked, "top": len(top_old),
                        "then": _pool([p for p in old if p["trader"] in s]),
                        "after": _pool([p for p in new if p["trader"] in s]),
                        "others_after": _pool([p for p in new if p["trader"] not in s]),
                        "copy_top": _copy([p for p in new if p["trader"] in s]),
                        "copy_others": _copy([p for p in new if p["trader"] not in s])}
        # the playbook itself: habits of the best fifth over the whole period, next to everyone else
        table = _trader_table(pos)
        top_all, ranked_all = _top(table, MIN_ALL)
        s2 = set(top_all)
        res["ranked_all"] = ranked_all
        res["top_habits"] = _habits([p for p in pos if p["trader"] in s2]) if ranked_all >= 10 else None
        res["rest_habits"] = _habits([p for p in pos if p["trader"] not in s2]) if ranked_all >= 10 else None
        res["names"] = [{"t": n, "n": table[n]["n"], "x": round(table[n]["x"], 2), "win": round(table[n]["wins"] / table[n]["n"] * 100)}
                        for n in top_all[:10]]
    db.kv_set(con, "playbook", json.dumps(res))
    return res


def load(con):
    try:
        return json.loads(db.kv_get(con, "playbook") or "{}")
    except ValueError:
        return {}


def _mins(m):
    if m is None:
        return "n/a"
    return "%.0f min" % m if m < 90 else ("%.1f hours" % (m / 60) if m < 2880 else "%.1f days" % (m / 1440))


def _x(v):
    return "$%.2f" % v


def _usd(v):
    return "n/a" if v is None else ("$%.1fK" % (v / 1000) if v >= 1000 else "$%.0f" % v)


def report(con):
    r = load(con)
    L = ["📓 <b>Trader playbook</b>: how the Fomo traders who make money actually trade"]
    if not r:
        return L[0] + "\nNot built yet. It is built when the bot starts and every 2 hours."
    if not r.get("n"):
        return "\n".join(L + ["No finished positions yet (%s seen). A position counts once it has been quiet for 24 hours." % format(r.get("seen", 0), ",")])
    a = r["all"]
    L.append("From %s finished positions by %s traders (buys and sells, last %d days). A sold position is worth the stake plus the profit or loss "
             "locked in when selling (anything unsold counts at cost); a coin never sold is worth its price 24h after the buy." % (format(r["n"], ","), format(r["traders"], ","), r["days"]))
    L.append("\n<b>Everyone</b>: %s back per $1 put in · %.0f%% of positions made money" % (_x(a["x"]), a["win"]))
    c = r.get("check") or {}
    L.append("\n<b>Are the top traders skilled or lucky?</b>")
    if not c.get("after") or not c.get("others_after") or c.get("top", 0) < 5:
        L.append("Not enough history yet: needs traders with %d+ finished positions in the older half of the data (%d so far)." % (MIN_HALF, c.get("ranked", 0)))
    else:
        t, af, o = c["then"], c["after"], c["others_after"]
        L.append("Best fifth in the older half (%d of %d traders): %s back per $1 then." % (c["top"], c["ranked"], _x(t["x"])))
        L.append("The same traders afterwards: %s per $1 on %d positions (%.0f%% made money)." % (_x(af["x"]), af["n"], af["win"]))
        L.append("Everyone else afterwards: %s per $1 on %s positions (%.0f%% made money)." % (_x(o["x"]), format(o["n"], ","), o["win"]))
        if af["n"] < 50:
            L.append("Verdict: too few positions afterwards to call it.")
        elif af["x"] > 1 and af["x"] > o["x"] * 1.05:
            L.append("Verdict: skill. They kept making money, and more than the rest.")
        elif af["x"] > o["x"] * 1.05:
            L.append("Verdict: better than the rest afterwards, but they still lost money.")
        else:
            L.append("Verdict: luck. Once picked, they did no better than everyone else.")
    th, rh = r.get("top_habits"), r.get("rest_habits")
    if th and rh:
        L.append("\n<b>The playbook</b> (best fifth of %d traders with %d+ positions · everyone else)" % (r["ranked_all"], MIN_ALL))
        L.append("Money per position: %s · %s" % (_usd(th["size"]), _usd(rh["size"])))
        L.append("First sale after buying: %s · %s" % (_mins(th["first_sell"]), _mins(rh["first_sell"])))
        L.append("Fully out after: %s · %s" % (_mins(th["last_sell"]), _mins(rh["last_sell"])))
        L.append("Sold something within 1h: %.0f%% · %.0f%%" % (th["in1h"], rh["in1h"]))
        L.append("Sold something within 6h: %.0f%% · %.0f%%" % (th["in6h"], rh["in6h"]))
        L.append("Sales per position: %s · %s" % (th["sells"] if th["sells"] is not None else "n/a", rh["sells"] if rh["sells"] is not None else "n/a"))
        L.append("Buys per position: %s · %s" % (th["buys"], rh["buys"]))
        L.append("Never sold: %.0f%% · %.0f%%" % (th["never"], rh["never"]))
        if th["liq_n"] >= 20 and rh["liq_n"] >= 20:
            L.append("Pool size when they buy: %s · %s" % (_usd(th["liq"]), _usd(rh["liq"])))
        if th["age_n"] >= 20 and rh["age_n"] >= 20:
            L.append("Coin age when they buy (new pump.fun coins only): %s · %s" % (_mins(th["age"]), _mins(rh["age"])))
        for label, h in (("Best fifth", th), ("Everyone else", rh)):
            e = h.get("exit")
            if e:
                L.append("%s, same %s positions: their sells came to %s per $1 · holding 6h would be %s · holding 24h %s" % (
                    label, format(e["n"], ","), _x(e["got"]), _x(e["hold6"]), _x(e["hold24"])))
        if r.get("names"):
            L.append("Best fifth, top names: " + ", ".join("@%s (%d, %s)" % (n["t"], n["n"], _x(n["x"])) for n in r["names"]))
    ct, co = c.get("copy_top") or {}, c.get("copy_others") or {}
    if ct.get("n"):
        L.append("\n<b>Could the bot copy them?</b> $100 on each coin the older half's best traders bought afterwards, sold on a clock, after costs")
        def row(label, d):
            return "%s (%s coins): sell at 1h %+.1f (%.0f%% win) · 6h %+.1f (%.0f%%) · 24h %+.1f (%.0f%%)" % (
                label, format(d["n"], ","), d["x1"]["avg"], d["x1"]["win"], d["x6"]["avg"], d["x6"]["win"], d["x24"]["avg"], d["x24"]["win"])
        L.append(row("Best traders' buys", ct))
        if co.get("n"):
            L.append(row("Everyone else's buys", co))
        best = max(("x1", "x6", "x24"), key=lambda k: ct[k]["avg"])
        if ct["n"] < 30:
            L.append("Verdict: under 30 coins, too few to act on.")
        elif ct[best]["avg"] > 0 and co.get("n", 0) >= 30 and ct[best]["avg"] <= co[best]["avg"]:
            L.append("Verdict: positive at %s, but no better than copying anyone's buys, so picking these traders is not what made it work."
                     % {"x1": "1h", "x6": "6h", "x24": "24h"}[best])
        elif ct[best]["avg"] > 0:
            L.append("Verdict: positive at %s on this sample. Worth a paper bot, not proof." % {"x1": "1h", "x6": "6h", "x24": "24h"}[best])
        else:
            L.append("Verdict: copying their buys loses at every exit time, even picking the best traders.")
    L.append("\nLimits: only trades made on Fomo are seen; a trader's own fees are not counted; one position counts for at most %dx; "
             "the copy test uses the price the bot recorded just after the buy." % CAP_X)
    if r.get("fields"):
        L.append("Feed fields: " + ", ".join(r["fields"]))
    L.append("A report only: it changes no trades. Rebuilt every 2 hours.")
    return "\n".join(L)
