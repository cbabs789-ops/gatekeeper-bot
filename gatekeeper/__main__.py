"""Command line: python -m gatekeeper <command>

  run                       start the recorder and live paper trader (the service does this)
  status                    feed health and open paper trades
  report [--days N]         paper trading results
  backtest [--days N] [--split] [--strategy main|wide] [--set NAME=VALUE ...] [--trades]
                            replay recorded history through the rules
  setup-telegram            find your chat and send a test message
  settings                  print every strategy setting and its current value
"""
import argparse
import asyncio
import re
import sys
import time

from . import backtest, config, db, report


def plain(text):
    return re.sub(r"<[^>]+>", "", text)


def cmd_backtest(a):
    con = db.connect()
    lo, hi, n = backtest.data_range(con)
    if not lo:
        print("No recorded data yet. Let the recorder run for a few days first.")
        return
    hours = (hi - lo) / 3600000
    print("Recorded history: %.1f hours across %d coins" % (hours, n))
    overrides = dict(kv.split("=", 1) for kv in (a.set or []))
    since = max(lo, int(time.time() * 1000) - a.days * 86400000) if a.days else lo
    if a.split:
        mid = since + (hi - since) // 2
        for label, s0, s1 in (("FIRST HALF (tune here)", since, mid), ("SECOND HALF (the honest test)", mid, hi + 1)):
            strat, s, _ = backtest.run(con, s0, s1, overrides, a.strategy)
            print("\n== %s ==" % label)
            print("\n".join(report.stats_lines(s)))
        print("\nIf the second half is much worse than the first, the rules are memorizing, not working.")
        return
    strat, s, p = backtest.run(con, since, None, overrides, a.strategy)
    if overrides:
        print("Changed settings: " + ", ".join("%s=%s" % (k.upper(), p[k.upper()]) for k in overrides))
    print("\n".join(report.stats_lines(s)))
    if a.trades:
        print("\nTrades:")
        for c in strat.closed:
            print("  %-12s %+8.2f  %+6.1f%%  %s" % (c.symbol[:12], c.pnl_usd, c.pnl_pct, c.exit_reason))


async def _setup_telegram():
    import aiohttp
    from . import notify
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("TELEGRAM_BOT_TOKEN is missing from %s" % config.ENV_FILE)
        return 1
    async with aiohttp.ClientSession() as s:
        try:
            ups = await notify.get_updates(s, timeout=1)
        except Exception as e:  # noqa: BLE001
            print("Telegram rejected the token: %s\nCheck it in %s" % (e, config.ENV_FILE))
            return 1
        chats = [u["message"]["chat"]["id"] for u in ups if u.get("message")]
        if not chats:
            print("No messages found. Open your bot in Telegram, tap Start, send 'hi', then run: gatekeeper setup-telegram")
            return 1
        chat_id = str(chats[-1])
        text = config.ENV_FILE.read_text() if config.ENV_FILE.exists() else ""
        if "TELEGRAM_CHAT_ID=" in text:
            text = re.sub(r"TELEGRAM_CHAT_ID=.*", "TELEGRAM_CHAT_ID=" + chat_id, text)
        else:
            text = text.rstrip("\n") + "\nTELEGRAM_CHAT_ID=" + chat_id + "\n"
        config.ENV_FILE.write_text(text)
        ok = await notify.send(s, "✅ Gatekeeper is connected. Paper trade alerts will show up here.", chat_id=chat_id)
        print("Telegram connected. Test message sent." if ok else "Found your chat but the test message failed.")
        return 0 if ok else 1


async def _fomo(a):
    import json as _json
    import aiohttp
    from . import fomo
    if not fomo.key():
        print("FOMO_API_KEY is missing. Add it with: gatekeeper config")
        return 1
    con = db.connect()
    fomo.ensure_schema(con)
    async with aiohttp.ClientSession() as s:
        if a.what == "credits":
            print("FOMO API credits used this month: %s of 250,000" % format(fomo.Client(s, con).credits_used(), ","))
        elif a.what == "feed":
            print(plain(fomo.feed_report(con, 24)))
        elif a.what == "raw":
            h = a.handle or "bigbabba"
            c = fomo.Client(s, con)
            for path in ("/v2/users/%s/positions" % h, "/v2/users/%s/following" % h):
                r = await c.get(path)
                print("== %s ==" % path)
                print(_json.dumps(r, indent=1)[:2500])
        else:
            print(plain(fomo.scan_text(await fomo.trend_scan(s, con))))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="gatekeeper")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    sub.add_parser("status")
    r = sub.add_parser("report")
    r.add_argument("--days", type=float, default=0)
    b = sub.add_parser("backtest")
    b.add_argument("--days", type=float, default=0)
    b.add_argument("--split", action="store_true")
    b.add_argument("--set", action="append")
    b.add_argument("--trades", action="store_true")
    b.add_argument("--strategy", default="main", choices=sorted(config.PRESETS))
    sub.add_parser("setup-telegram")
    sub.add_parser("settings")
    fm = sub.add_parser("fomo")
    fm.add_argument("what", choices=["scan", "feed", "raw", "credits"])
    fm.add_argument("handle", nargs="?")
    a = ap.parse_args()

    if a.cmd == "run":
        from .runner import main as run_main
        run_main()
    elif a.cmd == "status":
        print(plain(report.status_text(db.connect())))
    elif a.cmd == "report":
        con = db.connect()
        print(plain(report.period_text(con, hours=int(a.days * 24) if a.days else 24 * 3650)))
    elif a.cmd == "backtest":
        cmd_backtest(a)
    elif a.cmd == "setup-telegram":
        sys.exit(asyncio.run(_setup_telegram()))
    elif a.cmd == "fomo":
        sys.exit(asyncio.run(_fomo(a)))
    elif a.cmd == "settings":
        ps = {n: config.strategy_params(preset=n) for n in config.PRESETS}
        print("%-20s %s" % ("SETTING", "  ".join("%-10s" % n for n in ps)))
        for k in config.STRATEGY_DEFAULTS:
            print("%-20s %s" % (k, "  ".join("%-10s" % ps[n][k] for n in ps)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
