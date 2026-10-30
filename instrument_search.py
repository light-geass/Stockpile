"""Instrument catalogue: searchable, cached list of FYERS NSE cash-market instruments.

Source: FYERS public symbol master CSV (`.../sym_details/NSE_CM.csv`, headerless).
The file is downloaded at most once per `ttl` seconds by a background refresh; every keystroke is
served from the in-memory index. If a refresh fails the last valid catalogue keeps serving (and is
flagged stale) — a search never triggers a live provider call.

Column layout of the master (0-based), as reported by the FYERS developer community:
  1 name · 5 ISIN · 9 provider symbol (NSE:SYMBOL-EQ) · 13 short symbol · 15 strike (-1 = cash)
"""
import csv
import io
import logging
import re
import threading
import time

import requests

log = logging.getLogger("instruments")

NSE_CM_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
MIN_QUERY = 2
MAX_QUERY = 40
MAX_RESULTS = 8
_EQ = re.compile(r"^NSE:([A-Z0-9&\-]+)-EQ$")
_STRIKE = 15  # field index that is "-1" for cash instruments and a price for derivatives


def _is_cash_strike(raw):
    """FYERS writes the cash marker as "-1" or "-1.0" (it changed to the latter); derivatives carry a real price."""
    try:
        return float(_clean(raw, 20)) == -1.0
    except ValueError:
        return False


class CatalogueError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _clean(s, limit=120):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:limit]


def parse_master(text):
    """Parse raw NSE_CM.csv text into canonical instruments. Malformed rows are skipped, not guessed."""
    if not text or not text.strip():
        raise CatalogueError("invalid", "Symbol master was empty.")
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) <= _STRIKE:
            continue
        symbol = _clean(row[9], 60).upper()
        m = _EQ.match(symbol)
        if not m or not _is_cash_strike(row[_STRIKE]):
            continue  # derivatives, debt, indices or odd series — not analysable cash equity
        short = _clean(row[13], 40).upper() or m.group(1)
        name = _clean(row[1]) or short
        out.append({"symbol": short, "name": name, "exchange": "NSE", "asset_type": "EQUITY",
                    "provider": "fyers", "provider_symbol": symbol, "currency": "INR",
                    "isin": _clean(row[5], 20) or None})
    if not out:
        raise CatalogueError("invalid", "Symbol master contained no usable NSE cash instruments.")
    return out


def rank(instruments, q, limit=MAX_RESULTS):
    """Deterministic ranking: exact symbol, symbol prefix, short-symbol prefix, name prefix, substring."""
    q = _clean(q).upper()
    hits = []
    for it in instruments:
        sym, short, name = it["symbol"], it["symbol"], it["name"].upper()
        if sym == q:
            score = 0
        elif sym.startswith(q):
            score = 1
        elif short.startswith(q):
            score = 2
        elif name.startswith(q):
            score = 3
        elif q in sym or q in name:
            score = 4
        else:
            continue
        hits.append((score, len(sym), sym, it))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    return [h[3] for h in hits[:limit]]


class InstrumentCatalogue:
    """Thread-safe in-memory catalogue with TTL refresh and a last-known-good fallback."""

    def __init__(self, url=NSE_CM_URL, ttl=6 * 3600, http=requests, timeout=30,
                 clock=time.monotonic, records=None):
        self.url, self.ttl, self.http, self.timeout = url, ttl, http, timeout
        self.clock = clock
        self.lock = threading.Lock()
        self.records, self.by_symbol = [], {}
        self.loaded_at, self.error = None, None
        if records is not None:
            self._install(list(records), self.clock())

    # ---- internals ----
    def _install(self, records, at):
        self.records = records
        self.by_symbol = {r["provider_symbol"]: r for r in records}
        self.loaded_at, self.error = at, None

    def _stale(self):
        return self.loaded_at is None or (self.clock() - self.loaded_at) > self.ttl

    def _download(self):
        try:
            r = self.http.get(self.url, timeout=self.timeout, headers={"User-Agent": "signal-desk/1.0"})
        except requests.RequestException:
            raise CatalogueError("unavailable", "Symbol master could not be downloaded.")
        if getattr(r, "status_code", 0) != 200:
            raise CatalogueError("unavailable", f"Symbol master returned HTTP {getattr(r, 'status_code', '?')}.")
        return parse_master(getattr(r, "text", ""))

    # ---- public ----
    def refresh(self, force=False):
        """Download + parse if the cache is missing or older than ttl. Keeps last-known-good on failure."""
        with self.lock:
            if not force and not self._stale():
                return self
            try:
                self._install(self._download(), self.clock())
            except CatalogueError as e:
                self.error = e.kind
                if not self.records:
                    raise
                log.warning("instrument catalogue refresh failed (%s); serving previous copy", e.kind)
            return self

    def ensure_loaded(self, force=False):
        """Background warm-up: download at boot/most once per ttl, never raise. Search stays lazy if this fails."""
        try:
            self.refresh(force=force)
        except CatalogueError as e:
            log.warning("instrument catalogue warm-up failed (%s); searches will retry", e.kind)
        return self

    def ready(self):
        return bool(self.records)

    def status(self):
        return {"loaded": bool(self.loaded_at), "instruments": len(self.records),
                "stale": self._stale() if self.loaded_at is not None else None,
                "error": self.error}

    def search(self, q, limit=MAX_RESULTS):
        q = str(q or "")
        if len(q.strip()) < MIN_QUERY:
            return []
        self.refresh()
        with self.lock:
            return rank(self.records, q, limit)

    def get(self, provider_symbol):
        """Resolve a canonical instrument from a provider symbol; downloads the master once if empty."""
        if not isinstance(provider_symbol, str) or not _EQ.match(provider_symbol.strip().upper()):
            return None
        key = provider_symbol.strip().upper()
        if not self.records:
            try:
                self.refresh()
            except CatalogueError:
                return None
        with self.lock:
            return self.by_symbol.get(key)

    def get_if_fresh(self, provider_symbol):
        """Canonical lookup for a run start: never refreshes, so a start cannot hang on a download."""
        if not isinstance(provider_symbol, str):
            return None
        with self.lock:
            return self.by_symbol.get(provider_symbol.strip().upper())
