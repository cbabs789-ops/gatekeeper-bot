"""Telegram alerts. Plain HTTPS calls to the Bot API, nothing else."""
import contextvars
import html
import logging
import re

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


def dex_link(mint, chain=None):
    chain = chain or ("robinhood" if str(mint).startswith("0x") else "solana")
    return '<a href="%s">👉 Open in Fomo</a> · <a href="https://dexscreener.com/%s/%s">Chart</a>' % (fomo_url(mint, chain), chain, mint)


def fomo_url(mint, chain=None):
    """The coin's page in the Fomo app (opens the app on a phone that has it)."""
    chain = chain or ("robinhood" if str(mint).startswith("0x") else "solana")
    return "https://fomo.family/tokens/%s/%s" % (chain, mint)


def mcap(spot, supply):
    """Market cap at a given price, e.g. '$760K MC' (blank if supply unknown)."""
    if not supply or not spot:
        return ""
    v = spot * supply
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return "$%s%s MC" % (("%.2f" % (v / div)).rstrip("0").rstrip("."), suf)
    return "$%.0f MC" % v


def fmt_buy(pos, spot, liq, p=None, supply=None):
    p = p or {"TAKE_HALF_X": 2.0, "STOP_LOSS_PCT": 30, "MAX_HOLD_MIN": 360}
    mc = mcap(spot, supply)
    return ("🟢 <b>PAPER BUY ${sym}</b>" + (" at <b>%s</b>" % mc if mc else "") + "\n"
            "Spot {spot} · filled {fill} after costs\n"
            "Size {size} · pool ${liq}\n\n"
            "<b>Why:</b> {why}\n\n"
            "Plan: sell half at {tx:g}x, stop at -{sl:g}%, out in {mh:g}h max.\n{link}").format(
        tx=p["TAKE_HALF_X"], sl=p["STOP_LOSS_PCT"], mh=p["MAX_HOLD_MIN"] / 60,
        sym=html.escape(pos.symbol), spot=price(spot), fill=price(pos.entry_price), size=money(pos.size_usd),
        liq=format(int(liq), ","), why=html.escape(pos.why), link=dex_link(pos.mint))


def fmt_partial(pos, spot, usd, why=None, supply=None):
    at = mcap(spot, supply) or price(spot)
    if why and "moonbag" in why:
        return "🌙 <b>Took profit on ${}</b> at {} · locked {}\n{}. The moonbag rides until it falls far from its high. {}".format(
            html.escape(pos.symbol), at, money(usd), html.escape(why), dex_link(pos.mint))
    return "🟡 <b>Took half on ${}</b> at {} · locked {}\nRest rides with a trailing stop. {}".format(
        html.escape(pos.symbol), at, money(usd), dex_link(pos.mint))


def fmt_close(pos, spot, reason, supply=None):
    icon = "✅" if pos.pnl_usd > 0 else "🔴"
    mins = int((pos.closed_at - pos.opened_at) / 60000)
    ex = ("%s → %s" % (mcap(pos.spot_at_entry, supply), mcap(spot, supply))) if supply else price(spot)
    return "{} <b>PAPER SELL ${}</b>: {}\nBought → sold: {} · held {}h{:02d}m\nResult <b>{} ({:+.1f}%)</b> on {}\n{}".format(
        icon, html.escape(pos.symbol), html.escape(reason), ex, mins // 60, mins % 60,
        money(pos.pnl_usd), pos.pnl_pct, money(pos.size_usd), dex_link(pos.mint))


def _chunks(text, limit=3900):
    """Telegram caps messages at 4096 characters; split on line breaks."""
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line[:limit]
    return out + [cur] if cur else out


# ---- Telegram group with Topics: each kind of message goes to its own topic (set up with /setup in the group)
TOPICS = [("trades", "📈 Paper trades"), ("fomo", "🔵 Fomo alerts"), ("news", "📣 Trump & news"), ("events", "🗓️ Events"),
          ("trench", "⛏️ Trench wallets"), ("reports", "📊 Reports & tests")]
ROUTES = {"chat": None, "threads": {}}          # filled from the database at startup
REPLY = contextvars.ContextVar("reply_to", default=None)   # (chat, thread) a command came from: its answer goes back there


def classify(text):
    t = re.sub(r"^\[[A-Z]+\] ", "", (text or "").lstrip())
    if t.startswith(("🟢", "🟡", "🌙", "✅", "🔴", "⚠️")) or "PAPER " in t[:40]:
        return "trades"
    if t.startswith(("🔵", "🔥", "🟠")):
        return "fomo"
    if t.startswith(("📣", "📰")):
        return "news"
    if t.startswith("🗓") and "event" in t[:40].lower():
        return "events"
    if t.startswith("⛏"):
        return "trench"
    return "reports"


async def send(session, text, chat_id=None, token=None):
    parts = _chunks(text)
    if len(parts) > 1:
        ok = True
        for part in parts:
            ok = await _send_one(session, part, chat_id, token) and ok
        return ok
    return await _send_one(session, text, chat_id, token)


async def _send_one(session, text, chat_id=None, token=None):
    token = token or config.TELEGRAM_BOT_TOKEN
    thread = None
    if chat_id is None:
        if REPLY.get():
            chat_id, thread = REPLY.get()
        elif ROUTES["chat"]:
            chat_id, thread = ROUTES["chat"], ROUTES["threads"].get(classify(text))
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.info("Telegram not configured; message: %s", text[:120])
        return False
    try:
        body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if thread:
            body["message_thread_id"] = thread
        async with session.post(API.format(token, "sendMessage"), json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning("Telegram send failed %s: %s", r.status, (await r.text())[:200])
                return False
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram error: %s", e)
        return False


async def call(session, method, body, token=None):
    token = token or config.TELEGRAM_BOT_TOKEN
    async with session.post(API.format(token, method), json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "Telegram error")
        return data.get("result")


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
