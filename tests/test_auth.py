"""Mocked FYERS authentication tests. NO live FYERS call is made anywhere in this suite."""
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

import config
from fyers_auth import next_expiry_ist
from tests.conftest import HTTPS, LOCAL, FakeHTTP, Resp, unlock


def start_login(c):
    unlock(c)
    r = c.get("/auth/fyers/login", base_url=HTTPS)
    return r, parse_qs(urlparse(r.headers["Location"]).query)["state"][0] if r.status_code == 302 else None


def test_login_not_configured(mk):
    app, _ = mk()
    r = app.test_client().get("/auth/fyers/login", base_url=LOCAL)
    assert r.status_code == 400 and b"FYERS not configured" in r.data


def test_login_from_wrong_host_does_not_start_flow(mk, tunnel_env):
    app, _ = mk(tunnel_env)
    r = app.test_client().get("/auth/fyers/login", base_url=LOCAL)
    assert r.status_code == 400 and b"abc-123.ngrok-free.app" in r.data and "Location" not in r.headers


def test_login_redirect_url_follows_official_flow(mk, tunnel_env):
    app, s = mk(tunnel_env)
    r, state = start_login(app.test_client())
    loc = urlparse(r.headers["Location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://api-t1.fyers.in/api/v3/generate-authcode"
    assert q["client_id"] == ["ABCD1234-100"] and q["response_type"] == ["code"]
    assert q["redirect_uri"] == [s.fyers_redirect_uri] and len(state) >= 32
    assert "supersecretvalue99" not in r.headers["Location"]


def test_callback_invalid_state_never_exchanges(mk, tunnel_env):
    http = FakeHTTP()
    app, _ = mk(tunnel_env, {"http": http})
    c = app.test_client()
    unlock(c)
    r = c.get("/auth/fyers/callback?s=ok&code=200&auth_code=ABC&state=forged", base_url=HTTPS)
    assert r.headers["Location"].endswith("auth=invalid_state") and http.calls == []
    r = app.test_client().get("/auth/fyers/callback?auth_code=ABC&state=x", base_url=HTTPS)  # no session at all
    assert r.headers["Location"].endswith("auth=invalid_state") and http.calls == []


def test_callback_success_stores_token_securely(mk, tunnel_env):
    http = FakeHTTP([Resp({"s": "ok", "code": 200, "access_token": "TOKEN-VALUE-XYZ-123456"})])
    app, s = mk(tunnel_env, {"http": http})
    c = app.test_client()
    _, state = start_login(c)
    r = c.get(f"/auth/fyers/callback?s=ok&code=200&auth_code=THE-AUTH-CODE&state={state}", base_url=HTTPS)
    assert r.headers["Location"].endswith("auth=ok")
    kind, url, kw = http.calls[0]
    assert url == "https://api-t1.fyers.in/api/v3/validate-authcode"
    assert kw["json"] == {"grant_type": "authorization_code", "code": "THE-AUTH-CODE",
                          "appIdHash": hashlib.sha256(b"ABCD1234-100:supersecretvalue99").hexdigest()}
    f = config.LOCAL_DIR / "fyers_token.json"
    assert json.loads(f.read_text())["access_token"] == "TOKEN-VALUE-XYZ-123456"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    cfg = c.get("/api/config", base_url=HTTPS)
    assert cfg.json["fyers"]["state"] == "connected"
    blob = cfg.get_data(as_text=True) + c.get("/api/status", base_url=HTTPS).get_data(as_text=True) + c.get("/", base_url=HTTPS).get_data(as_text=True)
    for secret in ("TOKEN-VALUE-XYZ-123456", "supersecretvalue99", "THE-AUTH-CODE", s.flask_secret_key):
        assert secret not in blob


def test_state_is_single_use(mk, tunnel_env):
    http = FakeHTTP([Resp({"s": "ok", "access_token": "t" * 20})])
    app, _ = mk(tunnel_env, {"http": http})
    c = app.test_client()
    _, state = start_login(c)
    url = f"/auth/fyers/callback?auth_code=C&state={state}"
    assert c.get(url, base_url=HTTPS).headers["Location"].endswith("auth=ok")
    assert c.get(url, base_url=HTTPS).headers["Location"].endswith("auth=invalid_state") and len(http.calls) == 1


def test_denied_login_and_missing_code(mk, tunnel_env):
    http = FakeHTTP()
    app, _ = mk(tunnel_env, {"http": http})
    c = app.test_client()
    _, st = start_login(c)
    assert c.get(f"/auth/fyers/callback?s=error&state={st}", base_url=HTTPS).headers["Location"].endswith("auth=denied")
    _, st = start_login(c)
    # FYERS also sends code=200 (a status), which must NOT be mistaken for the auth code
    assert c.get(f"/auth/fyers/callback?s=ok&code=200&state={st}", base_url=HTTPS).headers["Location"].endswith("auth=missing")
    assert http.calls == []


def test_exchange_failures_are_graceful(mk, tunnel_env):
    http = FakeHTTP([Resp({"s": "error", "code": -413, "message": "invalid code"}), requests.ConnectionError("boom"),
                     Resp(None, raise_json=True)])
    app, _ = mk(tunnel_env, {"http": http})
    c = app.test_client()
    for expect in ("failed", "unavailable", "failed"):
        _, st = start_login(c)
        assert c.get(f"/auth/fyers/callback?auth_code=C&state={st}", base_url=HTTPS).headers["Location"].endswith(f"auth={expect}")
    assert not (config.LOCAL_DIR / "fyers_token.json").exists()


def test_status_not_configured_disconnected_expired(mk, tunnel_env):
    app, _ = mk()
    assert app.extensions["auth"].status()["state"] == "not_configured"
    app, s = mk(tunnel_env)
    auth = app.extensions["auth"]
    assert auth.status()["state"] == "disconnected"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    auth.store.save({"access_token": "tok", "app_id": s.fyers_app_id, "obtained_at": past, "expires_at": past})
    assert auth.status()["state"] == "expired" and auth.get_token() is None
    fresh = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    auth.store.save({"access_token": "tok", "app_id": s.fyers_app_id, "obtained_at": past, "expires_at": fresh})
    assert auth.status()["state"] == "connected"
    auth.mark_expired()
    assert auth.status()["state"] == "expired"


def test_token_from_env_is_used(mk, tunnel_env):
    app, _ = mk({**tunnel_env, "FYERS_ACCESS_TOKEN": "env-token-123456"})
    assert app.extensions["auth"].get_token() == "env-token-123456"


def test_expiry_is_next_0630_ist():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert next_expiry_ist(datetime(2026, 10, 28, 9, 0, tzinfo=ist)) == datetime(2026, 10, 29, 6, 30, tzinfo=ist)
    assert next_expiry_ist(datetime(2026, 10, 28, 5, 0, tzinfo=ist)) == datetime(2026, 10, 28, 6, 30, tzinfo=ist)
