"""Publish an hourly stats snapshot to a private GitHub repo so it can be read without the dashboard.

Writes latest.md (readable summary), latest.json (numbers) and history/YYYY-MM-DD.md (last snapshot of each
day) to GK_STATS_REPO using GITHUB_STATS_TOKEN (a fine-grained token that can write to that one repo only).
No keys, tokens or wallet secrets are ever included.
"""
import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime

import aiohttp

from . import bounce, config, db, fomo, report, research, risk, winmodel

log = logging.getLogger("gatekeeper.stats")
API = "https://api.github.com/repos/{}/contents/{}"


def token():
    return config.os.environ.get("GITHUB_STATS_TOKEN", "")


def repo():
    return config.os.environ.get("GK_STATS_REPO", "cbabs789-ops/gatekeeper-stats")


def plain(t):
    return re.sub(r"<[^>]+>", "", t or "")


def build(r, con=None, trench_text=None):
    """con: a database connection made in the calling thread (SQLite connections can't cross threads)."""
    con = con or r.con
    now = datetime.now(report.TZ)
    parts = {}

    def safe(name, fn):
        try:
            parts[name] = plain(fn())
        except Exception as e:  # noqa: BLE001
            parts[name] = "(unavailable: %s)" % str(e)[:120]

    keys = ("MIN_AGE_MIN", "MAX_AGE_MIN", "MIN_LIQ_USD", "MAX_TOP10_PCT", "RISK_SKIP", "INSIDER_SKIP", "TAKE_HALF_X",
            "TAKE_PROFIT_PCT", "STOP_LOSS_PCT", "LOCK_START_PCT", "LOCK_TRAIL_PCT", "BREAKEVEN_AT_PCT", "POSITION_USD")
    safe("settings", lambda: "\n".join("%s: %s" % (n, ", ".join("%s=%g" % (k, st.p[k]) for k in keys if k in st.p))
                                       for n, st in r.strats.items()))
    safe("status", lambda: report.status_text(con, r.strats))
    safe("today", lambda: report.period_text(con, hours=24))
    safe("week", lambda: report.period_text(con, hours=24 * 7))
    safe("exits", lambda: report.exits_text(con))
    safe("experiments", lambda: report.experiments_text(con))
    safe("risk", lambda: risk.report(con))
    safe("winscore", lambda: winmodel.report(con))
    safe("bounce", lambda: bounce.report(con))
    safe("ai", lambda: r.analyst.text(con) if getattr(r, "analyst", None) else "")
    safe("hold", lambda: r.hold.text(con) if getattr(r, "hold", None) else "")
    safe("moonshots", lambda: risk.moonshots_text(con))
    safe("traders", lambda: fomo.scorecard_text(con))
    safe("research", lambda: research.text(con))
    safe("trench", lambda: trench_text if trench_text is not None else r.trench.text())
    safe("events", lambda: r.events.plain() if getattr(r, "events", None) else "")
    tr = r.trends.state if getattr(r, "trends", None) else {}
    picks = "\n".join("$%s: %s" % (c["symbol"], " · ".join(c["reasons"])) for c in tr.get("suggestions", [])) or "none right now"
    parts["trends"] = "Hot words: %s\nPicks:\n%s" % (", ".join(tr.get("words", [])[:15]), picks)

    closed = [dict(x) for x in con.execute(
        "SELECT run_id, symbol, mint, opened_at, closed_at, size_usd, pnl_usd, pnl_pct, exit_reason, why_entered FROM trades "
        "WHERE mode='live' AND closed_at IS NOT NULL ORDER BY closed_at DESC LIMIT 100")]
    open_ = [{"strategy": n, "symbol": p.symbol, "mint": m, "opened_at": p.opened_at, "size": p.size_usd, "risk": p.risk,
              "target_x": p.target_x, "why": p.why} for n, st in r.strats.items() for m, p in st.positions.items()]
    data = {"generated": int(time.time() * 1000), "generated_local": now.strftime("%Y-%m-%d %H:%M %Z"),
            "strategies": list(r.strats), "epochs": getattr(r, "epochs", {}), "open": open_, "closed_recent": closed,
            "rug_model": {k: v for k, v in (r.rug_model or {}).items() if k != "counts"}, "reports": parts}
    order = ["settings", "status", "today", "week", "experiments", "ai", "hold", "exits", "winscore", "bounce", "risk", "moonshots", "traders", "trench", "research", "trends", "events"]
    md = ["# Gatekeeper stats: %s" % data["generated_local"], "",
          "Open paper trades: %d in Main/Follow, %d in experiment bots (Hold bot and AI trader are listed in their own sections) · running: %s" % (
              sum(1 for o in open_ if o["strategy"] not in config.SHADOW), sum(1 for o in open_ if o["strategy"] in config.SHADOW),
              ", ".join(r.strats)), ""]
    for k in order:
        md += ["## " + k.title(), "```", parts.get(k, ""), "```", ""]
    md += ["## Last 25 closed trades", "| When | Strategy | Coin | P/L | Exit |", "|---|---|---|---|---|"]
    for c in closed[:25]:
        md.append("| %s | %s | $%s | %+.0f%% ($%.2f) | %s |" % (
            datetime.fromtimestamp(c["closed_at"] / 1000, report.TZ).strftime("%m-%d %H:%M"), c["run_id"] or "main",
            (c["symbol"] or "?").replace("|", "/"), c["pnl_pct"] or 0, c["pnl_usd"] or 0, (c["exit_reason"] or "").replace("|", "/")[:60]))
    return data, "\n".join(md)


async def put(session, path, text, message):
    headers = {"Authorization": "Bearer " + token(), "Accept": "application/vnd.github+json", "User-Agent": "gatekeeper-bot"}
    url = API.format(repo(), path)
    sha = None
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status == 200:
            sha = (await r.json()).get("sha")
        elif r.status not in (404,):
            raise RuntimeError("GitHub %s: %s" % (r.status, (await r.text())[:200]))
    body = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    if sha:
        body["sha"] = sha
    async with session.put(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status not in (200, 201):
            raise RuntimeError("GitHub %s: %s" % (r.status, (await r.text())[:200]))


async def publish(r):
    tt = r.trench.text() if getattr(r, "trench", None) else ""
    data, md = await asyncio.to_thread(lambda: build(r, db.connect(), tt))
    msg = "Stats %s" % data["generated_local"]
    await put(r.session, "latest.md", md, msg)
    await put(r.session, "latest.json", json.dumps(data, indent=1, default=str), msg)
    await put(r.session, "history/%s.md" % datetime.now(report.TZ).strftime("%Y-%m-%d"), md, msg)
    return data["generated_local"]


async def loop(r):
    if not token():
        log.info("No GITHUB_STATS_TOKEN; stats publishing off")
        return
    await asyncio.sleep(180)
    first = True
    while True:
        try:
            when = await publish(r)
            if first:
                from . import notify
                await notify.send(r.session, "📤 Stats now publish every 15 minutes to github.com/%s (latest %s)." % (repo(), when))
                first = False
        except Exception as e:  # noqa: BLE001
            log.warning("Stats publish failed: %s", e)
            if first:
                from . import notify
                await notify.send(r.session, "📤 Couldn't publish stats to GitHub: %s" % str(e)[:200])
                first = False
        await asyncio.sleep(float(config.os.environ.get("GK_STATS_MIN", "15")) * 60)
