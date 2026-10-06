"""Events: speeches, summits, signings and announcements that are scheduled BEFORE they happen,
plus the coins most likely to move when they do.

How it works (free sources only):
  1. Google News searches for scheduled-event headlines ("Trump to host...", "will address...", "summit on...").
  2. Each event gets a date when the headline says one (tonight, tomorrow, Thursday, Oct 2) and topic words
     (AI, crypto, tariffs, space... plus the event's own key names).
  3. DexScreener search finds coins on Solana and Robinhood Chain named after those words; each one is
     safety-checked and rug-scored. Only coins with a real pool are kept.
  4. Telegram alert when an event is found and again on the day, with Fomo links. On the day, the coins are
     recorded as picks by "gatekeeper-events" so the scorecard grades them 1h, 6h and 24h later.
Ideas to watch, not buy signals.
"""
import asyncio
import json
import html
import logging
import re
import time
from datetime import datetime, timedelta

from . import config, db, notify, report
from .sources import best_pairs, safety_check
from .trends import GNEWS, STOP, _get, parse_rss

log = logging.getLogger("gatekeeper.events")
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={}"
QUERIES = ("Trump to speak", "Trump will address", "Trump to host", "Trump to sign", "Trump to announce",
           "Trump speech tomorrow", "Trump rally", "White House summit", "Trump press conference", "Trump to meet",
           "Trump crypto announcement", "address to the nation", "Elon Musk to unveil", "SEC crypto decision")
FUTURE = re.compile(r"\b(to (speak|address|host|sign|announce|meet|unveil|deliver|hold|visit|attend|headline|reveal)|"
                    r"will (speak|address|host|sign|announce|meet|unveil|deliver|hold|visit|attend|reveal)|set to|expected to|"
                    r"scheduled|upcoming|plans to|tonight|tomorrow|this week|next week|later today)\b", re.I)
PAST = re.compile(r"\b(spoke|said|signed|hosted|announced|met with|delivered|unveiled|after|recap|takeaways|what we learned|had the chance|threw|instead|failed to|missed|why|opinion|analysis)\b", re.I)
EVENT_WORDS = set("""speak speaks speech address addresses host hosts summit rally sign signs signing announce announcement
press conference meet meets meeting unveil deliver hold visit attend expected scheduled upcoming tonight tomorrow week
white house plans leaders executive order remarks event events tuesday wednesday thursday friday saturday sunday monday
january february march april june july august september october november december nation""".split())
# themes that reliably have coins named after them; each maps to the words searched on DexScreener
THEMES = [
    (r"\bA\.?I\b|artificial intelligence|chatbot|openai|nvidia", ["AI", "AGI"]),
    (r"crypto|bitcoin|digital asset|stablecoin|blockchain|strategic reserve", ["crypto", "bitcoin", "reserve"]),
    (r"tariff", ["tariff"]),
    (r"\bchina\b|\bxi\b|beijing", ["china"]),
    (r"\bmars\b|spacex|nasa|space force|moon landing|artemis", ["mars", "space"]),
    (r"elon|musk|doge\b|tesla", ["elon", "doge"]),
    (r"golden dome|missile", ["dome"]),
    (r"maga|america first|make america", ["maga", "america"]),
    (r"peace|ceasefire|truce", ["peace"]),
    (r"robot|humanoid|optimus", ["robot"]),
    (r"quantum", ["quantum"]),
    (r"\bfed\b|rate cut|powell", ["fed"]),
]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def when_of(text, pub_ms):
    """(event datetime or None, 'label'). Reads tonight / tomorrow / weekday / 'Oct 2' relative to the article date."""
    pub = datetime.fromtimestamp(pub_ms / 1000, report.TZ)
    t = text.lower()
    if "tonight" in t or "later today" in t:
        return pub.replace(hour=20, minute=0, second=0, microsecond=0), "tonight"
    if "tomorrow" in t:
        return (pub + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0), "day"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})\b", t)
    if m:
        try:
            d = pub.replace(month=MONTHS[m.group(1)], day=int(m.group(2)), hour=12, minute=0, second=0, microsecond=0)
            if d < pub - timedelta(days=2):
                d = d.replace(year=d.year + 1)
            return d, "day"
        except ValueError:
            pass
    for i, wd in enumerate(WEEKDAYS):
        if re.search(r"\b(on |this |next )?%s\b" % wd, t):
            ahead = (i - pub.weekday()) % 7
            return (pub + timedelta(days=ahead)).replace(hour=12, minute=0, second=0, microsecond=0), "day"
    return None, ""


def topic_words(title):
    words = []
    for pat, ws in THEMES:
        if re.search(pat, title, re.I):
            words += ws
    # (the event's own place and people names were tried and matched junk coins, e.g. "center" -> $DATACENTER)
    return list(dict.fromkeys(words))[:6]


def km(x):
    x = float(x or 0)
    return "$%.1fM" % (x / 1e6) if x >= 1e6 else "$%.0fK" % (x / 1e3) if x >= 1e3 else "$%.0f" % x


def name_hit(word, sym, name):
    w = word.lower()
    sym, name = (sym or "").lower(), (name or "").lower()
    if len(w) <= 3:          # short words (AI, AGI) must be the whole ticker or a whole word in the name
        return sym == w or sym == w + "s" or bool(re.search(r"(^|[^a-z])%s([^a-z]|$)" % re.escape(w), name))
    return w in sym or bool(re.search(r"(^|[^a-z])%s" % re.escape(w), name))


class Events:
    def __init__(self, runner):
        self.r = runner
        self.state = {"updated": 0, "events": []}
        try:                       # event key -> 'found' / 'today'. Kept across restarts so an alert is sent once.
            self.alerted = json.loads(db.kv_get(runner.con, "events_alerted") or "{}")
        except Exception:  # noqa: BLE001
            self.alerted = {}
        self.coin_cache = {}       # word -> (ts, cards)

    async def coins_for(self, words):
        s, now = self.r.session, int(time.time() * 1000)
        found = {}
        for w in words:
            hit = self.coin_cache.get(w)
            if hit and now - hit[0] < 30 * 60000:
                pairs = hit[1]
            else:
                data = await _get(s, DEX_SEARCH.format(w), as_json=True) or {}
                pairs = [p for p in (data.get("pairs") or []) if (p.get("chainId") or "").lower() in ("solana", "robinhood")]
                self.coin_cache[w] = (now, pairs)
                await asyncio.sleep(0.5)
            for a, p in best_pairs(pairs).items():
                base = p.get("baseToken") or {}
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if liq < 15000 or len(base.get("symbol") or "") > 12 or not name_hit(w, base.get("symbol"), base.get("name")):
                    continue
                if a not in found or liq > found[a]["liq"]:
                    info, pc = p.get("info") or {}, p.get("priceChange") or {}
                    found[a] = {"address": a, "chain": (p.get("chainId") or "").lower(), "symbol": base.get("symbol") or "?",
                                "name": base.get("name") or "", "image": info.get("imageUrl"), "header": info.get("header"),
                                "mc": p.get("marketCap") or p.get("fdv"), "liq": liq, "vol1h": (p.get("volume") or {}).get("h1"),
                                "vol24": (p.get("volume") or {}).get("h24") or 0, "pc1h": pc.get("h1"), "pc24h": pc.get("h24"),
                                "age_h": round((now - p["pairCreatedAt"]) / 3600000, 1) if p.get("pairCreatedAt") else None,
                                "links": [{"type": x.get("type"), "url": x.get("url")} for x in (info.get("socials") or [])][:3],
                                "word": w}
        # most traded first; the real "AI" coin is the one with the volume, not a copycat
        cards = sorted(found.values(), key=lambda c: -(c["vol24"] + c["liq"]))[:6]
        checked = 0
        for c in cards:
            if c["address"] not in self.r.safety and checked < 4:
                res = await safety_check(s, c["chain"], c["address"])
                checked += 1
                if res:
                    res.update(mint=c["address"], checked_at=now)
                    self.r.safety[c["address"]] = res
                    db.save_safety(self.r.con, res)
                await asyncio.sleep(1.2)
            saf = self.r.safety.get(c["address"])
            fails = self.r.strat.safety_fails(c["address"], c["liq"]) if saf else None
            c["safety"] = None if fails is None else ("pass" if not fails else "; ".join(fails)[:120])
            try:
                c["risk"] = self.r.rug_risk(c["address"])[0]
            except Exception:  # noqa: BLE001
                c["risk"] = None
        good = [c for c in cards if c["safety"] == "pass" and (c["risk"] is None or c["risk"] < 60)]
        return (good or cards)[:4]

    async def collect(self):
        s, now = self.r.session, int(time.time() * 1000)
        items = []
        for q in QUERIES:
            x = await _get(s, GNEWS.format(q.replace(" ", "+")))
            items += parse_rss(x or "", None, 15)
            await asyncio.sleep(1)
        nowdt = datetime.now(report.TZ)
        events, seen = [], set()
        for it in sorted(items, key=lambda i: -i["ts"]):
            title = it["title"]
            if now - it["ts"] > 5 * 86400000 or not FUTURE.search(title) or PAST.search(title):
                continue
            key = re.sub(r"[^a-z ]", "", title.lower())[:70]
            if key in seen:
                continue
            seen.add(key)
            when, kind = when_of(title + " " + it.get("text", "")[:200], it["ts"])
            if when is None:
                if now - it["ts"] > 36 * 3600000:
                    continue                      # undated and not fresh: probably already happened
            elif when < nowdt - timedelta(hours=18) or when > nowdt + timedelta(days=14):
                continue
            words = topic_words(title)
            if not words:
                continue
            events.append({"key": key, "title": title, "link": it["link"], "source": it["source"], "pub": it["ts"],
                           "when": int(when.timestamp() * 1000) if when else None, "kind": kind,
                           "when_text": self._when_text(when, kind, nowdt), "words": words})
        # soonest dated events first, then fresh undated ones; one event per topic
        events.sort(key=lambda e: (e["when"] is None, e["when"] or -e["pub"]))
        out, topics = [], set()
        for e in events:
            tkey = tuple(sorted(e["words"][:2]))
            if tkey in topics:
                continue
            topics.add(tkey)
            out.append(e)
            if len(out) >= 10:
                break
        for e in out:
            e["coins"] = await self.coins_for(e["words"][:4])
        self.state = {"updated": now, "events": out}
        await self.alerts(out, nowdt)

    @staticmethod
    def _when_text(when, kind, nowdt):
        if not when:
            return "date not stated (coming up)"
        d = (when.date() - nowdt.date()).days
        day = "today" if d == 0 else "tomorrow" if d == 1 else "yesterday" if d == -1 else when.strftime("%a %b %-d")
        return day + (" night" if kind == "tonight" else "")

    async def alerts(self, events, nowdt):
        now = int(time.time() * 1000)
        for e in events:
            if not e["coins"]:
                continue
            is_today = e["when"] is not None and datetime.fromtimestamp(e["when"] / 1000, report.TZ).date() == nowdt.date()
            stage = self.alerted.get(e["key"])
            if stage == "today" or (stage == "found" and not is_today):
                continue
            if is_today and nowdt.hour < 7:
                continue                          # no 3 AM pings; the day's alert comes after 7
            head = "🗓️ <b>Event today</b>" if is_today else "🗓️ <b>Upcoming event</b> (%s)" % e["when_text"]
            L = [head + ": " + html.escape(e["title"]), '<a href="%s">Article</a> · topic: %s' % (html.escape(e["link"] or ""), ", ".join(e["words"][:4])),
                 "\n<b>Coins that could move</b> (ideas to watch, not buy signals):"]
            for c in e["coins"]:
                L.append("$%s (%s) · MC %s · pool %s · %s%s\n%s" % (
                    html.escape(c["symbol"]), c["chain"], notify.mcap(1, c["mc"]) if c.get("mc") else "?", km(c["liq"]),
                    "passes safety" if c["safety"] == "pass" else "⚠️ " + html.escape(c["safety"] or "not checked"),
                    " · rug risk %d%%" % c["risk"] if c.get("risk") is not None else "", notify.dex_link(c["address"], c["chain"])))
            L.append("\nThe move usually happens in the minutes after the headline, and news coins often dump right after. Plan your exit before you buy.")
            await notify.send(self.r.session, "\n".join(L))
            self.alerted[e["key"]] = "today" if is_today else "found"
            try:
                db.kv_set(self.r.con, "events_alerted", json.dumps(dict(list(self.alerted.items())[-300:])))
            except Exception:  # noqa: BLE001
                pass
            if is_today:
                # grade these as the bot's own picks so we learn whether event coins actually pay
                for c in e["coins"]:
                    self.r.track_coin(c["address"], c["chain"])
                    dup = self.r.con.execute("SELECT 1 FROM trader_calls WHERE trader='gatekeeper-events' AND token_address=? AND ts>?",
                                             (c["address"], now - 24 * 3600000)).fetchone()
                    if not dup:
                        self.r.con.execute("INSERT INTO trader_calls(trader, token_address, symbol, chain, ts, usd) VALUES('gatekeeper-events',?,?,?,?,0)",
                                           (c["address"], c["symbol"], c["chain"], now))

    def text(self):
        st = self.state
        if not st.get("updated"):
            return "Events are still loading. Try again in a few minutes."
        if not st["events"]:
            return "🗓️ No scheduled events with coins attached right now. Checked every 30 minutes."
        L = ["🗓️ <b>Upcoming events and the coins that could move</b> (ideas, not buy signals)"]
        for e in st["events"]:
            L.append("\n<b>%s</b>: %s" % (e["when_text"], html.escape(e["title"])))
            L.append("topic: " + ", ".join(e["words"][:4]))
            for c in e["coins"]:
                L.append("  $%s (%s) · pool %s%s · %s" % (html.escape(c["symbol"]), c["chain"], km(c["liq"]),
                                                         " · rug risk %d%%" % c["risk"] if c.get("risk") is not None else "",
                                                         '<a href="%s">Fomo</a>' % notify.fomo_url(c["address"], c["chain"])))
            if not e["coins"]:
                L.append("  no coins with a real pool yet")
        return "\n".join(L)

    def plain(self):
        return re.sub(r"<[^>]+>", "", self.text())

    async def loop(self):
        await asyncio.sleep(90)
        every = float(config.os.environ.get("GK_EVENTS_MIN", "30"))
        while True:
            try:
                await self.collect()
            except Exception:  # noqa: BLE001
                log.exception("Events update failed")
            await asyncio.sleep(every * 60)
