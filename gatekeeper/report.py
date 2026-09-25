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
LABEL = {"main": "Main (strict, alerts on)", "wide": "Wide (looser, silent)"}


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
    if config.os.environ.get("FOMO_API_KEY"):
        fl = con.execute("SELECT v FROM kv WHERE k='fomo_last_event'").fetchone()
        fd = con.execute("SELECT v FROM kv WHERE k='fomo_feed_delay'").fetchone()
        lines.append("Fomo feed: %s%s" % ("last trade %ds ago" % ((now - int(fl["v"])) // 1000) if fl else "waiting for first trade",
                                          " (%ss delay)" % fd["v"] if fd and fd["v"] not in ("0", "0.0") else ""))
    rows = con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NULL ORDER BY opened_at").fetchall()
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
