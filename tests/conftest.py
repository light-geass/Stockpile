import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from app import create_app  # noqa: E402

TUNNEL = "abc-123.ngrok-free.app"
LOCAL = "http://127.0.0.1:5000"
HTTPS = f"https://{TUNNEL}"


class Resp:
    def __init__(self, data=None, status=200, text="", headers=None, raise_json=False):
        self._d, self.status_code, self.headers, self.raise_json = data, status, headers or {}, raise_json
        self.text = text or (str(data) if data is not None else "")

    def json(self):
        if self.raise_json:
            raise ValueError("no json")
        return self._d


class FakeHTTP:
    """Records calls; returns queued responses (or raises queued exceptions)."""

    def __init__(self, queue=None):
        self.queue, self.calls = list(queue or []), []

    def _next(self, kind, url, kw):
        self.calls.append((kind, url, kw))
        item = self.queue.pop(0) if self.queue else Resp({})
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next("GET", url, kw)

    def post(self, url, **kw):
        return self._next("POST", url, kw)


@pytest.fixture
def mk(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("FYERS_", "GROQ_", "TELEGRAM_", "FLASK_", "DASHBOARD_", "ALLOW_", "MARKET_", "BRAND")):
            monkeypatch.delenv(k)
    monkeypatch.setattr(config, "LOCAL_DIR", tmp_path / ".local")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_DELAY", "0")
    # Point the symbol master at a never-touched local path: tests must not download it. Suites that
    # exercise search pass their own fake catalogue through deps.
    monkeypatch.setenv("INSTRUMENT_CATALOGUE_URL", (tmp_path / "no-symbol-master.csv").as_uri())

    def make(env=None, deps=None):
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        s = config.Settings.from_env()
        return create_app(s, deps), s
    return make


@pytest.fixture
def tunnel_env():
    return {"FYERS_APP_ID": "ABCD1234-100", "FYERS_APP_SECRET": "supersecretvalue99",
            "FYERS_REDIRECT_URI": f"{HTTPS}/auth/fyers/callback", "DASHBOARD_ACCESS_KEY": "open-sesame-key"}


def unlock(client, key="open-sesame-key"):
    return client.post("/unlock", data={"key": key}, base_url=HTTPS)
