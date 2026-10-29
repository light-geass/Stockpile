"""FYERS OAuth (individual app) per https://fyers.in/api-docs/v3/api/authentication/individual-app/
Flow: GET /api/v3/generate-authcode -> browser login -> redirect with auth_code+state ->
POST /api/v3/validate-authcode {grant_type, appIdHash=sha256(app_id:app_secret), code} -> access_token.
Tokens expire daily 06:30 IST. Token is stored server-side only, in a gitignored 0600 file."""
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from security import redact

AUTH_BASE = "https://api-t1.fyers.in/api/v3"
IST = timezone(timedelta(hours=5, minutes=30))
EXPIRED_CODES = {-8, -15, -16, -17}  # per FYERS docs: expired/invalid token


class AuthError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def next_expiry_ist(obtained: datetime) -> datetime:
    t = obtained.astimezone(IST)
    exp = t.replace(hour=6, minute=30, second=0, microsecond=0)
    return exp if exp > t else exp + timedelta(days=1)


def jwt_exp(token):
    """Best-effort read of the 'exp' claim (no signature check; used only for status display)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


class TokenStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return None

    def save(self, info):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(info, fh)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class FyersAuth:
    def __init__(self, settings, store, http=requests):
        self.s = settings
        self.store = store
        self.http = http
        self._runtime_expired = False
        self._unavailable = False

    # ---- login URL ----
    def build_login_url(self, state):
        q = urlencode({"client_id": self.s.fyers_app_id, "redirect_uri": self.s.fyers_redirect_uri,
                       "response_type": "code", "state": state})
        return f"{AUTH_BASE}/generate-authcode?{q}"

    def app_id_hash(self):
        return hashlib.sha256(f"{self.s.fyers_app_id}:{self.s.fyers_app_secret}".encode()).hexdigest()

    # ---- code exchange (server side) ----
    def exchange_code(self, auth_code):
        if not self.s.fyers_configured:
            raise AuthError("not_configured", "FYERS_APP_ID / FYERS_APP_SECRET are not set.")
        body = {"grant_type": "authorization_code", "appIdHash": self.app_id_hash(), "code": auth_code}
        try:
            r = self.http.post(f"{AUTH_BASE}/validate-authcode", json=body, timeout=15)
            data = r.json()
        except requests.RequestException:
            self._unavailable = True
            raise AuthError("unavailable", "Could not reach FYERS to exchange the code.")
        except ValueError:
            raise AuthError("exchange_failed", "FYERS returned an unreadable response.")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token or data.get("s") != "ok":
            msg = redact((data or {}).get("message", "")) if isinstance(data, dict) else ""
            raise AuthError("exchange_failed", f"FYERS refused the code exchange. {msg}".strip())
        now = datetime.now(timezone.utc)
        self.store.save({"access_token": token, "app_id": self.s.fyers_app_id,
                         "obtained_at": now.isoformat(), "expires_at": next_expiry_ist(now).isoformat()})
        self._runtime_expired = False
        self._unavailable = False
        return True

    # ---- token access ----
    def _current(self):
        info = self.store.load()
        if info and info.get("access_token") and info.get("app_id") == self.s.fyers_app_id:
            exp = datetime.fromisoformat(info["expires_at"]).timestamp()
            return info["access_token"], exp, "oauth"
        if self.s.fyers_access_token:
            return self.s.fyers_access_token, jwt_exp(self.s.fyers_access_token), "env"
        return None, None, None

    def get_token(self):
        tok, exp, _ = self._current()
        if not tok or self._runtime_expired or (exp is not None and time.time() >= exp):
            return None
        return tok

    def mark_expired(self):
        self._runtime_expired = True

    def mark_unavailable(self, flag=True):
        self._unavailable = flag

    def logout(self):
        self.store.clear()
        self._runtime_expired = False

    def status(self):
        """Safe to expose: no secrets. state in not_configured|disconnected|connected|expired|unavailable."""
        tok, exp, origin = self._current()
        if not self.s.fyers_configured and not tok:
            return {"state": "not_configured", "label": "Not configured",
                    "detail": "Add FYERS_APP_ID and FYERS_APP_SECRET to .env."}
        if not tok:
            return {"state": "disconnected", "label": "Not connected", "detail": "Click Connect FYERS."}
        expired = self._runtime_expired or (exp is not None and time.time() >= exp)
        if expired:
            return {"state": "expired", "label": "Expired",
                    "detail": "FYERS tokens expire daily at ~06:30 IST. Reconnect."}
        if self._unavailable:
            return {"state": "unavailable", "label": "Unavailable", "detail": "FYERS could not be reached."}
        d = {"state": "connected", "label": "Connected", "detail": f"Token source: {origin}."}
        if exp:
            d["expires_at"] = datetime.fromtimestamp(exp, IST).strftime("%Y-%m-%d %H:%M IST")
        return d
