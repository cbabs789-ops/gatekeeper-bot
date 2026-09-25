"""Telegram alerts. Plain HTTPS calls to the Bot API, nothing else."""
import html
import logging

import aiohttp

from . import config

log = logging.getLogger("gatekeeper.notify")
API = "https://api.telegram.org/bot{}/{}"


def money(x):
    sign = "-" if x < 0 else ""
    return "%s$%s" % (sign, format(abs(x), ",.2f"))


def price(x):
    if x >= 1:
        return "$%.4f" % x
    return "$%.10f" % x if x < 0.0001 else "$%.8f" % x


def dex_link(mint):
    return '<a href="https://dexscreener.com/solana/%s">DexScreener</a>' % mint


def fmt_buy(pos, spot, liq, p=None):
    p = p or {"TAKE_HALF_X": 2.0, "STOP_LOSS_PCT": 30, "MAX_HOLD_MIN": 360}
    return ("🟢 <b>PAPER BUY ${sym}</b>\n"
            "Spot {spot} · filled {fill} after costs\n"
            "Size {size} · pool ${liq}\n\n"
            "<b>Why:</b> {why}\n\n"
            "Plan: sell half at {tx:g}x, stop at -{sl:g}%, out in {mh:g}h max.\n{link}").format(
        tx=p["TAKE_HALF_X"], sl=p["STOP_LOSS_PCT"], mh=p["MAX_HOLD_MIN"] / 60,
        sym=html.escape(pos.symbol), spot=price(spot), fill=price(pos.entry_price), size=money(pos.size_usd),
        liq=format(int(liq), ","), why=html.escape(pos.why), link=dex_link(pos.mint))


def fmt_partial(pos, spot, usd):
    return "🟡 <b>Took half on ${}</b> at {} · locked {}\nRest rides with a trailing stop. {}".format(
        html.escape(pos.symbol), price(spot), money(usd), dex_link(pos.mint))


def fmt_close(pos, spot, reason):
    icon = "✅" if pos.pnl_usd > 0 else "🔴"
    mins = int((pos.closed_at - pos.opened_at) / 60000)
    return "{} <b>PAPER SELL ${}</b>: {}\nExit {} · held {}h{:02d}m\nResult <b>{} ({:+.1f}%)</b> on {}\n{}".format(
        icon, html.escape(pos.symbol), html.escape(reason), price(spot), mins // 60, mins % 60,
        money(pos.pnl_usd), pos.pnl_pct, money(pos.size_usd), dex_link(pos.mint))


async def send(session, text, chat_id=None, token=None):
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram not configured; message: %s", text[:120])
        return False
    try:
        async with session.post(API.format(token, "sendMessage"), json={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("Telegram send failed %s: %s", r.status, (await r.text())[:200])
                return False
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram error: %s", e)
        return False


async def get_updates(session, offset=None, timeout=25, token=None):
    token = token or config.TELEGRAM_BOT_TOKEN
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    async with session.get(API.format(token, "getUpdates"), params=params,
                           timeout=aiohttp.ClientTimeout(total=timeout + 10)) as r:
        data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram rejected the token")
        return data.get("result") or []
