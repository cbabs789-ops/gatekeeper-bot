"""Human-readable summaries for Telegram and the command line."""
import time
from types import SimpleNamespace

from .notify import money
from .strategy import summarize


def _closed(con, since_ms=None, mode="live"):
    q = "SELECT * FROM trades WHERE mode=? AND closed_at IS NOT NULL"
    args = [mode]
    if since_ms:
        q += " AND closed_at>=?"
        args.append(since_ms)
    return [SimpleNamespace(pnl_usd=r["pnl_usd"], size_usd=r["size_usd"], exit_reason=r["exit_reason"] or "", symbol=r["symbol"],
                            pnl_pct=r["pnl_pct"]) for r in con.execute(q + " ORDER BY closed_at", args)]


def stats_lines(s):
    if not s.get("trades"):
        return ["No closed paper trades yet."]
    lines = [
        "Trades: %d · win rate %.0f%%" % (s["trades"], s["win_rate"]),
        "Paper profit: %s on %s put in" % (money(s["total_pnl"]), money(s["invested"])),
        "Avg win %s · avg loss %s" % (money(s["avg_win"]), money(s["avg_loss"])),
        "Best %s · worst %s" % (money(s["best"]), money(s["worst"])),
        "Deepest drawdown %s · longest losing streak %d" % (money(s["max_drawdown"]), s["worst_losing_streak"]),
        "By exit:",
    ]
    for k, (n, pnl) in sorted(s["by_exit"].items(), key=lambda kv: -kv[1][0]):
        lines.append("  %s: %d trades, %s" % (k, n, money(pnl)))
    return lines


def period_text(con, hours=24, header=True):
    since = int(time.time() * 1000) - hours * 3600 * 1000
    s = summarize(_closed(con, since))
    open_n = con.execute("SELECT COUNT(*) n FROM trades WHERE mode='live' AND closed_at IS NULL").fetchone()["n"]
    label = "Last 24 hours" if hours == 24 else ("All time" if hours > 24 * 365 else "Last %d days" % (hours // 24))
    out = (["<b>%s</b>" % label] if header else []) + stats_lines(s)
    out.append("Open right now: %d" % open_n)
    if s.get("trades", 0) < 100:
        out.append("\n(Judge the rules after 100+ trades, not before.)")
    return "\n".join(out)


def status_text(con, strat=None):
    now = int(time.time() * 1000)
    last = con.execute("SELECT v FROM kv WHERE k='last_poll'").fetchone()
    watching = con.execute("SELECT COUNT(*) n FROM coins WHERE status='watching'").fetchone()["n"]
    day = con.execute("SELECT * FROM launches ORDER BY day DESC LIMIT 1").fetchone()
    snaps = con.execute("SELECT COUNT(*) n FROM snapshots WHERE ts>=?", (now - 86400000,)).fetchone()["n"]
    lines = ["<b>Gatekeeper status</b>"]
    lines.append("Last price check: %s" % ("%ds ago" % ((now - int(last["v"])) // 1000) if last else "never"))
    lines.append("Coins being watched: %d" % watching)
    if day:
        lines.append("Today: %d launches seen, %d graduated" % (day["created"], day["graduated"]))
    lines.append("Snapshots recorded (24h): %s" % format(snaps, ","))
    rows = con.execute("SELECT * FROM trades WHERE mode='live' AND closed_at IS NULL ORDER BY opened_at").fetchall()
    if rows:
        lines.append("\n<b>Open paper trades</b>")
        for r in rows:
            cur = None
            if strat and r["mint"] in strat.coins:
                cur = strat.coins[r["mint"]].last_price
            mins = (now - r["opened_at"]) // 60000
            if cur:
                spot = __import__("json").loads(r["legs"])[0]["spot"]
                lines.append("${} · {:+.0f}% since entry · {}m".format(r["symbol"], (cur / spot - 1) * 100, mins))
            else:
                lines.append("${} · {}m".format(r["symbol"], mins))
    else:
        lines.append("No open paper trades.")
    return "\n".join(lines)
