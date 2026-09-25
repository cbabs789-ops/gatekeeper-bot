"""Replay recorded market history through the same strategy the live bot runs."""
import time

from . import config
from .strategy import Strategy, summarize


def run(con, since_ms, until_ms=None, overrides=None):
    p = config.strategy_params(overrides)
    until_ms = until_ms or int(time.time() * 1000)
    safety = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM safety")}
    coins = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM coins")}
    strat = Strategy(p, lambda m: safety.get(m))
    last_tick = None
    cur = con.execute("SELECT * FROM snapshots WHERE ts>=? AND ts<? ORDER BY ts", (since_ms, until_ms))
    for r in cur:
        s = dict(r)
        if last_tick is not None and s["ts"] != last_tick:
            strat.on_tick(last_tick)
        last_tick = s["ts"]
        c = coins.get(s["mint"])
        if c:
            strat.on_snapshot(s, c)
    # anything still open at the end is valued at its last price
    for mint, pos in list(strat.positions.items()):
        cs = strat.coins[mint]
        strat._close(pos, cs, cs.last_ts, cs.last_price, cs.last_liq, "Still open when the replay ended")
    return strat, summarize(strat.closed), p


def data_range(con):
    r = con.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(DISTINCT mint) n FROM snapshots").fetchone()
    return r["a"], r["b"], r["n"]
