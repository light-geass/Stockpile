import requests

from notifier import FOOTER, Telegram, format_buy, format_summary
from storage import Store
from tests.conftest import FakeHTTP, Resp, rows
from tests.test_llm import live_bundle
import llm, config

BUY_TEMPLATE = """🟢 BUY SIGNAL — NWSTL (Large cap)

Verdict: BUY | Confidence: 9/10

Winner: Bull

Why: <RAT>

Key catalyst: <CAT>

Live price: ₹1,842.50 | Day change: +2.93%

— Analysis only. No trade was placed. Not investment advice."""


def result(rat="Volume confirms the breakout.", cat="Strong volume"):
    return {"verdict": {"verdict": "BUY", "confidence": 9, "winner": "Bull", "rationale": rat, "key_catalyst": cat}}


def test_message_matches_template_exactly():
    msg = format_buy(live_bundle(), result())
    assert msg == BUY_TEMPLATE.replace("<RAT>", "Volume confirms the breakout.").replace("<CAT>", "Strong volume")


def test_dynamic_fields_are_html_escaped():
    msg = format_buy(live_bundle(), result("<b>bold</b> & <script>", "x<y"))
    assert "<script>" not in msg and "&lt;script&gt;" in msg and "&amp;" in msg


def test_summary_lists_buys_and_label():
    m = format_summary("2026-10-28", "deterministic", [("NWSTL", 9)], 12)
    assert "NWSTL (9/10)" in m and "not a probability of profit" in m and m.endswith(FOOTER)
    assert "none" in format_summary("2026-10-28", "x", [], 3)


def test_send_ok_failure_and_no_token_leak():
    tok = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    t = Telegram(tok, "42", FakeHTTP([Resp({"ok": True})]))
    assert t.send("hi") == (True, None)
    t = Telegram(tok, "42", FakeHTTP([Resp({}, 400)]))
    ok, err = t.send("hi")
    assert not ok and tok not in err
    t = Telegram(tok, "42", FakeHTTP([requests.ConnectionError(f"failed for https://api.telegram.org/bot{tok}/sendMessage")]))
    ok, err = t.send("hi")
    assert not ok and tok not in err


def test_429_retry_after_respected():
    sleeps = []
    t = Telegram("tok", "1", FakeHTTP([Resp({"parameters": {"retry_after": 3}}, 429), Resp({"ok": True})]), sleeps.append)
    assert t.send("x")[0] and sleeps == [3.0]


def test_dedupe_and_delivery_log(tmp_path):
    st = Store(tmp_path / "a.sqlite3")
    k = Store.dedupe_key("NWSTL", "2026-10-28")
    assert not st.already_sent(k)
    st.log_alert(1, "NWSTL", k, "failed", "boom")
    assert not st.already_sent(k)  # failures do not block a retry
    st.log_alert(1, "NWSTL", k, "sent")
    assert st.already_sent(k) and not st.already_sent(Store.dedupe_key("NWSTL", "2026-10-29"))


def test_audit_rows_are_scrubbed(tmp_path):
    st = Store(tmp_path / "b.sqlite3", ["SECRETXYZ123"])
    b = live_bundle()
    b["note"] = "leaked SECRETXYZ123 here"
    st.add_verdict(1, b, {"verdict": {"verdict": "WATCH", "confidence": 5}, "engine": "deterministic", "validation": {}}, {"eligible": False, "blocked_reasons": []})
    assert "SECRETXYZ123" not in rows(st, "SELECT bundle FROM verdicts")[0][0]
