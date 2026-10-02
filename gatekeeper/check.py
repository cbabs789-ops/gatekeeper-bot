"""/check: paste a coin (contract address, Fomo or DexScreener link, or $TICKER) and get the bot's verdict.

Uses everything the bot knows: live price and pool, safety checks, the learned rug-risk score, the win score,
known insider wallets, what your Fomo traders did, and whether Main's rules would buy it right now.
A verdict, not a buy signal.
"""
import html
import logging
import re
import time

import aiohttp

from . import db, fomo, notify, risk, winmodel
from .sources import EVM_RE, SOL_RE, best_pairs, dexscreener_batch, pair_to_snapshot, safety_check
from .strategy import MIN

log = logging.getLogger("gatekeeper.check")
SEARCH = "https://api.dexscreener.com/latest/dex/search?q={}"
PAIR = "https://api.dexscreener.com/latest/dex/pairs/{}/{}"


def find_address(text):
    """(address, chain) from a pasted address or link, else (None, None)."""
    m = re.search(EVM_RE, text or "")
    if m:
        return m.group(0).lower(), "robinhood"
    for cand in re.findall(SOL_RE, text or ""):
        if len(cand) >= 32:
            return cand, "solana"
    return None, None


async def _json(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except Exception as e:  # noqa: BLE001
        log.info("check fetch failed: %s", e)
        return None


async def resolve(session, arg):
    """Find the coin's best pool. Returns (address, chain, pair, note) or (None, None, None, why)."""
    addr, chain = find_address(arg)
    if addr:
        pair = (await dexscreener_batch(session, [addr], chain)).get(addr)
        if not pair and chain == "solana":
            # a DexScreener link carries the pool's address, not the coin's
            data = await _json(session, PAIR.format("solana", addr))
            ps = (data or {}).get("pairs") or ([data["pair"]] if (data or {}).get("pair") else [])
            if ps:
                pair = ps[0]
                addr = (pair.get("baseToken") or {}).get("address") or addr
        if not pair:
            return None, None, None, "I couldn't find a pool for that address on Solana or Robinhood Chain. It may be too new, dead, or on another chain."
        return addr, chain, pair, ""
    sym = (arg or "").strip().lstrip("$").strip()
    if not sym:
        return None, None, None, "Send /check followed by the coin: its contract address, a Fomo or DexScreener link, or its ticker (for example /check $AI)."
    data = await _json(session, SEARCH.format(sym))
    pairs = [p for p in ((data or {}).get("pairs") or []) if (p.get("chainId") or "").lower() in ("solana", "robinhood")
             and ((p.get("baseToken") or {}).get("symbol") or "").lower() == sym.lower()]
    if not pairs:
        return None, None, None, "I couldn't find a coin called $%s on Solana or Robinhood Chain. Paste its contract address to be sure." % html.escape(sym[:20])
    best = best_pairs(pairs)
    addr, pair = max(best.items(), key=lambda kv: (kv[1].get("liquidity") or {}).get("usd") or 0)
    note = ("%d coins use the ticker $%s. This is the one with the biggest pool. Paste the contract address to check a specific one."
            % (len(best), sym.upper())) if len(best) > 1 else ""
    return addr, (pair.get("chainId") or "").lower(), pair, note


def km(x):
    x = float(x or 0)
    return "$%.2fM" % (x / 1e6) if x >= 1e6 else "$%.0fK" % (x / 1e3) if x >= 1e3 else "$%.0f" % x


async def run(r, arg):
    """r: the Runner. Returns the Telegram text."""
    s = r.session
    addr, chain, pair, note = await resolve(s, arg)
    if not addr:
        return "🔎 " + note
    now = int(time.time() * 1000)
    snap = pair_to_snapshot(addr, pair, now)
    sym = html.escape(snap.get("_symbol") or "?")
    created = pair.get("pairCreatedAt") or now
    row_c = r.con.execute("SELECT graduated_at, socials FROM coins WHERE mint=?", (addr,)).fetchone()
    grad = (row_c["graduated_at"] if row_c and row_c["graduated_at"] else created)
    age_min = max(0, (now - grad) / MIN)

    saf = r.safety.get(addr)
    if not saf:
        try:
            saf = await safety_check(s, chain, addr)
            if saf:
                saf.update(mint=addr, checked_at=now)
                r.safety[addr] = saf
                db.save_safety(r.con, saf)
        except Exception:  # noqa: BLE001
            log.exception("safety check for /check")
    p = r.strat.p
    row = risk.live_row(snap, saf, snap.get("_socials"), age_min)
    rug, how = risk.score(r.rug_model, row)
    win = winmodel.score(r.win_model, row) if (r.win_model or {}).get("counts") else None
    cut = (r.win_model or {}).get("cut")
    insiders = r.trench.insiders_in(addr) if getattr(r, "trench", None) else 0

    # what your Fomo traders did with it in the last 24h
    watch = {h.lower() for h in fomo.watchlist()}
    buyers, yours, sellers = set(), set(), set()
    try:
        fomo.ensure_schema(r.con)
        for e in r.con.execute("SELECT trader, side FROM fomo_events WHERE ts>? AND (token_address=? OR lower(token_address)=?)",
                               (now - 86400000, addr, addr.lower())):
            (buyers if e["side"] == "buy" else sellers).add(e["trader"])
            if e["side"] == "buy" and (e["trader"] or "").lower() in watch:
                yours.add(e["trader"])
    except Exception:  # noqa: BLE001
        pass

    # --- the bot's rules, one by one
    bad, warn, good = [], [], []
    if not saf:
        warn.append("safety check unavailable right now (RugCheck didn't answer)")
    else:
        if not saf.get("mint_revoked"): bad.append("mint authority is still on: the dev can print more tokens")
        if not saf.get("freeze_revoked"): bad.append("freeze authority is still on: the dev can freeze your tokens")
        if saf.get("danger"): bad.append("RugCheck danger flag: %s" % html.escape(str(saf["danger"])[:80]))
        if not saf.get("lp_na") and (saf.get("lp_locked") or 0) < p["MIN_LP_LOCKED_PCT"]:
            bad.append("liquidity only %.0f%% locked" % (saf.get("lp_locked") or 0))
        t10 = saf.get("top10")
        if t10 is not None:
            (bad if t10 > 35 else warn if t10 > p["MAX_TOP10_PCT"] else good).append("top 10 holders own %.0f%%" % t10)
        ins = saf.get("insiders")
        if ins is not None and ins > p["MAX_INSIDERS"]:
            warn.append("%d insider-linked holders" % ins)
        if (saf.get("creator_dead") or 0) > 0:
            warn.append("dev has %d dead earlier coins" % saf["creator_dead"])
        if saf.get("mint_revoked") and saf.get("freeze_revoked") and not saf.get("danger"):
            good.append("mint and freeze are off, no danger flag")
    liq = snap["liq"] or 0
    (bad if liq < 10000 else warn if liq < p["MIN_LIQ_USD"] else good).append("pool %s" % km(liq))
    if age_min < 60:
        bad.append("only %.0f min old: young coins lost money in every test" % age_min)
    elif age_min < p["MIN_AGE_MIN"]:
        warn.append("%.0f min old (Main waits for %d+)" % (age_min, p["MIN_AGE_MIN"]))
    else:
        good.append("%.1f hours old" % (age_min / 60) if age_min < 2880 else "%.0f days old" % (age_min / 1440))
    if rug >= p["RISK_SKIP"]:
        bad.append("rug risk %d%% (Main skips %d%%+)" % (rug, p["RISK_SKIP"]))
    elif rug >= 30:
        warn.append("rug risk %d%%" % rug)
    else:
        good.append("rug risk %d%%" % rug)
    if insiders >= 2:
        bad.append("%d known insider wallets bought it" % insiders)
    elif insiders == 1:
        warn.append("1 known insider wallet bought it")
    pc1 = snap.get("pc_h1")
    if pc1 is not None and pc1 >= 200:
        bad.append("up %.0f%% in the last hour: coins that pump this hard usually drain" % pc1)
    b1, s1 = snap.get("buys_h1") or 0, snap.get("sells_h1") or 0
    b5, s5 = snap.get("buys_m5") or 0, snap.get("sells_m5") or 0
    if b1 + s1 >= 10:
        (warn if b1 < s1 else good).append("%d buys vs %d sells in the last hour" % (b1, s1))
    if b5 + s5 < 10:
        warn.append("quiet: only %d trades in 5 min" % (b5 + s5))
    if win is not None and cut is not None:
        (good if win >= cut else warn).append("win score %d of 100 (%s the top-40%% line of %d)" % (win, "above" if win >= cut else "below", cut))

    if bad:
        verdict = "🔴 <b>Skip</b>: fails %d of the bot's checks" % len(bad)
    elif len(warn) >= 2:
        verdict = "🟡 <b>Careful</b>: no hard fails, but %d warnings" % len(warn)
    elif warn:
        verdict = "🟡 <b>Mostly clean</b>: one warning"
    else:
        verdict = "🟢 <b>Passes the bot's checks</b>"

    L = ["🔎 <b>$%s</b> (%s) %s" % (sym, chain.title(), html.escape((snap.get("_name") or "")[:40])), verdict, ""]
    L.append("Market cap %s · pool %s · 1h %s · 5m %s" % (
        km(snap.get("fdv")), km(liq), "%+.0f%%" % pc1 if pc1 is not None else "n/a",
        "%+.0f%%" % snap["pc_m5"] if snap.get("pc_m5") is not None else "n/a"))
    if bad:
        L.append("\n<b>Fails</b>")
        L += ["❌ " + x for x in bad]
    if warn:
        L.append("\n<b>Warnings</b>")
        L += ["⚠️ " + x for x in warn]
    if good:
        L.append("\n<b>Good signs</b>")
        L += ["✅ " + x for x in good]
    if buyers or sellers:
        L.append("\nFomo, last 24h: %d traders bought, %d sold%s" % (
            len(buyers), len(sellers), (" · from your list: " + ", ".join("@" + html.escape(t) for t in sorted(yours)[:5])) if yours else ""))
    # would Main buy it right now? Only answerable for coins the bot has been tracking
    cs = r.strat.coins.get(addr)
    if cs and cs.last_snap:
        ok, _, fails = r.strat.entry_check(cs, cs.last_snap)
        L.append("\n<b>Would Main buy it now?</b> " + ("Yes, it passes every rule." if ok and not bad else
                 "No: " + html.escape("; ".join(fails[:4]) if fails else "see the fails above")))
    else:
        L.append("\n<b>Would Main buy it now?</b> Main only trades coins it has tracked since launch, so it has no price pattern for this one. The checks above still apply.")
    if note:
        L.append("\n" + html.escape(note))
    L.append("\n" + notify.dex_link(addr, chain))
    L.append("A check, not a buy signal. Even clean coins can dump: keep bets small and set your exit first.")
    return "\n".join(L)
