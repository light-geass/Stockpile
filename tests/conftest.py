import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import _bootstrap  # noqa: F401, E402
import config  # noqa: E402
from app import create_app  # noqa: E402
from services.watchlists import UNIVERSE_SEED  # noqa: E402
import services.config as config  # noqa: E402
import services.watchlists as watchlists  # noqa: E402
import services.storage as storage  # noqa: E402
import services.security as security  # noqa: E402
import services.notifier as notifier  # noqa: E402
import engine.pipeline as pipeline  # noqa: E402
import engine.scoring as scoring  # noqa: E402
import engine.evidence as evidence  # noqa: E402
import engine.playbook as playbook  # noqa: E402
import engine.sizing as sizing  # noqa: E402
import agents.chart_patterns as chart_patterns  # noqa: E402
import agents.regime as regime  # noqa: E402
import agents.grounding as grounding  # noqa: E402
import agents.llm as llm  # noqa: E402
import agents.predict as predict  # noqa: E402
import agents.outcomes as outcomes  # noqa: E402
import market.data_sources as data_sources  # noqa: E402
import market.fyers_client as fyers_client  # noqa: E402
import market.fyers_auth as fyers_auth  # noqa: E402
import market.instrument_search as instrument_search  # noqa: E402
import market.news as news  # noqa: E402
import market.events as events  # noqa: E402
import market.etf as etf  # noqa: E402
import market.ipo as ipo  # noqa: E402

for _k, _v in (
    ("config", config), ("watchlists", watchlists), ("storage", storage),
    ("security", security), ("notifier", notifier), ("pipeline", pipeline),
    ("scoring", scoring), ("evidence", evidence), ("playbook", playbook),
    ("sizing", sizing), ("chart_patterns", chart_patterns), ("regime", regime),
    ("grounding", grounding), ("llm", llm), ("predict", predict),
    ("outcomes", outcomes), ("data_sources", data_sources),
    ("fyers_client", fyers_client), ("fyers_auth", fyers_auth),
    ("instrument_search", instrument_search), ("news", news),
    ("events", events), ("etf", etf), ("ipo", ipo),
):
    sys.modules[_k] = _v
from app import create_app  # noqa: E402
from services.watchlists import UNIVERSE_SEED  # noqa: E402

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


class FakeWatchlists:
    """In-memory stand-in for the Supabase store: same API and errors, so routes/pipeline/tests never need a DB.
    Starts as the seeded Watchlist 1 (the retired universe) unless items=[]."""

    def __init__(self, items=None, max_items=50):
        from watchlists import WatchlistError
        self._err = WatchlistError
        self.max_items = max_items
        self.lists_ = [{"id": 1, "name": "Watchlist 1", "created_at": "", "updated_at": "", "items": [
            {"provider_symbol": f"NSE:{s}-EQ", "symbol": s, "name": n, "exchange": "NSE", "asset_type": "EQUITY"}
            for _, s, n in UNIVERSE_SEED]}] if items is None else []
        if items is not None:
            self.create("Watchlist 1")
            for i in items:
                self.add_item(1, i)
        self.next_id = 2

    @staticmethod
    def _out(w):
        return {**w, "count": len(w["items"])}

    def _get(self, wid):
        for w in self.lists_:
            if w["id"] == int(wid):
                return w
        raise self._err("not_found", "That watchlist no longer exists.")

    def lists(self):
        return [self._out(w) for w in self.lists_]

    def get(self, wid):
        return self._out(self._get(wid))

    def first_id(self):
        return self.lists_[0]["id"] if self.lists_ else None

    def create(self, name):
        w = {"id": self.next_id, "name": name, "created_at": "", "updated_at": "", "items": []}
        self.next_id += 1
        self.lists_.append(w)
        return self._out(w)

    def rename(self, wid, name):
        w = self._get(wid)
        w["name"] = name
        return self._out(w)

    def delete(self, wid):
        w = self._get(wid)
        if len(self.lists_) <= 1:
            raise self._err("last_list", "Keep at least one watchlist.")
        self.lists_.remove(w)

    def add_item(self, wid, instrument):
        w = self._get(wid)
        if any(i["provider_symbol"] == instrument["provider_symbol"] for i in w["items"]):
            raise self._err("duplicate", "already in this watchlist")
        if len(w["items"]) >= self.max_items:
            raise self._err("too_many", "limit reached")
        w["items"].append({k: instrument.get(k) for k in ("provider_symbol", "symbol", "name", "exchange", "asset_type")})
        return self._out(w)

    def remove_item(self, wid, provider_symbol):
        w = self._get(wid)
        keep = [i for i in w["items"] if i["provider_symbol"] != provider_symbol]
        if len(keep) == len(w["items"]):
            raise self._err("not_found", "That stock is not in the watchlist.")
        w["items"] = keep
        return self._out(w)


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
        deps = {"watchlists": FakeWatchlists(), **(deps or {})}  # tests never touch Supabase
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
