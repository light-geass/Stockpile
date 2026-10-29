"""Newsdesk: GDELT DOC 2.0 (primary) + yfinance news (supplementary). Headline-only evidence.
Dedupe, company-relevance filter, prompt-injection flagging. Sentiment = transparent headline lexicon heuristic."""
import re
import threading
import time
from datetime import datetime, timezone

import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
POS = {"surge", "surges", "jump", "jumps", "rally", "rallies", "gain", "gains", "rise", "rises", "soar", "soars",
       "beat", "beats", "record", "upgrade", "upgrades", "profit", "growth", "wins", "win", "order", "orders",
       "bullish", "strong", "outperform", "buy", "expansion", "approval", "dividend"}
NEG = {"fall", "falls", "drop", "drops", "plunge", "plunges", "slump", "slumps", "miss", "misses", "loss", "losses",
       "downgrade", "downgrades", "probe", "fraud", "fine", "penalty", "lawsuit", "weak", "bearish", "cut", "cuts",
       "default", "resigns", "resignation", "raid", "ban", "sell", "decline", "declines", "concern", "concerns"}
INJECTION = re.compile(r"(ignore (all |any )?(previous|prior|above)|disregard|system prompt|you are now|"
                       r"assistant:|<\s*/?\s*(system|instruction)|act as|override)", re.I)
_CTRL = re.compile(r"[\x00-\x1f\x7f<>]")


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

    def search(self, query, timespan="3d", maxrecords=25):
        params = {"query": f'"{query}" sourcelang:english', "mode": "artlist", "format": "json",
                  "maxrecords": maxrecords, "timespan": timespan, "sort": "datedesc"}
        if self.blocked:
            return None, "gdelt_rate_limited"
        for attempt in range(2):
            self._pace()
            try:
                r = self.http.get(GDELT_URL, params=params, timeout=20)
            except requests.RequestException:
                return None, "gdelt_unreachable"
            if r.status_code == 429 or "limit requests" in (r.text or "")[:200].lower():
                if attempt == 0:
                    self.sleep(12)
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


def build_news(entry, gdelt, yf_ticker=None, max_recent=8):
    """Return (news_block, gaps). yfinance headlines first (ticker-linked, no extra throttled call);
    GDELT only when fewer than 3 items were found."""
    gaps, items = [], []
    aliases = [entry.get("news_query") or entry.get("name"), entry.get("name")]
    if yf_ticker is not None:
        items += [n for n in yf_news(yf_ticker) if n["title"]]  # already tied to this ticker
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
