"""Trends: real-world news, Trump's posts, what Fomo traders are piling into, and new coin profiles,
matched together so a headline can be tied to the coins riding it.

Free sources only:
  - Trump's Truth Social posts via trumpstruth.org's public RSS feed
  - Google News RSS (top stories + crypto / meme-coin searches)
  - DexScreener token profiles and boosts (pictures, descriptions, links)
  - the bot's own Fomo feed and safety / rug-risk data
"""
import asyncio
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime

import aiohttp

from . import config, db, fomo
from .sources import best_pairs

log = logging.getLogger("gatekeeper.trends")
UA = {"User-Agent": "Mozilla/5.0 (gatekeeper-bot trends)"}
TRUMP_FEED = "https://www.trumpstruth.org/feed"
GNEWS = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
GNEWS_TOP = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
NEWS_QUERIES = ("memecoin", "meme coin solana", "crypto Trump", "pump.fun", "viral trend")
# The wider news desk on the site: everything that can move the coins the bots trade. Refreshed every 15 minutes.
# (section, why it matters, Google News searches, direct RSS feeds)
NEWS_SECTIONS = [
    ("Meme coins and Solana", "What the coins we trade are riding, and anything that changes how Solana or pump.fun work.",
     ("memecoin", "meme coin solana", "pump.fun", "solana network", "Robinhood chain crypto", "dexscreener trending"), ()),
    ("Crypto market", "When Bitcoin and the big coins fall, meme coins fall harder. A rough market day is a bad day to buy.",
     ("bitcoin price today", "crypto market today", "ethereum price"),
     ("https://cointelegraph.com/rss", "https://www.coindesk.com/arc/outboundfeeds/rss/", "https://decrypt.co/feed")),
    ("Trump, Musk and politics", "One post from either can launch or kill a coin within minutes.",
     ("Trump crypto", "Elon Musk crypto", "Trump executive order", "White House crypto"), ()),
    ("Rules and regulators", "SEC, Congress and court decisions move the whole market, and can shut down platforms we rely on.",
     ("SEC crypto", "crypto regulation", "stablecoin bill", "CFTC crypto"), ()),
    ("Economy and rates", "Interest rates, inflation and the stock market decide whether people have money to gamble with.",
     ("Federal Reserve interest rates", "CPI inflation report", "stock market today"), ()),
    ("Hacks, rugs and scams", "Exploits and rug pulls scare buyers off and show which tricks scammers are using right now.",
     ("crypto hack exploit", "rug pull crypto", "crypto scam"), ()),
    ("Listings and exchanges", "A coin listed on Binance, Coinbase or Robinhood can jump; an exchange problem can freeze trading.",
     ("Binance listing", "Coinbase listing", "Robinhood crypto listing"), ()),
    ("AI and tech", "AI is the biggest meme coin theme; big AI news often spawns new coins within hours.",
     ("AI agents crypto", "OpenAI", "Nvidia"), ()),
    ("Viral and pop culture", "Viral moments, celebrities and memes are what new coins get named after.",
     ("viral trend", "TikTok trend", "celebrity crypto"), ()),
]
NEWS_EVERY_MIN = 15
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKENS = "https://api.dexscreener.com/tokens/v1/{}/{}"
MIN = 60000

STOP = set("""the a an and or but of to in on for with at by from as is are was were be been it its this that these those
his her their our your my we you they he she i me him them us not no yes all any more most very just than then so if
into over after before about up down out new says said say will would can could may might should also who what when
where why how which while amid says get gets got make makes made one two three year years day days week today news
live update updates report reports video watch first last big top best news world us usa u.s. trump's donald president
crypto coin coins token tokens meme memecoin memecoins price prices market markets solana bitcoin ethereum""".split())


def _text(el, tag):
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def _ts(s):
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000)


def parse_rss(xml_text, source=None, limit=30):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = html.unescape(re.sub(r"<[^>]+>", "", _text(it, "title")))
        desc = html.unescape(re.sub(r"<[^>]+>", " ", _text(it, "description")))
        src = it.find("source")
        img = None
        for child in it:
            if child.tag.endswith("content") or child.tag.endswith("thumbnail") or child.tag == "enclosure":
                img = child.attrib.get("url") or img
        out.append({"title": title[:300], "text": re.sub(r"\s+", " ", desc).strip()[:500], "link": _text(it, "link"),
                    "ts": _ts(_text(it, "pubDate")), "source": (src.text if src is not None and src.text else source) or "",
                    "image": img})
        if len(out) >= limit:
            break
    return out


async def _get(session, url, as_json=False):
    try:
        async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None) if as_json else await r.text()
    except Exception as e:  # noqa: BLE001
        log.info("trends fetch %s: %s", url[:60], e)
        return None


def hot_words(items, now, hours=12, top=25):
    """Words showing up across recent headlines and posts: the narratives of the moment."""
    c = Counter()
    for it in items:
        if now - it["ts"] > hours * 3600 * 1000:
            continue
        words = set(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9']{3,}", it["title"] + " " + it.get("text", "")[:200]))
        c.update(w.strip("'") for w in words if w not in STOP)
    return [w for w, n in c.most_common(top) if n >= 2]


def matches(name, symbol, words):
    txt = ("%s %s" % (name or "", symbol or "")).lower()
    return [w for w in words if len(w) >= 4 and re.search(r"(^|[^a-z])%s" % re.escape(w), txt)]


class Trends:
    def __init__(self, runner):
        self.r = runner
        self.state = {"updated": 0, "trump": [], "news": [], "hot": [], "profiles": [], "matches": [], "suggestions": [], "words": []}
        self.seen_trump = set()
        self.sections, self.sections_ts = [], 0
        try:                       # kept across restarts so a pick isn't re-sent after every update
            from . import db as _db
            self.alerted = {k: int(v) for k, v in json.loads(_db.kv_get(runner.con, "trends_alerted") or "{}").items()}
        except Exception:  # noqa: BLE001
            self.alerted = {}

    # ------------------------------------------------------------------ news desk
    async def collect_sections(self):
        """Every section's latest headlines, newest first, no repeats across sections."""
        s = self.r.session
        now = int(time.time() * 1000)
        out, seen = [], set()
        for name, why, queries, feeds in NEWS_SECTIONS:
            items = []
            for q in queries:
                x = await _get(s, GNEWS.format(q.replace(" ", "+")))
                items += parse_rss(x or "", None, 12)
                await asyncio.sleep(1)
            for url in feeds:
                x = await _get(s, url)
                items += parse_rss(x or "", url.split("/")[2].replace("www.", ""), 15)
                await asyncio.sleep(1)
            keep = []
            for n in sorted(items, key=lambda n: -n["ts"]):
                # Google adds " - Outlet" to titles, so the same story from two outlets looks different without this
                k = re.sub(r"\W+", " ", n["title"].rsplit(" - ", 1)[0].lower()).strip()[:70]
                if not n["title"] or k in seen or now - n["ts"] > 48 * 3600 * 1000:
                    continue
                seen.add(k)
                keep.append({"title": n["title"], "link": n["link"], "source": n["source"], "ts": n["ts"]})
                if len(keep) >= 12:
                    break
            out.append({"name": name, "why": why, "items": keep})
        if any(sec["items"] for sec in out):     # a failed round keeps the last good one
            self.sections, self.sections_ts = out, now

    # ------------------------------------------------------------------ collect
    async def collect(self):
        s = self.r.session
        now = int(time.time() * 1000)
        trump_xml = await _get(s, TRUMP_FEED)
        trump = parse_rss(trump_xml or "", "Truth Social", 20)
        news = []
        top = await _get(s, GNEWS_TOP)
        news += [dict(n, topic="Top stories") for n in parse_rss(top or "", None, 20)]
        for q in NEWS_QUERIES:
            x = await _get(s, GNEWS.format(q.replace(" ", "+")))
            news += [dict(n, topic=q) for n in parse_rss(x or "", None, 10)]
            await asyncio.sleep(1)
        seen, dedup = set(), []
        for n in sorted(news, key=lambda n: -n["ts"]):
            k = n["title"][:80].lower()
            if k not in seen:
                seen.add(k)
                dedup.append(n)
        news = dedup[:60]
        words = hot_words(trump + news, now)
        # Trump's newest posts count double: his words move coins directly
        tw = hot_words(trump, now, hours=6, top=15)
        words = list(dict.fromkeys([w for w in tw] + words))[:30]

        profiles = await _get(s, DEX_PROFILES, as_json=True) or []
        boosts = await _get(s, DEX_BOOSTS, as_json=True) or []
        prof = {}
        for p in (profiles if isinstance(profiles, list) else []) + (boosts if isinstance(boosts, list) else []):
            ch = (p.get("chainId") or "").lower()
            if ch not in ("solana", "robinhood") or not p.get("tokenAddress"):
                continue
            a = p["tokenAddress"].lower() if p["tokenAddress"].startswith("0x") else p["tokenAddress"]
            q = prof.setdefault(a, {"address": a, "chain": ch, "icon": None, "header": None, "description": "", "links": [], "boost": 0})
            q["icon"] = q["icon"] or p.get("icon")
            q["header"] = q["header"] or p.get("header")
            q["description"] = q["description"] or (p.get("description") or "")[:300]
            q["links"] = q["links"] or [{"type": l.get("type") or l.get("label") or "link", "url": l.get("url")} for l in (p.get("links") or []) if l.get("url")][:4]
            q["boost"] = max(q["boost"], p.get("totalAmount") or p.get("amount") or 0)

        # what Fomo traders piled into over the last 3 hours (impossible trade sizes already filtered)
        cap = float(config.os.environ.get("GK_FOMO_MAX_TRADE_USD", "250000"))
        hot = defaultdict(lambda: {"buyers": set(), "buy": 0.0, "sell": 0.0, "symbol": "", "chain": "", "watch": set()})
        watch = {h.lower() for h in fomo.watchlist()}
        for r in self.r.con.execute("SELECT trader, side, token, token_address, chain, usd FROM fomo_events WHERE ts>? AND usd<=?",
                                    (now - 3 * 3600 * 1000, cap)):
            a, ch = fomo.covered(r["token_address"], r["chain"])
            if not a:
                continue
            h = hot[a]
            h["symbol"], h["chain"] = r["token"], ch
            h[r["side"]] += r["usd"] or 0
            if r["side"] == "buy":
                h["buyers"].add(r["trader"])
                if (r["trader"] or "").lower() in watch:
                    h["watch"].add(r["trader"])
        hot_list = sorted(hot.items(), key=lambda kv: len(kv[1]["buyers"]), reverse=True)[:20]

        # safety checks for the top candidates the bot hasn't checked yet (RugCheck / GoPlus, free)
        from .sources import safety_check
        todo = [(a, v["chain"]) for a, v in hot_list[:10] if a not in self.r.safety]
        todo += [(a, v["chain"]) for a, v in list(prof.items())[:10] if a not in self.r.safety]
        for a, ch in todo[:12]:
            res = await safety_check(s, ch, a)
            if res:
                res.update(mint=a, checked_at=now)
                self.r.safety[a] = res
                db.save_safety(self.r.con, res)
            await asyncio.sleep(1.5)

        # price data (and pictures) for everything we might show
        want = {a: v["chain"] for a, v in hot_list}
        want.update({a: v["chain"] for a, v in list(prof.items())[:40]})
        pairs = {}
        for ch in ("solana", "robinhood"):
            addrs = [a for a, c in want.items() if c == ch]
            for i in range(0, len(addrs), 30):
                data = await _get(s, DEX_TOKENS.format(ch, ",".join(addrs[i:i + 30])), as_json=True)
                pairs.update(best_pairs(data or []))
                await asyncio.sleep(1)

        def card(a, ch, extra=None):
            p = pairs.get(a) or {}
            info = p.get("info") or {}
            base = p.get("baseToken") or {}
            pc = p.get("priceChange") or {}
            pr = prof.get(a) or {}
            risk_score = None
            try:
                risk_score = self.r.rug_risk(a)[0]
            except Exception:  # noqa: BLE001
                pass
            saf = self.r.safety.get(a)
            fails = self.r.strat.safety_fails(a, float((p.get("liquidity") or {}).get("usd") or 0)) if saf else None
            c = {"address": a, "chain": ch, "symbol": base.get("symbol") or (extra or {}).get("symbol") or "?",
                 "name": base.get("name") or "", "image": info.get("imageUrl") or pr.get("icon"), "header": info.get("header") or pr.get("header"),
                 "description": pr.get("description") or "", "mc": p.get("marketCap") or p.get("fdv"),
                 "liq": (p.get("liquidity") or {}).get("usd"), "vol1h": (p.get("volume") or {}).get("h1"),
                 "pc1h": pc.get("h1"), "pc24h": pc.get("h24"), "age_h": round((time.time() * 1000 - p["pairCreatedAt"]) / 3600000, 1) if p.get("pairCreatedAt") else None,
                 "links": pr.get("links") or [{"type": x.get("type"), "url": x.get("url")} for x in (info.get("socials") or [])][:3] + [{"type": "website", "url": w.get("url")} for w in (info.get("websites") or [])][:1],
                 "boosted": bool(pr.get("boost")) or bool((p.get("boosts") or {}).get("active")), "risk": risk_score,
                 "safety": None if fails is None else ("pass" if not fails else "; ".join(fails)[:120])}
            if extra:
                c.update(extra)
            return c

        hot_cards = []
        for a, v in hot_list:
            hot_cards.append(card(a, v["chain"], {"symbol": v["symbol"], "buyers": len(v["buyers"]), "net": round(v["buy"] - v["sell"]),
                                                  "your_traders": sorted(v["watch"])[:4]}))
        prof_cards = [card(a, v["chain"]) for a, v in list(prof.items())[:24]]

        # narratives: coins whose name matches a word that's hot in the news or Trump's posts
        match_cards = []
        for c in hot_cards + prof_cards:
            m = matches(c["name"], c["symbol"], words)
            if m and not any(x["address"] == c["address"] for x in match_cards):
                why = [it for it in trump + news if any(w in (it["title"] + " " + it.get("text", "")).lower() for w in m)][:2]
                match_cards.append(dict(c, words=m, headlines=[{"title": h["title"], "link": h["link"], "source": h["source"]} for h in why]))

        # suggestions: worth a look, not a buy signal
        sugg = []
        for c in {x["address"]: x for x in hot_cards + match_cards + prof_cards}.values():
            reasons, score = [], 0
            if c.get("safety") != "pass":
                continue
            if c.get("risk") is not None and c["risk"] >= 50:
                continue
            if (c.get("pc1h") or 0) > 200 or (c.get("liq") or 0) < 15000:
                continue
            if c.get("buyers", 0) >= 3:
                score += min(c["buyers"], 15)
                reasons.append("%d Fomo traders bought in 3h" % c["buyers"])
            if c.get("your_traders"):
                score += 5
                reasons.append("your traders: " + ", ".join("@" + t for t in c["your_traders"]))
            if c.get("words"):
                score += 6
                reasons.append("matches the news: " + ", ".join(c["words"][:3]))
            if c.get("boosted"):
                score += 3
                reasons.append("paid DexScreener boost")
            if (c.get("pc1h") or 0) > 0:
                score += 2
                reasons.append("up %.0f%% in 1h" % c["pc1h"])
            if c.get("risk") is not None:
                reasons.append("rug risk %d%%" % c["risk"])
            if score >= 6:
                sugg.append(dict(c, score=score, reasons=reasons))
        sugg.sort(key=lambda c: -c["score"])
        sugg = sugg[:6]

        # tie each headline on the news desk to coins being traded right now whose name it mentions
        coin_words = [(c["symbol"], c.get("words") or []) for c in match_cards]
        sections = []
        for sec in self.sections:
            items = []
            for n in sec["items"]:
                t = n["title"].lower()
                hit = [sym for sym, ws in coin_words if any(re.search(r"\b%s\b" % re.escape(w), t) for w in ws)][:3]
                items.append(dict(n, coins=hit))
            sections.append(dict(sec, items=items))
        self.state = {"updated": now, "trump": trump[:10], "news": news[:30], "hot": hot_cards[:12], "profiles": prof_cards[:18],
                      "matches": match_cards[:10], "suggestions": sugg, "words": words[:20],
                      "sections": sections, "sections_updated": self.sections_ts}
        await self.alerts(trump, sugg, match_cards)
        # track suggested coins so the scorecard can grade the bot's own picks
        for c in sugg:
            self.r.track_coin(c["address"], c["chain"])
            self._record_pick(c, now)

    def news_text(self, per=3):
        """Plain summary for the stats feed: the newest few headlines in each section."""
        if not self.sections:
            return "News desk not loaded yet (first load a few minutes after the bot starts, then every %d minutes)." % NEWS_EVERY_MIN
        L = []
        for sec in self.sections:
            L.append("%s:" % sec["name"])
            L += ["  %s (%s)" % (n["title"][:140], n["source"] or "?") for n in sec["items"][:per]] or ["  nothing in the last 48h"]
        return "\n".join(L)

    def _record_pick(self, c, now):
        fomo.ensure_schema(self.r.con)
        dup = self.r.con.execute("SELECT 1 FROM trader_calls WHERE trader='gatekeeper-picks' AND token_address=? AND ts>?",
                                 (c["address"], now - 12 * 3600 * 1000)).fetchone()
        if not dup:
            self.r.con.execute("INSERT INTO trader_calls(trader, token_address, symbol, chain, ts, usd) VALUES('gatekeeper-picks',?,?,?,?,0)",
                               (c["address"], c["symbol"], c["chain"], now))

    async def alerts(self, trump, sugg, match_cards):
        from . import notify
        now = int(time.time() * 1000)
        first = not self.seen_trump
        for t in trump[:5]:
            k = t["link"] or t["title"]
            if k in self.seen_trump:
                continue
            self.seen_trump.add(k)
            if first or now - t["ts"] > 3 * 3600 * 1000:
                continue                  # don't replay old posts on startup
            body = (t["title"] if t["title"] and not t["title"].startswith("http") else t["text"])[:400]
            coins = [m for m in match_cards if any(w in body.lower() for w in m.get("words", []))]
            msg = "📣 <b>Trump posted</b>: %s\n<a href=\"%s\">Open post</a>" % (html.escape(body), html.escape(t["link"] or ""))
            if coins:
                msg += "\nCoins matching it: " + ", ".join("$%s (%s)" % (html.escape(c["symbol"]), c["chain"]) for c in coins[:4])
            await notify.send(self.r.session, msg)
        for c in sugg[:3]:
            if now - self.alerted.get(c["address"], 0) < 12 * 3600 * 1000 or c["score"] < 10:
                continue
            self.alerted[c["address"]] = now
            try:
                from . import db as _db
                _db.kv_set(self.r.con, "trends_alerted", json.dumps(dict(list(self.alerted.items())[-300:])))
            except Exception:  # noqa: BLE001
                pass
            mc = notify.mcap(1, c["mc"]) if c.get("mc") else "?"
            await notify.send(self.r.session, "📰 <b>Trend pick: $%s</b> at %s\n%s\n%s\nWorth a look, not a buy signal. Check the chart." % (
                html.escape(c["symbol"]), mc, html.escape(" · ".join(c["reasons"])), notify.dex_link(c["address"], c["chain"])))

    async def loop(self):
        await asyncio.sleep(30)
        every = int(float(config.os.environ.get("GK_TRENDS_MIN", "2")))
        while True:
            if time.time() * 1000 - self.sections_ts > NEWS_EVERY_MIN * MIN:
                try:
                    await self.collect_sections()
                except Exception:  # noqa: BLE001
                    log.exception("News desk update failed")
            try:
                await self.collect()
            except Exception:  # noqa: BLE001
                log.exception("Trends update failed")
            await asyncio.sleep(every * 60)
