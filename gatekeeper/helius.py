"""Helius: reads the Solana blockchain (free plan, 1M credits a month).

Credits are counted per day and capped (GK_HELIUS_DAILY_CREDITS) so the free plan never runs out:
plain RPC calls cost about 1 credit, parsed-transaction calls about 100.
"""
import asyncio
import logging
import time

import aiohttp

from . import config, db

log = logging.getLogger("gatekeeper.helius")
RPC = "https://mainnet.helius-rpc.com/?api-key={}"
PARSE = "https://api.helius.xyz/v0/transactions?api-key={}"
WEBHOOKS = "https://api.helius.xyz/v0/webhooks?api-key={}"
WEBHOOK = "https://api.helius.xyz/v0/webhooks/{}?api-key={}"
SOL_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def key():
    k = config.os.environ.get("HELIUS_API_KEY", "")
    return "" if k.startswith("alch_") else k           # the Alchemy key isn't a Helius key


class Helius:
    def __init__(self, session, con):
        self.s, self.con = session, con

    def _day(self):
        return "helius_credits_" + time.strftime("%Y-%m-%d")

    def used_today(self):
        return int(float(db.kv_get(self.con, self._day(), "0")))

    def _spend(self, n):
        db.kv_set(self.con, self._day(), self.used_today() + n)

    def can_spend(self, n):
        return self.used_today() + n <= int(float(config.os.environ.get("GK_HELIUS_DAILY_CREDITS", "25000")))

    async def _post(self, url, body, cost):
        if not key():
            raise RuntimeError("No Helius key")
        if not self.can_spend(cost):
            raise RuntimeError("Helius daily credit budget used up")
        for attempt in range(4):
            try:
                async with self.s.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    self._spend(cost)
                    data = await r.json(content_type=None)
                    if r.status != 200:
                        raise RuntimeError("Helius %s: %s" % (r.status, str(data)[:200]))
                    return data
            except aiohttp.ClientError as e:
                log.info("Helius error: %s", e)
                await asyncio.sleep(2)
        raise RuntimeError("Helius busy")

    async def rpc(self, method, params):
        data = await self._post(RPC.format(key()), {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, 1)
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:200])
        return data.get("result")

    async def signatures(self, address, before=None, limit=1000):
        opts = {"limit": limit}
        if before:
            opts["before"] = before
        return await self.rpc("getSignaturesForAddress", [address, opts]) or []

    async def parse(self, sigs):
        """Parsed ("enhanced") transactions for up to 100 signatures."""
        out = []
        for i in range(0, len(sigs), 100):
            out += await self._post(PARSE.format(key()), {"transactions": sigs[i:i + 100]}, 100) or []
        return out

    # ---- webhooks: Helius pushes our wallets' trades to the bot as they happen
    async def set_webhook(self, url, addresses):
        if not key() or not addresses:
            return None
        body = {"webhookURL": url, "transactionTypes": ["SWAP"], "accountAddresses": addresses[:100], "webhookType": "enhanced"}
        hid = db.kv_get(self.con, "helius_webhook_id")
        try:
            if hid:
                async with self.s.put(WEBHOOK.format(hid, key()), json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        return hid
            async with self.s.post(WEBHOOKS.format(key()), json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
                data = await r.json(content_type=None)
                if r.status == 200 and data.get("webhookID"):
                    db.kv_set(self.con, "helius_webhook_id", data["webhookID"])
                    return data["webhookID"]
                log.warning("Helius webhook not created (%s): %s", r.status, str(data)[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("Helius webhook error: %s", e)
        return None


def swaps_for(tx, wallets):
    """From one parsed transaction: [(wallet, 'buy'|'sell', mint, sol_amount)] for wallets we care about.
    wallets=None means any fee payer."""
    out = []
    payer = tx.get("feePayer")
    if not payer or (wallets is not None and payer not in wallets):
        return out
    sol = 0.0
    for n in tx.get("nativeTransfers") or []:
        if n.get("fromUserAccount") == payer:
            sol -= (n.get("amount") or 0) / 1e9
        if n.get("toUserAccount") == payer:
            sol += (n.get("amount") or 0) / 1e9
    for t in tx.get("tokenTransfers") or []:
        m = t.get("mint")
        if not m or m in SOL_MINTS:
            continue
        if t.get("toUserAccount") == payer:
            out.append((payer, "buy", m, abs(sol)))
        elif t.get("fromUserAccount") == payer:
            out.append((payer, "sell", m, abs(sol)))
    return out
