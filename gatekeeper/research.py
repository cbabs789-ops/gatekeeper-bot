"""Sunday research report: what the last week of data says, in one message.

Everything here comes from data the bot already collects for free:
trader buys priced 1h/6h/24h later (trader_calls), the coins it watched and
whether they died (coins), and the safety checks (safety).
"""
import time
from collections import defaultdict

from . import fomo

DAY = 86400000


def _med(v):
    v = sorted(v)
    return v[len(v) // 2] if v else None


def _group_stats(rows):
    ch = [(r["p6h"] / r["p0"] - 1) * 100 for r in rows]
    drained = [r for r in rows if r["liq24h"] is not None and r["liq24h"] >= 0 and r["liq0"]]
    return {"n": len(rows), "med6h": _med(ch), "win": sum(1 for x in ch if x > 10) / len(ch) * 100 if ch else None,
            "drained": (sum(1 for r in drained if r["liq24h"] < r["liq0"] * 0.2) / len(drained) * 100) if drained else None}


def _fmt(label, g):
    if not g["n"]:
        return "%s: no data" % label
    return "%s: %s buys · median %+.0f%% at 6h · %.0f%% up 10%%+%s" % (
        label, format(g["n"], ","), g["med6h"], g["win"],
        " · %.0f%% drained" % g["drained"] if g["drained"] is not None else "")


def compute(con, days=7):
    fomo.ensure_schema(con)
    since = int(time.time() * 1000) - days * DAY
    calls = [dict(r) for r in con.execute(
        "SELECT symbol, socials, p0, p6h, liq0, liq24h FROM trader_calls WHERE ts>=? AND p0>0 AND p6h>=0", (since,))]
    out = {"calls": len(calls), "themes": [], "socials": [], "safety": [], "dead_base": None}

    by = defaultdict(list)
    for r in calls:
        by[fomo.theme_of(r["symbol"] or "")].append(r)
    out["themes"] = sorted(((t, _group_stats(rs)) for t, rs in by.items() if len(rs) >= 20),
                           key=lambda x: x[1]["med6h"], reverse=True)

    known = [r for r in calls if r["socials"] is not None]
    def split(tag, yes, no):
        a = [r for r in known if tag in (r["socials"] or "").split(",")]
        b = [r for r in known if tag not in (r["socials"] or "").split(",")]
        return [(yes, _group_stats(a)), (no, _group_stats(b))]
    if known:
        out["socials"] = (split("x", "Has an X account", "No X account") + split("web", "Has a website", "No website")
                          + split("boost", "Paid DexScreener boost", "No boost"))

    rows = [dict(r) for r in con.execute(
        "SELECT c.status, s.* FROM coins c JOIN safety s ON s.mint=c.mint "
        "WHERE c.status IN ('dead','expired') AND COALESCE(c.added_at, c.graduated_at)>=?", (since,))]
    flags = [
        ("Mint authority still on", lambda s: not s["mint_revoked"]),
        ("Freeze authority still on", lambda s: not s["freeze_revoked"]),
        ("LP under 90% locked", lambda s: not s.get("lp_na") and (s["lp_locked"] or 0) < 90),
        ("Top 10 hold over 30%", lambda s: (s["top10"] or 0) > 30),
        ("Over 10 insider wallets", lambda s: (s["insiders"] or 0) > 10),
        ("RugCheck danger flag", lambda s: bool(s["danger"])),
        ("Dev launched 3+ coins before", lambda s: (s.get("creator_prev") or 0) >= 3),
        ("Dev has a dead coin", lambda s: (s.get("creator_dead") or 0) >= 1),
    ]
    def rate(rs):
        return (sum(1 for r in rs if r["status"] == "dead") / len(rs) * 100, len(rs)) if rs else (None, 0)
    clean = [r for r in rows if not any(f(r) for _, f in flags)]
    out["dead_base"] = rate(clean)
    for label, f in flags:
        d, n = rate([r for r in rows if f(r)])
        if n >= 10:
            out["safety"].append((label, d, n))
    out["safety"].sort(key=lambda x: -x[1])
    return out


def text(con, days=7):
    res = compute(con, days)
    L = ["🔬 <b>Research report</b> (last %d days)" % days]
    if not res["calls"]:
        L.append("\nNo Fomo buys with 6-hour results yet. The first ones land about 6 hours after the scorecard starts.")
    else:
        L.append("\n<b>Themes that paid</b> (what coins did 6h after any Fomo trader bought)")
        L += [_fmt(t, g) for t, g in res["themes"][:8]] or ["Not enough buys per theme yet (need 20+)."]
        if res["socials"]:
            L.append("\n<b>Do socials matter?</b>")
            for i in range(0, len(res["socials"]), 2):
                (a, ga), (b, gb) = res["socials"][i], res["socials"][i + 1]
                L += [_fmt(a, ga), _fmt(b, gb)]
                if ga["n"] >= 30 and gb["n"] >= 30:
                    diff = ga["med6h"] - gb["med6h"]
                    L.append("  → %s" % ("real difference (%+.0f points)" % diff if abs(diff) >= 5 else "no real difference"))
    base, n = res["dead_base"]
    L.append("\n<b>Which safety warnings predicted a dead coin</b>")
    if base is None:
        L.append("Not enough finished coins yet.")
    else:
        L.append("Coins that passed everything: %.0f%% died within the watch window (%s coins)" % (base, format(n, ",")))
        for label, d, k in res["safety"]:
            L.append("%s: %.0f%% died (%s coins)%s" % (label, d, format(k, ","), " ⚠️" if d >= base + 15 else ""))
    L.append("\n⚠️ = at least 15 points worse than clean coins, so that check is earning its keep. Best new traders are in /traders.")
    return "\n".join(L)
