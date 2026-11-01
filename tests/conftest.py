import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import watchlists as wl  # noqa: E402
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
    # No database and no universe.json in tests: suites that need watchlists pass FakeWatchlists via deps.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DATABASE_URL", raising=False)
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


def rows(store, sql):
    """Read the audit DB directly (the app itself never exposes SQL)."""
    with store.lock:
        return store.db.execute(sql).fetchall()


class FakeWatchlists:
    """In-memory stand-in for watchlists.WatchlistStore: the same contract, no PostgreSQL.

    It seeds itself exactly like the real store does on a fresh database, so a test sees the retired universe.json
    as "Watchlist 1" — the behaviour a first deploy gets."""

    def __init__(self, seed=True, error=None):
        self.lock = threading.Lock()
        self.by_id, self.order = {}, []
        self.max_items = 100
        self.error = error
        if seed and not error:
            self.ensure_seeded()

    # --- helpers ---
    def _fail(self):
        if self.error:
            raise wl.WatchlistError(self.error, "Watchlist database is unreachable.")

    def _view(self, uid):
        w = self.by_id.get(uid)
        if not w:
            raise wl.WatchlistError("not_found", "That watchlist no longer exists.")
        return {"id": uid, "name": w["name"], "items": [dict(i) for i in w["items"]],
                "created_at": "2026-11-01T00:00:00+00:00", "updated_at": "2026-11-01T00:00:00+00:00"}

    # --- contract ---
    def configured(self):
        return self.error != "not_configured"

    def status(self):
        return {"ready": not self.error, "error": self.error, "instruments": None}

    def add_instrument(self, uid, **fields):
        """Test helper: append an item without going through catalogue validation."""
        self.by_id[uid]["items"].append({**fields, "sector": fields.get("sector"),
                                         "cap_segment": fields.get("cap_segment"),
                                         "position": len(self.by_id[uid]["items"])})
        return self.get(uid)

    def ensure_seeded(self, entries=None, name=wl.UNIVERSE_SEED_NAME):
        self._fail()
        if self.order:
            return 0
        entries = wl.seed_entries() if entries is None else entries
        uid = "list-1"
        self.by_id[uid] = {"name": name, "items": [
            {"provider_symbol": e["provider_symbol"], "symbol": e["symbol"], "name": e["name"],
             "sector": e.get("sector"), "cap_segment": e.get("cap_segment"), "position": n}
            for n, e in enumerate(entries)]}
        self.order.append(uid)
        return len(entries)

    def lists(self):
        self._fail()
        return [{"id": u, "name": self.by_id[u]["name"], "count": len(self.by_id[u]["items"]), "updated_at": None}
                for u in self.order]

    def get(self, uid):
        self._fail()
        return self._view(uid)

    def first_id(self):
        self._fail()
        return self.order[0] if self.order else None

    def create(self, name):
        self._fail()
        name = wl.validate_name(name)
        uid = f"list-{len(self.order) + 1}"
        self.by_id[uid] = {"name": name, "items": []}
        self.order.append(uid)
        return self.get(uid)

    def rename(self, uid, name):
        self._fail()
        self._view(uid)
        self.by_id[uid]["name"] = wl.validate_name(name)
        return self.get(uid)

    def delete(self, uid):
        self._fail()
        if len(self.order) <= 1:
            raise wl.WatchlistError("limit", "Keep at least one watchlist.")
        self._view(uid)
        self.order.remove(uid)
        del self.by_id[uid]
        return True

    def add_item(self, uid, instrument, cap_segment=None):
        self._fail()
        w = self._view(uid)
        item = wl.normalise_instrument(instrument, cap_segment)
        if any(i["provider_symbol"] == item["provider_symbol"] for i in w["items"]):
            raise wl.WatchlistError("duplicate", f"{item['symbol']} is already in this watchlist.")
        if len(w["items"]) >= self.max_items:
            raise wl.WatchlistError("full", "A watchlist holds at most 100 stocks.")
        self.by_id[uid]["items"].append({**item, "position": len(w["items"])})
        return self.get(uid)

    def remove_item(self, uid, provider_symbol):
        self._fail()
        w = self._view(uid)
        before = len(w["items"])
        key = str(provider_symbol or "").strip().upper()
        self.by_id[uid]["items"] = [i for i in w["items"] if i["provider_symbol"] != key]
        if len(self.by_id[uid]["items"]) == before:
            raise wl.WatchlistError("not_found", "That stock is not in this watchlist.")
        return self.get(uid)
