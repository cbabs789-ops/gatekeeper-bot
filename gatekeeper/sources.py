"""Free market data sources.

PumpPortal  - websocket feed of new pump.fun launches and graduations (free methods only)
DexScreener - price, liquidity, volume and buy/sell counts for each pool (free, no key)
RugCheck    - authorities, LP lock, holder concentration, insider wallets (free, no key)
"""
import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("gatekeeper.sources")
UA = {"User-Agent": "gatekeeper-bot/1.0", "Accept": "application/json"}

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{}/report"


def classify_pumpportal(msg):
    """Returns ('create'|'migrate'|None, mint, symbol, name)."""
    if not isinstance(msg, dict):
        return None, None, None, None
    mint = msg.get("mint")
    if not mint:
        return None, None, None, None
    tx = str(msg.get("txType") or "").lower()
    if tx == "create":
        return "create", mint, msg.get("symbol"), msg.get("name")
    if "migrat" in tx or tx == "complete":
        return "migrate", mint, msg.get("symbol"), msg.get("name")
    return None, mint, msg.get("symbol"), msg.get("name")


async def pumpportal_stream(on_event, api_key=""):
    """Runs forever. Calls on_event(kind, mint, symbol, name, raw)."""
    url = PUMPPORTAL_WS + ("?api-key=" + api_key if api_key else "")
    backoff = 2
    unknown_logged = 0
    while True:
        try:
            async with aiohttp.ClientSession(headers=UA) as s:
                async with s.ws_connect(url, heartbeat=30, timeout=20) as ws:
                    await ws.send_str(json.dumps({"method": "subscribeMigration"}))
                    await ws.send_str(json.dumps({"method": "subscribeNewToken"}))
                    log.info("PumpPortal connected")
                    backoff = 2
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        try:
                            data = json.loads(m.data)
                        except ValueError:
                            continue
                        kind, mint, sym, name = classify_pumpportal(data)
                        if kind:
                            await on_event(kind, mint, sym, name, data)
                        elif mint and unknown_logged < 5:
                            unknown_logged += 1
                            log.info("Unrecognized PumpPortal message: %s", str(data)[:300])
        except Exception as e:  # noqa: BLE001
            log.warning("PumpPortal disconnected: %s", e)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


def best_pairs(pairs):
    """DexScreener returns every pool for a token; keep the deepest one per token."""
    best = {}
    for p in pairs or []:
        try:
            mint = p["baseToken"]["address"]
        except (KeyError, TypeError):
            continue
        liq = ((p.get("liquidity") or {}).get("usd")) or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p
    return best


def pair_to_snapshot(mint, p, ts):
    tx = p.get("txns") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    try:
        price = float(p.get("priceUsd") or 0)
    except ValueError:
        price = 0.0
    return {
        "mint": mint, "ts": ts, "price": price,
        "liq": float((p.get("liquidity") or {}).get("usd") or 0),
        "fdv": float(p.get("fdv") or p.get("marketCap") or 0),
        "vol_m5": float(vol.get("m5") or 0), "vol_h1": float(vol.get("h1") or 0),
        "buys_m5": int((tx.get("m5") or {}).get("buys") or 0), "sells_m5": int((tx.get("m5") or {}).get("sells") or 0),
        "buys_h1": int((tx.get("h1") or {}).get("buys") or 0), "sells_h1": int((tx.get("h1") or {}).get("sells") or 0),
        "pc_m5": pc.get("m5"), "pc_h1": pc.get("h1"),
        "_symbol": (p.get("baseToken") or {}).get("symbol"),
        "_name": (p.get("baseToken") or {}).get("name"),
        "_pair": p.get("pairAddress"), "_dex": p.get("dexId"),
    }


async def dexscreener_batch(session, mints):
    """Up to 30 mints per call."""
    url = DEX_TOKENS + ",".join(mints)
    for attempt in range(3):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 429:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                if r.status != 200:
                    log.warning("DexScreener %s", r.status)
                    return {}
                return best_pairs(await r.json(content_type=None))
        except Exception as e:  # noqa: BLE001
            log.warning("DexScreener error: %s", e)
            await asyncio.sleep(2)
    return {}


def parse_rugcheck(rep):
    """Turn a RugCheck report into the handful of numbers the gates use."""
    known = rep.get("knownAccounts") or {}
    amm = {a for a, v in known.items() if isinstance(v, dict) and str(v.get("type", "")).upper() == "AMM"}
    pool_accts = set()
    lp_locked = 0.0
    for m in rep.get("markets") or []:
        for k in ("liquidityA", "liquidityB", "liquidityAAccount", "liquidityBAccount", "pubkey"):
            v = m.get(k)
            if isinstance(v, str):
                pool_accts.add(v)
        lp = m.get("lp") or {}
        try:
            lp_locked = max(lp_locked, float(lp.get("lpLockedPct") or 0))
        except (TypeError, ValueError):
            pass
    top = []
    for h in rep.get("topHolders") or []:
        if h.get("owner") in amm or h.get("address") in amm or h.get("owner") in pool_accts or h.get("address") in pool_accts:
            continue
        try:
            top.append(float(h.get("pct") or 0))
        except (TypeError, ValueError):
            pass
    top10 = round(sum(sorted(top, reverse=True)[:10]), 2) if top else None
    danger = [r.get("name") for r in (rep.get("risks") or [])
              if str(r.get("level")).lower() == "danger" and r.get("name") != "Low Liquidity"]
    return {
        "mint_revoked": 1 if not rep.get("mintAuthority") else 0,
        "freeze_revoked": 1 if not rep.get("freezeAuthority") else 0,
        "lp_locked": round(lp_locked, 2),
        "top10": top10,
        "insiders": rep.get("graphInsidersDetected"),
        "danger": ", ".join(d for d in danger if d) or None,
        "rc_score": rep.get("score_normalised"),
    }


async def rugcheck(session, mint):
    for attempt in range(3):
        try:
            async with session.get(RUGCHECK_REPORT.format(mint), timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status == 429:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                if r.status != 200:
                    log.info("RugCheck %s for %s", r.status, mint)
                    return None
                return parse_rugcheck(await r.json(content_type=None))
        except Exception as e:  # noqa: BLE001
            log.info("RugCheck error for %s: %s", mint, e)
            await asyncio.sleep(3)
    return None
