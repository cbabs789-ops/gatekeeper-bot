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
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{}/"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{}?contract_addresses={}"
# chains the bot can record and safety-check
CHAINS = {"solana": {"dex": "solana", "goplus": None}, "robinhood": {"dex": "robinhood", "goplus": "4663"}}
EVM_RE = r"0x[0-9a-fA-F]{40}"
SOL_RE = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
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
            if mint.startswith("0x"):
                mint = mint.lower()
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


async def dexscreener_batch(session, mints, chain="solana"):
    """Up to 30 mints per call."""
    url = DEX_TOKENS.format(CHAINS.get(chain, {}).get("dex", chain)) + ",".join(mints)
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
    prev = rep.get("creatorTokens") or []
    if not isinstance(prev, list):
        prev = []
    prev = [t for t in prev if isinstance(t, dict) and t.get("mint") != rep.get("mint")]
    def _mc(t):
        for k in ("marketCap", "market_cap", "mcap", "usdMarketCap"):
            try:
                return float(t.get(k))
            except (TypeError, ValueError):
                continue
        return None
    dead = sum(1 for t in prev if (_mc(t) is not None and _mc(t) < 10000))
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
        "creator": rep.get("creator"),
        "creator_prev": len(prev),
        "creator_dead": dead,
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


def parse_goplus(res):
    """GoPlus token security (EVM chains) -> the same fields the gates use."""
    def yes(k):
        return str(res.get(k, "0")) == "1"
    def f(k):
        try:
            return float(res.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0
    danger = []
    if yes("is_honeypot") or yes("cannot_sell_all"): danger.append("honeypot (can't sell)")
    if f("sell_tax") > 0.10: danger.append("sell tax %.0f%%" % (f("sell_tax") * 100))
    if f("buy_tax") > 0.10: danger.append("buy tax %.0f%%" % (f("buy_tax") * 100))
    if yes("can_take_back_ownership"): danger.append("owner can take back control")
    if yes("slippage_modifiable"): danger.append("tax can be changed")
    if yes("hidden_owner"): danger.append("hidden owner")
    top = []
    for h in res.get("holders") or []:
        if str(h.get("is_contract")) == "1" or str(h.get("is_locked")) == "1" or "dead" in str(h.get("address", "")).lower():
            continue
        top.append(float(h.get("percent") or 0) * 100)
    lp = res.get("lp_holders") or []
    lp_locked = sum(float(h.get("percent") or 0) for h in lp if str(h.get("is_locked")) == "1" or "dead" in str(h.get("address", "")).lower()) * 100
    v4 = any("v4" in str(d.get("name", "")).lower() for d in res.get("dex") or [])
    return {
        "mint_revoked": 0 if yes("is_mintable") else 1,
        "freeze_revoked": 0 if (yes("transfer_pausable") or yes("is_blacklisted")) else 1,
        "lp_locked": round(lp_locked, 2),
        "lp_na": 1 if v4 else 0,              # Uniswap v4 pools don't lock LP the old way
        "top10": round(sum(sorted(top, reverse=True)[:10]), 2) if top else None,
        "insiders": None,
        "danger": ", ".join(danger) or None,
        "rc_score": None,
        "holders": int(f("holder_count")),
    }


async def goplus(session, chain_id, addr):
    for attempt in range(3):
        try:
            async with session.get(GOPLUS.format(chain_id, addr), timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 429:
                    await asyncio.sleep(8 * (attempt + 1))
                    continue
                data = await r.json(content_type=None)
                res = (data.get("result") or {})
                tok = res.get(addr.lower()) or next(iter(res.values()), None)
                if not tok:
                    return None
                return parse_goplus(tok)
        except Exception as e:  # noqa: BLE001
            log.info("GoPlus error for %s: %s", addr, e)
            await asyncio.sleep(3)
    return None


async def safety_check(session, chain, addr):
    """One call for any supported chain."""
    if chain == "solana":
        return await rugcheck(session, addr)
    gp = CHAINS.get(chain, {}).get("goplus")
    return await goplus(session, gp, addr) if gp else None
