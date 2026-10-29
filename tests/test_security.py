import logging
import re

from security import RedactingFilter, redact, scrub_obj
from tests.conftest import HTTPS, LOCAL, TUNNEL, unlock


def csrf_of(c, base):
    return re.search(rb'name="csrf" content="([^"]+)"', c.get("/", base_url=base).data).group(1).decode()


def test_bad_host_rejected(mk):
    app, _ = mk()
    assert app.test_client().get("/", base_url="http://evil.example").status_code == 400


def test_tunnel_requests_are_locked_until_unlock(mk, tunnel_env):
    app, _ = mk(tunnel_env)
    c = app.test_client()
    r = c.get("/", base_url=HTTPS)
    assert r.status_code == 302 and r.headers["Location"].endswith("/unlock")
    for path in ("/api/status", "/api/config"):
        assert c.get(path, base_url=HTTPS).status_code == 401
    assert c.post("/api/start", json={"mode": "live"}, base_url=HTTPS).status_code == 401
    assert c.get("/healthz", base_url=HTTPS).status_code == 200  # only health + callback + unlock are public


def test_wrong_key_stays_locked_and_attempts_are_limited(mk, tunnel_env):
    app, _ = mk(tunnel_env)
    c = app.test_client()
    assert unlock(c, "nope").status_code == 200 and c.get("/api/status", base_url=HTTPS).status_code == 401
    for _ in range(6):
        r = unlock(c, "nope")
    assert b"Too many attempts" in r.data
    assert unlock(c).status_code == 200  # correct key still blocked by the limiter during the window
    assert c.get("/api/status", base_url=HTTPS).status_code == 401


def test_correct_key_unlocks(mk, tunnel_env):
    app, _ = mk(tunnel_env)
    c = app.test_client()
    assert unlock(c).status_code == 302
    assert c.get("/", base_url=HTTPS).status_code == 200


def test_forwarded_headers_on_loopback_host_count_as_public(mk):
    app, _ = mk()
    c = app.test_client()
    assert c.get("/api/status", base_url=LOCAL).status_code == 200
    r = c.get("/api/status", base_url=LOCAL, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401


def test_local_dashboard_works_without_credentials_or_tunnel(mk):
    app, _ = mk()
    c = app.test_client()
    assert c.get("/", base_url=LOCAL).status_code == 200
    cfg = c.get("/api/config", base_url=LOCAL).json
    assert cfg["fyers"]["state"] == "not_configured" and cfg["groq"] is False and cfg["telegram"] is False


def test_csrf_and_origin_enforced(mk):
    app, _ = mk()
    c = app.test_client()
    tok = csrf_of(c, LOCAL)
    assert c.post("/api/start", json={"mode": "live"}, base_url=LOCAL).status_code == 403
    assert c.post("/api/start", json={"mode": "live"}, base_url=LOCAL, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/api/start", json={"mode": "live"}, base_url=LOCAL,
                  headers={"X-CSRF-Token": tok, "Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/start", json={"mode": "bogus"}, base_url=LOCAL, headers={"X-CSRF-Token": tok}).status_code == 400


def test_cookie_secure_only_on_https_tunnel_host(mk, tunnel_env):
    app, _ = mk(tunnel_env)
    r = unlock(app.test_client())
    cookie = r.headers.get("Set-Cookie", "")
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    r2 = app.test_client().get("/", base_url=LOCAL)
    assert "Secure" not in r2.headers.get("Set-Cookie", "") and "HttpOnly" in r2.headers.get("Set-Cookie", "")


def test_security_headers_and_debug_off(mk):
    app, _ = mk()
    r = app.test_client().get("/", base_url=LOCAL)
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"] and "nonce-" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Cache-Control"] == "no-store"
    assert app.debug is False and logging.getLogger("werkzeug").level >= logging.WARNING


def test_secrets_and_codes_are_redacted():
    line = "GET /auth/fyers/callback?s=ok&code=200&auth_code=AUTHCODE123&state=STATE456 HTTP/1.1"
    out = redact(line)
    assert "AUTHCODE123" not in out and "STATE456" not in out
    assert "abcdefghijklmnop1234" not in redact("key gsk_abcdefghijklmnop1234")
    assert "123456789:" not in redact("https://api.telegram.org/bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/send")
    assert redact("my secret is hunter2value", ["hunter2value"]) == "my secret is <redacted>"
    assert scrub_obj({"access_token": "abc", "ok": "x", "n": {"api_key": "k"}}) == {"access_token": "<redacted>", "ok": "x", "n": {"api_key": "<redacted>"}}
    rec = logging.LogRecord("t", logging.INFO, "f", 1, "token=%s", ("SECRETVAL99",), None)
    RedactingFilter().filter(rec)
    assert "SECRETVAL99" not in rec.getMessage()
