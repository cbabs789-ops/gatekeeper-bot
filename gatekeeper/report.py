"""Human-readable summaries for Telegram and the command line."""
import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import config
from .notify import money
from .strategy import summarize

TZ = ZoneInfo(config.TIMEZONE)
LABEL = {"main": "Main (strict rules)", "wide": "Wide (newer coins, looser)", "follow": "Follow (copies your Fomo traders)", "momentum": "Momentum (day-trader style)", "survivor": "Survivor (4h+ coins climbing steadily)", "moonshot": "Moonshot Hunter ($20 bets on early 10x signs)",
         "x_all40": "Test: sell all at +40%", "x_trail10": "Test: tighter lock (10% trail)", "x_skip35": "Test: stricter rug skip (35%)", "x_noinsider": "Test: no insider rule", "x_insider1": "Test: skip if any insider wallet is in"}


def _closed(con, since_ms=None, strategy=None):
    q = "SELECT * FROM trades WHERE mode='live' AND closed_at IS NOT NULL"
    args = []
    if strategy:
        q += " AND COALESCE(run_id,'main')=?"
        args.append(strategy)
    if since_ms:
        q += " AND closed_at>=?"
        args.append(since_ms)
    return [SimpleNamespace(pnl_usd=r["pnl_usd"], size_usd=r["size_usd"], exit_reason=r["exit_reason"] or "",
                            symbol=r["symbol"], pnl_pct=r["pnl_pct"]) for r in con.execute(q + " ORDER BY closed_at", args)]


def stats_lines(s, compact=False):
    if not s.get("trades"):
        return ["No closed paper trades yet."]
    lines = [
        "Trades: %d · win rate %.0f%%" % (s["trades"], s["win_rate"]),
        "Paper profit: %s on %s put in" % (money(s["total_pnl"]), money(s["invested"])),
        "Avg win %s · avg loss %s" % (money(s["avg_win"]), money(s["avg_loss"])),
    ]
    if compact:
        return lines
    lines += [
        "Best %s · worst %s" % (money(s["best"]), money(s["worst"])),
        "Deepest drawdown %s · longest losing streak %d" % (money(s["max_drawdown"]), s["worst_losing_streak"]),
        "By exit:",
    ]
    for k, (n, pnl) in sorted(s["by_exit"].items(), key=lambda kv: -kv[1][0]):
        lines.append("  %s: %d trades, %s" % (k, n, money(pnl)))
    return lines


def funnel_lines(con, since_ms):
    start_hour = datetime.fromtimestamp(since_ms / 1000, TZ).strftime("%Y-%m-%d %H")
    created = con.execute("SELECT COALESCE(SUM(created),0) c FROM launches WHERE day>=?", (start_hour,)).fetchone()["c"]
    grad = con.execute("SELECT COUNT(*) n FROM coins WHERE graduated_at>=?", (since_ms,)).fetchone()["n"]
    checked = con.execute("SELECT COUNT(*) n FROM safety WHERE checked_at>=?", (since_ms,)).fetchone()["n"]
    passed = con.execute("SELECT COUNT(*) n FROM safety WHERE checked_at>=? AND mint_revoked=1 AND freeze_revoked=1 "
                         "AND lp_locked>=90 AND danger IS NULL", (since_ms,)).fetchone()["n"]
    traded = con.execute("SELECT COUNT(DISTINCT mint) n FROM trades WHERE mode='live' AND opened_at>=?", (since_ms,)).fetchone()["n"]
    rugs = con.execute("SELECT COUNT(*) n FROM coins WHERE graduated_at>=? AND status='dead'", (since_ms,)).fetchone()["n"]
    return ["<b>Coin funnel</b>",
            "Launched on pump.fun: %s" % format(created, ","),
            "Graduated to a real pool: %s" % format(grad, ","),
            "Died or drained already: %s" % format(rugs, ","),
            "Safety-checked: %d · passed basic safety: %d" % (checked, passed),
            "Traded by at least one strategy: %d" % traded]


def period_text(con, hours=24, header=True):
    since = int(time.time() * 1000) - hours * 3600 * 1000
    label = "Last 24 hours" if hours == 24 else ("Since the start" if hours > 24 * 365 else "Last %d days" % (hours // 24))
    out = ["<b>%s</b>" % label] if header else []
    total = 0
    for name in config.ENABLED_PRESETS:
        s = summarize(_closed(con, since, name))
        total = max(total, s.get("trades", 0))
        n_open = con.execute("SELECT COUNT(*) n FROM trades WHERE mode='live' AND closed_at IS NULL AND COALESCE(run_id,'main')=?",
                             (name,)).fetchone()["n"]
        out.append("\n<b>%s</b>" % LABEL.get(name, name))
        out += stats_lines(s, compact=hours <= 24)
        out.append("Open right now: %d" % n_open)
    if hours <= 24 * 7:
        out.append("")
        out += funnel_lines(con, since)
    all_trades = max(len(_closed(con, None, n)) for n in config.ENABLED_PRESETS) if config.ENABLED_PRESETS else 0
    if all_trades < 100:
        out.append("\nTrades so far (best strategy): %d of the 100 needed before judging the rules." % all_trades)
    return "\n".join(out)


def experiments_text(con):
    """Main vs its silent test copies, side by side (since each copy started)."""
    names = ["main"] + list(config.SHADOW_PRESETS)
    rows = []
    start = None
    for n in config.SHADOW_PRESETS:
        r = con.execute("SELECT MIN(opened_at) t FROM trades WHERE mode='live' AND run_id=?", (n,)).fetchone()
        if r and r["t"]:
            start = r["t"] if start is None else min(start, r["t"])
    L = ["🧪 <b>Experiments</b> (paper, no alerts)" + (" since %s" % datetime.fromtimestamp(start / 1000, TZ).strftime("%b %-d %-I:%M %p") if start else "")]
    if not start:
        L.append("No experiment trades yet. They trade the same coins as Main, so they start when Main finds its next coin.")
        return "\n".join(L)
    for n in names:
        s = summarize(_closed(con, start, n))
        if not s.get("trades"):
            L.append("%s: no closed trades yet" % LABEL.get(n, n))
            continue
        L.append("%s: %d trades · %.0f%% win · %s (%s a trade)" % (LABEL.get(n, n).replace("Main (strict rules)", "Main (as set)"), s["trades"],
                 s["win_rate"], money(s["total_pnl"]), money(s["avg_pnl"])))
    L.append("Same coins, different exits. After about a week, the best one becomes Main.")
    return "\n".join(L)


def status_text(con, strats=None):
    now = int(time.time() * 1000)
    last = con.execute("SELECT v FROM kv WHERE k='last_poll'").fetchone()
    watching = con.execute("SELECT COUNT(*) n FROM coins WHERE status='watching'").fetchone()["n"]
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    day = con.execute("SELECT COALESCE(SUM(created),0) created, COALESCE(SUM(graduated),0) graduated FROM launches "
                      "WHERE day LIKE ?", (today + "%",)).fetchone()
    snaps = con.execute("SELECT COUNT(*) n FROM snapshots WHERE ts>=?", (now - 86400000,)).fetchone()["n"]
    lines = ["<b>Gatekeeper status</b>"]
    lines.append("Last price check: %s" % ("%ds ago" % ((now - int(last["v"])) // 1000) if last else "never"))
    lines.append("Coins being watched: %d" % watching)
    if day:
        lines.append("Today: %s launches seen, %s graduated" % (format(day["created"], ","), format(day["graduated"], ",")))
    lines.append("Snapshots recorded (24h): %s" % format(snaps, ","))
    try:
        import shutil
        du = shutil.disk_usage(str(config.DATA_DIR))
        lines.append("Server disk: %.1f GB free of %.1f GB%s" % (du.free / 1e9, du.total / 1e9, " ⚠️ getting low" if du.free < 1.5e9 else ""))
    except OSError:
        pass
    if config.os.environ.get("FOMO_API_KEY"):
        fl = con.execute("SELECT v FROM kv WHERE k='fomo_last_event'").fetchone()
        fd = con.execute("SELECT v FROM kv WHERE k='fomo_feed_delay'").fetchone()
        lines.append("Fomo feed: %s%s" % ("last trade %ds ago" % ((now - int(fl["v"])) // 1000) if fl else "waiting for first trade",
                                          " (%ss delay)" % fd["v"] if fd and fd["v"] not in ("0", "0.0") else ""))
    rows = [r for r in con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NULL ORDER BY opened_at").fetchall() if (r['run_id'] or 'main') not in config.SHADOW]
    if rows:
        lines.append("\n<b>Open paper trades</b>")
        for r in rows:
            name = r["run_id"] or "main"
            st = (strats or {}).get(name)
            cur = st.coins[r["mint"]].last_price if st and r["mint"] in st.coins else None
            mins = (now - r["opened_at"]) // 60000
            spot = json.loads(r["legs"] or "[{}]")[0].get("spot")
            chg = " · {:+.0f}% since entry".format((cur / spot - 1) * 100) if cur and spot else ""
            lines.append("${} [{}]{} · {}m".format(r["symbol"], name, chg, mins))
    else:
        lines.append("No open paper trades.")
    return "\n".join(lines)


def exits_text(con, days=3):
    """What each exit rule did: how often the coin went on to rise after we sold (sold too early)
    versus kept falling (the exit saved us). Plus the full-loss rugs and their rug-risk score at entry."""
    import re
    since = int(time.time() * 1000) - days * 86400000
    rows = con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NOT NULL AND closed_at>=?", (since,)).fetchall()
    if not rows:
        return "No closed trades in the last %d days." % days
    by = {}
    rugs = []
    for r in rows:
        legs = json.loads(r["legs"] or "[]")
        sells = [g for g in legs if g.get("side") == "sell" and g.get("spot")]
        if not sells:
            continue
        reason = re.split(r" \(|:| ·", r["exit_reason"] or "?")[0].strip()
        exit_px = sells[-1]["spot"]
        after = con.execute("SELECT MAX(price) hi, MIN(price) lo FROM snapshots WHERE mint=? AND ts>? AND ts<=?",
                            (r["mint"], r["closed_at"], r["closed_at"] + 3600000)).fetchone()
        later = con.execute("SELECT price FROM snapshots WHERE mint=? AND ts>=? ORDER BY ts LIMIT 1",
                            (r["mint"], r["closed_at"] + 3600000)).fetchone()
        b = by.setdefault(reason, {"n": 0, "pnl": 0.0, "early": 0, "saved": 0, "known": 0, "h1": []})
        if later and later["price"]:
            b["h1"].append((later["price"] / exit_px - 1) * 100)
        elif (r["exit_reason"] or "").startswith(("Liquidity", "Pool", "No data")):
            b["h1"].append(-100.0)          # coin stopped trading: it went to zero
        b["n"] += 1
        b["pnl"] += r["pnl_usd"] or 0
        if after and after["hi"]:
            b["known"] += 1
            if after["hi"] >= exit_px * 1.2:
                b["early"] += 1
            if after["lo"] and after["lo"] <= exit_px * 0.8:
                b["saved"] += 1
        if (r["pnl_pct"] or 0) <= -70:
            m = re.search(r"rug risk (\d+)%", r["why_entered"] or "")
            rugs.append((r["symbol"], r["pnl_usd"] or 0, int(m.group(1)) if m else None, r["run_id"] or "main"))
    L = ["🔍 <b>How the exits did</b> (last %d days)" % days,
         "Per exit: trades · profit · coin rose 20%+ within 1h after we sold (too early) · fell another 20%+ (exit saved us)\n"]
    for reason, b in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        k = b["known"] or 1
        h1 = sorted(b["h1"])
        med = h1[len(h1) // 2] if h1 else None
        verdict = "" if med is None else (" · 1h later: typical coin %+.0f%% vs our sale → %s" % (
            med, "sold too early" if med > 10 else "exit helped" if med < -10 else "about even"))
        L.append("<b>%s</b>: %d · %s · %.0f%% too early · %.0f%% saved us%s" % (
            reason, b["n"], money(b["pnl"]), b["early"] / k * 100, b["saved"] / k * 100, verdict))
    if rugs:
        L.append("\n<b>Near-total losses (-70%% or worse)</b>: %d trades, %s" % (len(rugs), money(sum(x[1] for x in rugs))))
        scored = [x for x in rugs if x[2] is not None]
        if scored:
            hi = sum(1 for x in scored if x[2] >= 50)
            L.append("Rug risk at entry: %s · %d of %d were scored 50%%+" % (
                ", ".join("$%s %d%%" % (x[0], x[2]) for x in scored[:8]), hi, len(scored)))
    L.append("\nAn exit that's 'too early' far more often than it 'saved us' is the one to loosen.")
    return "\n".join(L)
