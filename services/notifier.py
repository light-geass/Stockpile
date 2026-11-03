"""Telegram delivery. Exact PDF template, HTML-escaped dynamic fields, per-symbol/day dedupe, secrets never logged."""
import html
import logging
import time

import requests

from security import redact

log = logging.getLogger("notifier")
FOOTER = "— Analysis only. No trade was placed. Not investment advice."


def _inr(x):
    return "n/a" if x is None else f"₹{x:,.2f}"


def format_buy(bundle, result):
    v, p = result["verdict"], bundle["price"]
    chg = p.get("day_change_pct")
    e = html.escape
    return (f"🟢 BUY SIGNAL — {e(bundle['symbol'])} ({e(str(bundle.get('cap_segment') or 'n/a'))} cap)\n\n"
            f"Verdict: BUY | Confidence: {v['confidence']}/10\n\n"
            f"Winner: {e(v['winner'])}\n\n"
            f"Why: {e(v['rationale'])}\n\n"
            f"Key catalyst: {e(v['key_catalyst'])}\n\n"
            f"Live price: {_inr(p.get('live'))} | Day change: {'n/a' if chg is None else f'{chg:+.2f}%'}\n\n"
            f"{FOOTER}")


def format_summary(date_str, engine, buys, shortlisted):
    lines = [f"📊 Daily summary — {html.escape(date_str)} IST", f"Engine: {html.escape(engine)} | Stocks in debate: {shortlisted}"]
    lines.append("BUY signals fired: " + (", ".join(f"{html.escape(s)} ({c}/10)" for s, c in buys) if buys else "none"))
    lines.append("Confidence is an internal conviction score, not a probability of profit.")
    lines.append(FOOTER)
    return "\n".join(lines)


class Telegram:
    def __init__(self, token, chat_id, http=requests, sleep=time.sleep):
        self.token, self.chat_id, self.http, self.sleep = token, chat_id, http, sleep

    def send(self, text):
        """Returns (ok, error_string_or_None). Never raises, never leaks the bot token."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(2):
            try:
                r = self.http.post(url, json=body, timeout=15)
            except requests.RequestException as e:
                return False, f"network error ({type(e).__name__})"
            if r.status_code == 200:
                return True, None
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = float(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2
                self.sleep(min(wait, 10))
                continue
            return False, redact(f"Telegram HTTP {r.status_code}", [self.token])
        return False, "Telegram rate limited"
