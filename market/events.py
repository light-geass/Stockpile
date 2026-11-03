"""Corporate calendar: yfinance dates first, NSE public pages as a defensive extra. Never invents an event."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from evidence import IST

_UA = {"User-Agent": "Mozilla/5.0 signal-desk/1.0", "Accept": "application/json"}


def _d(v):
    if v is None:
        return None
    if hasattr(v, "date"):
        return v.date() if not hasattr(v, "hour") or v.__class__.__name__ == "date" else v.date()
    if isinstance(v, (int, float)) and v > 10_000:
        return datetime.fromtimestamp(v / (1000 if v > 1e12 else 1), tz=timezone.utc).date()
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(v[:10] if fmt == "%Y-%m-%d" else v, fmt).date()
            except ValueError:
                pass
        try:
            return parsedate_to_datetime(v).date()
        except Exception:
            return None
    return None


def from_yf(ticker):
    items = []
    if ticker is None:
        return items
    try:
        cal = getattr(ticker, "calendar", None) or {}
        if hasattr(cal, "to_dict"):
            cal = {k: (v[0] if isinstance(v, list) and v else v) for k, v in cal.to_dict().items()}
        for key, kind in (("Earnings Date", "results"), ("Ex-Dividend Date", "ex_dividend"),
                          ("Dividend Date", "dividend")):
            d = _d(cal.get(key))
            if d:
                items.append({"kind": kind, "date": d.isoformat(), "title": key, "source": "yfinance"})
    except Exception:
        pass
    try:
        eds = getattr(ticker, "earnings_dates", None)
        if eds is not None and len(eds):
            idx = eds.index[0]
            d = _d(idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx)
            if d and not any(i["kind"] == "results" for i in items):
                items.append({"kind": "results", "date": d.isoformat(), "title": "Earnings Date", "source": "yfinance"})
    except Exception:
        pass
    return items


def from_nse(symbol, http, timeout=12):
    """Best-effort NSE event-calendar JSON. Any failure is a gap, never a fabricated meeting."""
    url = "https://www.nseindia.com/api/event-calendar"
    try:
        http.get("https://www.nseindia.com", timeout=timeout, headers=_UA)
        r = http.get(url, timeout=timeout, headers={**_UA, "Referer": "https://www.nseindia.com/"})
        rows = r.json() if getattr(r, "status_code", 0) == 200 else None
    except Exception:
        return [], "events.nse_unavailable"
    if not isinstance(rows, list):
        return [], "events.nse_unavailable"
    sym, out = (symbol or "").upper(), []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or row.get("sm_name") or "").upper() != sym:
            continue
        d = _d(row.get("date") or row.get("exDate") or row.get("bm_date"))
        if not d:
            continue
        title = str(row.get("purpose") or row.get("subject") or row.get("desc") or "Corporate event")[:180]
        kind = "results" if "result" in title.lower() else "ex_dividend" if "dividend" in title.lower() else "board"
        out.append({"kind": kind, "date": d.isoformat(), "title": title, "source": "nse"})
    return out, None


def collect(symbol, ticker=None, http=None, now=None):
    now = now or datetime.now(IST)
    items, gaps = from_yf(ticker), []
    if http is not None:
        extra, err = from_nse(symbol, http)
        if err:
            gaps.append(err)
        seen = {(i["kind"], i["date"]) for i in items}
        items += [e for e in extra if (e["kind"], e["date"]) not in seen]
    items.sort(key=lambda i: i["date"])
    return items, gaps


def blocks_buy(items, days, now=None, hold=5):
    """True when a scheduled event falls inside [today, today+max(days, hold)]."""
    now = (now or datetime.now(IST)).date()
    window = timedelta(days=max(int(days or 0), 1))
    end = now + window
    for it in items or []:
        d = _d(it.get("date"))
        if d and now <= d <= end:
            return True, it
    return False, None
