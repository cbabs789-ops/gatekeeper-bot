"""Test Follow rules (buy when your Fomo traders buy) against recorded history.

Uses the stored Fomo trade feed for the signals and the recorded price
snapshots for the fills. Each variation decides WHEN to buy (how many
traders, how big, how long to wait); exits use the Strategy engine with
the same fees and slippage as live paper trading. Results are split into
first and second half by entry time, same as /test.
"""
import re
import time

from . import config, db, fomo
from .sources import EVM_RE, SOL_RE
from .strategy import MIN, Strategy, summarize

# signal: list = N+ traders from your list within 30 min; crowd = N+ of anyone within 15 min and $5K+ total
VARIANTS = [
    ("Current Follow: 2+ of your traders", {"sig": "list", "n": 2}, {}),
    ("3+ of your traders", {"sig": "list", "n": 3}, {}),
    ("Any 1 of your traders (copy every buy)", {"sig": "list", "n": 1}, {}),
    ("2+ traders, each bought $1K+", {"sig": "list", "n": 2, "min_usd": 1000}, {}),
    ("2+ traders, wait 10 min then buy", {"sig": "list", "n": 2, "delay": 10}, {}),
    ("2+ traders, wait 30 min then buy", {"sig": "list", "n": 2, "delay": 30}, {}),
    ("Trending: 5+ Fomo traders, $5K+", {"sig": "crowd", "n": 5}, {}),
    ("2+ traders, wide stop (-50%, 50% trail)", {"sig": "list", "n": 2}, {"STOP_LOSS_PCT": 50, "TRAIL_PCT": 50}),
    ("2+ traders, take half at 1.5x", {"sig": "list", "n": 2}, {"TAKE_HALF_X": 1.5}),
    ("2+ traders, sell everything after 1h", {"sig": "list", "n": 2}, {"MAX_HOLD_MIN": 60}),
    ("2+ traders, pool $50K+", {"sig": "list", "n": 2}, {"MIN_LIQ_USD": 50000}),
    ("2+ traders, no safety check (reference only)", {"sig": "list", "n": 2, "no_safety": True}, {}),
    ("2+ traders, zero trading costs (reference only)", {"sig": "list", "n": 2}, {"FEE_PCT": 0, "PENALTY_PCT": 0, "PANIC_PENALTY_PCT": 0}),
]
LIST_WINDOW = 30 * MIN
CROWD_WINDOW = 15 * MIN
MISS_AFTER = 10 * MIN        # no price within 10 min of the signal = couldn't have bought


def norm(addr, chain):
    """Same address the recorder uses, or None if the chain isn't covered."""
    addr = addr or ""
    chain = (chain or "").lower()
    if re.fullmatch(EVM_RE, addr) and chain in ("robinhood", ""):
        return addr.lower()
    if re.fullmatch(SOL_RE, addr) and chain in ("solana", "sol", ""):
        return addr
    return None


def signals(buys, watch, v):
    """buys: {mint: [(ts, trader, usd), ...] sorted}. Returns {mint: signal_ts} (first time the rule fires)."""
    out = {}
    min_usd = v.get("min_usd", 200)
    for mint, rows in buys.items():
        for i, (ts, _, _) in enumerate(rows):
            if v["sig"] == "list":
                who = {t.lower() for (t2, t, u) in rows[:i + 1]
                       if t2 >= ts - LIST_WINDOW and t.lower() in watch and u >= min_usd}
                if len(who) >= v["n"]:
                    out[mint] = ts
                    break
            else:
                win = [(t, u) for (t2, t, u) in rows[:i + 1] if t2 >= ts - CROWD_WINDOW]
                if len({t for t, _ in win}) >= v["n"] and sum(u for _, u in win) >= 5000:
                    out[mint] = ts
                    break
    return out


def run(days=7):
    con = db.connect()
    fomo.ensure_schema(con)
    watch = {h.lower() for h in fomo.watchlist()}
    since = int(time.time() * 1000) - int(days * 86400000)
    coins = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM coins")}
    safety = {r["mint"]: dict(r) for r in con.execute("SELECT * FROM safety")}

    buys = {}
    for r in con.execute("SELECT ts, trader, token_address, chain, usd FROM fomo_events "
                         "WHERE side='buy' AND usd>=200 AND ts>=? ORDER BY ts", (since,)):
        m = norm(r["token_address"], r["chain"])
        if m and m in coins:
            buys.setdefault(m, []).append((r["ts"], r["trader"] or "", r["usd"] or 0))
    if not buys:
        return None

    strats = []
    for label, v, ov in VARIANTS:
        p = config.strategy_params(ov, "follow")
        p["MAX_OPEN"] = 1000
        look = (lambda m: {"mint_revoked": 1, "freeze_revoked": 1, "lp_na": 1}) if v.get("no_safety") else safety.get
        if v.get("no_safety"):
            p["MIN_LIQ_USD"] = 0
        sig = signals(buys, watch, v)
        pending = {m: ts + v.get("delay", 0) * MIN for m, ts in sig.items()}
        strats.append({"label": label, "st": Strategy(p, look), "pending": pending, "signals": len(sig), "missed": 0})

    mints = set()
    for s in strats:
        mints |= set(s["pending"])
    if not mints:
        return {"rows": 0, "coins": len(buys), "from": since, "to": since, "results": []}
    con.execute("CREATE TEMP TABLE IF NOT EXISTS ft_mints(mint TEXT PRIMARY KEY)")
    con.execute("DELETE FROM ft_mints")
    con.executemany("INSERT OR IGNORE INTO ft_mints VALUES(?)", [(m,) for m in mints])
    q = "SELECT * FROM snapshots WHERE ts>=? AND mint IN (SELECT mint FROM ft_mints) ORDER BY ts"
    first_sig = min(ts for s in strats for ts in s["pending"].values())
    lo = hi = None
    n = 0
    last = None
    for r in con.execute(q, (first_sig - 60 * MIN,)):
        snap = dict(r)
        ts, m = snap["ts"], snap["mint"]
        if last is not None and ts != last:
            for s in strats:
                s["st"].on_tick(last)
        last = ts
        lo = ts if lo is None else lo
        hi = ts
        n += 1
        c = coins.get(m)
        if not c:
            continue
        for s in strats:
            st = s["st"]
            st.on_snapshot(snap, c)          # signal mode: never enters on its own
            due = s["pending"].get(m)
            if due is not None and ts >= due:
                del s["pending"][m]
                if ts - due > MISS_AFTER:
                    s["missed"] += 1
                else:
                    st.signal_enter(m, s["label"])
    out = []
    mid = None
    opened = [c.opened_at for s in strats for c in s["st"].closed] + [p.opened_at for s in strats for p in s["st"].positions.values()]
    if opened:
        mid = min(opened) + (max(opened) - min(opened)) // 2
    for s in strats:
        st = s["st"]
        for mint, pos in list(st.positions.items()):
            cs = st.coins[mint]
            st._close(pos, cs, cs.last_ts, cs.last_price, cs.last_liq, "Still open at the end")
        s["missed"] += len(s["pending"])
        first = [c for c in st.closed if mid is not None and c.opened_at < mid]
        second = [c for c in st.closed if mid is not None and c.opened_at >= mid]
        out.append({"label": s["label"], "all": summarize(st.closed), "a": summarize(first), "b": summarize(second),
                    "signals": s["signals"], "missed": s["missed"]})
    return {"rows": n, "coins": len(buys), "from": lo or since, "to": hi or since, "results": out}


def text(res):
    if not res:
        return "No Fomo trades recorded yet. The feed needs to run for a few days first."
    if not res["results"]:
        return "Fomo trades are recorded, but none of the coins have price history yet. Try again in a day or two."
    days = (res["to"] - res["from"]) / 86400000
    L = ["🧪 <b>Follow test</b>: buying when your Fomo traders buy, %.1f days, %d coins with price history" % (days, res["coins"]),
         "Each line: trades · win rate · total profit · first half / second half",
         "A rule is only real if both halves are positive.\n"]
    ranked = sorted(res["results"], key=lambda r: (r["b"].get("total_pnl", 0) if r["b"].get("trades") else -1e9), reverse=True)
    for r in ranked:
        a, b, al = r["a"], r["b"], r["all"]
        if not al.get("trades"):
            L.append("<b>%s</b>: no trades (%d signals, %d blocked or no price)" % (r["label"], r["signals"], r["missed"]))
            continue
        both = a.get("total_pnl", 0) > 0 and b.get("total_pnl", 0) > 0
        L.append("%s<b>%s</b>: %d · %.0f%% · $%s · $%s / $%s" % (
            "✅ " if both else "", r["label"], al["trades"], al["win_rate"], format(int(al["total_pnl"]), ","),
            format(int(a.get("total_pnl", 0)), ","), format(int(b.get("total_pnl", 0)), ",")))
    L.append("\nUnder 20 trades on a line is too few to trust. Each trade is $100 of fake money.")
    L.append("Coins are only priced from the moment the bot started watching them, so the 1-trader and wait lines fill in over the next week.")
    return "\n".join(L)
