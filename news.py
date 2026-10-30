"""Newsdesk: yfinance news + Google News RSS (supplementary) + GDELT DOC 2.0 (last resort). Headline-only evidence.
Dedupe, company-relevance filter, prompt-injection flagging. Sentiment = transparent headline lexicon heuristic."""
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS_URL = "https://news.google.com/rss/search"
POS = {"surge", "surges", "jump", "jumps", "rally", "rallies", "gain", "gains", "rise", "rises", "soar", "soars",
       "beat", "beats", "record", "upgrade", "upgrades", "profit", "growth", "wins", "win", "order", "orders",
       "bullish", "strong", "outperform", "buy", "expansion", "approval", "dividend"}
NEG = {"fall", "falls", "drop", "drops", "plunge", "plunges", "slump", "slumps", "miss", "misses", "loss", "losses",
       "downgrade", "downgrades", "probe", "fraud", "fine", "penalty", "lawsuit", "weak", "bearish", "cut", "cuts",
       "default", "resigns", "resignation", "raid", "ban", "sell", "decline", "declines", "concern", "concerns"}
INJECTION = re.compile(r"(ignore (all |any )?(previous|prior|above)|disregard|system prompt|you are now|"
                       r"assistant:|<\s*/?\s*(system|instruction)|act as|override)", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f<>]")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) signal-desk/1.0"


def clean_title(t):
    t = _CTRL.sub(" ", str(t or "")).strip()
    return re.sub(r"\s+", " ", t)[:220]


def _norm(t):
    t = re.sub(r"\s[-|–]\s[^-|–]{2,40}$", "", t.lower())
    return set(re.findall(r"[a-z0-9]+", t))


def sentiment(title):
    w = set(re.findall(r"[a-z]+", title.lower()))
    p, n = len(w & POS), len(w & NEG)
    return "positive" if p > n else "negative" if n > p else "neutral"


def parse_seendate(s):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def relevant(title, aliases):
    tl = title.lower()
    return any(re.search(rf"\b{re.escape(a.lower())}\b", tl) for a in aliases if a)


def dedupe(items):
    out, seen_urls, sets = [], set(), []
    for it in items:
        key = it.get("url")
        ts = _norm(it["title"])
        if (key and key in seen_urls) or not ts:
            continue
        if any(len(ts & s) / max(1, len(ts | s)) >= 0.8 for s in sets):
            continue
        seen_urls.add(key)
        sets.append(ts)
        out.append(it)
    return out


class Gdelt:
    _lock = threading.Lock()
    _last = 0.0

    def __init__(self, min_interval=8.0, http=requests, sleep=time.sleep, clock=time.monotonic):
        self.min_interval, self.http, self.sleep, self.clock = min_interval, http, sleep, clock
        self.blocked = False  # circuit: once rate-limited, skip GDELT for the rest of the run

    def _pace(self):
        with Gdelt._lock:
            wait = self.min_interval - (self.clock() - Gdelt._last)
            if wait > 0:
                self.sleep(wait)
            Gdelt._last = self.clock()

    def search(self, query, timespan="3d", maxrecords=25, retries=2):
        # GDELT only accepts a subset of its parameters in DOC 2.0 (an unknown one yields a 429 page),
        # so the query itself carries the phrase/window: `"ITC" sourcelang:english timespan:3d`.
        params = {"query": f'"{query}" sourcelang:english timespan:{timespan}', "mode": "artlist",
                  "format": "json", "maxrecords": maxrecords}
        if self.blocked:
            return None, "gdelt_rate_limited"
        for attempt in range(retries):
            self._pace()
            try:
                r = self.http.get(GDELT_URL, params=params, timeout=20, headers={"User-Agent": _UA})
            except requests.RequestException:
                return None, "gdelt_unreachable"
            if r.status_code == 429 or "limit requests" in (r.text or "")[:200].lower():
                if attempt < retries - 1:  # GDELT throttles bursts; back off well past its 5 s window
                    self.sleep(self.min_interval * (attempt + 2))
                    continue
                self.blocked = True
                return None, "gdelt_rate_limited"
            try:
                return (r.json().get("articles") or []), None
            except ValueError:
                return [], None  # GDELT returns non-JSON text when there are no matches / bad query
        return None, "gdelt_failed"


def yf_news(ticker_obj):
    out = []
    try:
        for n in (ticker_obj.news or [])[:15]:
            c = n.get("content", n)
            url = ((c.get("canonicalUrl") or {}).get("url")) or c.get("link")
            out.append({"title": clean_title(c.get("title")), "url": url,
                        "domain": ((c.get("provider") or {}).get("displayName")) or c.get("publisher"),
                        "published_at": c.get("pubDate") or None, "source": "yfinance"})
    except Exception:
        pass
    return out


_SUFFIX = re.compile(r"\b(limited|ltd|private|pvt|company|co|corporation|corp|industries|india)\b")


def _match_title(title, keep):
    """Google News relevance. True when a whole name matches, when the company's leading words appear
    ("Tata Steel" in "Tata Steel shares jump"), or when a long ticker appears inside a concatenation
    ("TATASTEEL" in "TATASTEEL Outlook"). One-word/one-letter tokens never match."""
    if relevant(title, keep):
        return True
    tl = title.lower()
    squashed = re.sub(r"[^a-z0-9]", "", tl)
    cores, keys = [], []
    for name in (str(x) for x in keep if x):
        core = " ".join(_SUFFIX.sub(" ", name.lower()).split())
        if len(core) >= 4:
            cores.append(core)
            keys.append(re.sub(r"[^a-z0-9]", "", core))  # "tata steel" -> "tatasteel"
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(key) >= 5:
            keys.append(key)
    return (any(re.search(rf"\b{re.escape(c)}\b", tl) for c in cores)
            or any(k in squashed for k in keys))


def _rss_date(s):
    try:
        return parsedate_to_datetime(s).isoformat()
    except Exception:
        return None


def google_news(query, http=requests, days=7, max_items=15):
    """Google News RSS titles (no API key). Returns (items, error). Yahoo moved its NSE news feed and
    yfinance now returns nothing for `<SYM>.NS`, so this is the dependable alternate source."""
    params = {"q": f"{query} when:{days}d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    try:
        r = http.get(f"{GNEWS_URL}?" + "&".join(f"{k}={quote_plus(v)}" for k, v in params.items()),
                     timeout=15, headers={"User-Agent": _UA})
    except requests.RequestException:
        return [], "gnews_unreachable"
    if getattr(r, "status_code", 0) != 200:
        return [], "gnews_failed"
    try:
        root = ET.fromstring(r.content if isinstance(r.content, bytes) else str(r.text).encode())
    except ET.ParseError:
        return [], "gnews_failed"
    out = []
    for it in root.findall(".//item")[:max_items]:
        title = clean_title(it.findtext("title"))
        if title:
            out.append({"title": title, "url": it.findtext("link"),
                        "domain": clean_title(it.findtext("source"))[:60] or None,
                        "published_at": _rss_date(it.findtext("pubDate") or ""), "source": "google_news"})
    return out, None


def build_news(entry, gdelt, yf_ticker=None, max_recent=8, gnews=None):
    """Return (news_block, gaps). yfinance headlines first (ticker-linked, no extra call); Google News RSS (free,
    unthrottled) when those are thin; GDELT only as a further fallback because it rate-limits hard."""
    gaps, items = [], []
    aliases = [entry.get("news_query") or entry.get("name"), entry.get("name")]
    if yf_ticker is not None:
        items += [n for n in yf_news(yf_ticker) if n["title"]]  # already tied to this ticker
        items = dedupe(items)
    if len(items) < 3 and gnews is not None:
        query = f"{aliases[0]} NSE {entry.get('symbol') or ''}".strip()
        arts, err = gnews(query)
        if err:
            gaps.append(f"news.gnews({err})")
        keep = [a for a in aliases if a] + [str(entry.get("symbol") or "")]
        for a in arts or []:
            # Google News matches loosely, so keep only headlines naming this company or its ticker.
            if _match_title(a["title"], keep):
                items.append(a)
        items = dedupe(items)
    if len(items) < 3 and gdelt is not None:
        arts, err = gdelt.search(aliases[0])
        if err:
            gaps.append(f"news.gdelt({err})")
        for a in arts or []:
            t = clean_title(a.get("title"))
            if t and relevant(t, aliases):
                items.append({"title": t, "url": a.get("url"), "domain": a.get("domain"),
                              "published_at": parse_seendate(a.get("seendate", "")), "source": "gdelt"})
        items = dedupe(items)
    items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    for it in items:
        it["sentiment"] = sentiment(it["title"])
        it["suspicious"] = bool(INJECTION.search(it["title"]))
        it["evidence_type"] = "headline_only"
    pos = sum(i["sentiment"] == "positive" for i in items)
    neg = sum(i["sentiment"] == "negative" for i in items)
    block = {"total": len(items), "positive": pos, "negative": neg, "neutral": len(items) - pos - neg,
             "tone_method": "headline_lexicon_v1 (heuristic; headline-only, not full-text)",
             "recent": items[:max_recent]}
    if not items:
        gaps.append("news.items")
    return block, gaps
