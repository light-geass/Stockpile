"""Settings loader. Tiny .env parser (no dependency). Missing values never crash startup."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
LOCAL_DIR = BASE_DIR / ".local"
DATA_DIR = BASE_DIR / "data"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def load_env(path=None, override=False):
    p = Path(path) if path else BASE_DIR / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if override or k not in os.environ:
            os.environ[k] = v


def _b(name, default=False):
    v = os.environ.get(name, "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


def _i(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _f(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _s(name, default=""):
    return os.environ.get(name, "").strip() or default


def local_secret(name, env_value=""):
    """Return env value, else a random secret persisted (0600) in the gitignored .local dir."""
    if env_value:
        return env_value
    LOCAL_DIR.mkdir(exist_ok=True)
    f = LOCAL_DIR / name
    if f.exists():
        val = f.read_text().strip()
        if val:
            return val
    val = secrets.token_urlsafe(48)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(val)
    return val


@dataclass
class Settings:
    fyers_app_id: str
    fyers_app_secret: str
    fyers_redirect_uri: str
    fyers_access_token: str
    market_data_provider: str
    flask_secret_key: str
    dashboard_access_key: str
    groq_api_key: str
    groq_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_enabled: bool
    brand: str
    confidence_threshold: int
    agent_delay: float
    shortlist_per_bucket: int
    host: str
    port: int
    quote_max_age_sec: int
    alert_require_open_market: bool
    fyers_per_minute_cap: int
    fyers_daily_cap: int
    gdelt_min_interval: float
    groq_max_attempts: int
    groq_timeout: float
    instrument_catalogue_url: str
    instrument_catalogue_ttl: int
    data_dir: Path

    @classmethod
    def from_env(cls):
        return cls(
            fyers_app_id=_s("FYERS_APP_ID"),
            fyers_app_secret=_s("FYERS_APP_SECRET"),
            fyers_redirect_uri=_s("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/auth/fyers/callback"),
            fyers_access_token=_s("FYERS_ACCESS_TOKEN"),
            market_data_provider=_s("MARKET_DATA_PROVIDER", "fyers").lower(),
            flask_secret_key=local_secret("flask_secret", _s("FLASK_SECRET_KEY")),
            dashboard_access_key=local_secret("dashboard_access_key", _s("DASHBOARD_ACCESS_KEY")),
            groq_api_key=_s("GROQ_API_KEY"),
            groq_model=_s("GROQ_MODEL", "openai/gpt-oss-120b"),
            telegram_bot_token=_s("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_s("TELEGRAM_CHAT_ID"),
            telegram_enabled=_b("TELEGRAM_ENABLED", False),
            brand=_s("BRAND", "Signal Desk"),
            confidence_threshold=_i("CONFIDENCE_THRESHOLD", 7),
            agent_delay=_f("AGENT_DELAY", 0.6),
            shortlist_per_bucket=_i("SHORTLIST_PER_BUCKET", 4),
            host=_s("HOST", "127.0.0.1"),
            port=_i("PORT", 5000),
            quote_max_age_sec=_i("QUOTE_MAX_AGE_SEC", 300),
            alert_require_open_market=_b("ALERT_REQUIRE_OPEN_MARKET", True),
            fyers_per_minute_cap=_i("FYERS_PER_MINUTE_CAP", 40),  # docs: Standard plan = 50/min
            fyers_daily_cap=_i("FYERS_DAILY_CAP", 4500),          # docs: Standard plan = 5000/day
            gdelt_min_interval=_f("GDELT_MIN_INTERVAL", 8.0),     # GDELT asks ~1 request / 5 s
            groq_max_attempts=_i("GROQ_MAX_ATTEMPTS", 3),
            groq_timeout=_f("GROQ_TIMEOUT", 60),
            instrument_catalogue_url=_s("INSTRUMENT_CATALOGUE_URL", "https://public.fyers.in/sym_details/NSE_CM.csv"),
            instrument_catalogue_ttl=_i("INSTRUMENT_CATALOGUE_TTL", 21600),  # 6 h; file changes far slower
            data_dir=Path(_s("DATA_DIR", str(DATA_DIR))),
        )

    # --- derived, never secret ---
    @property
    def redirect_host(self):
        return (urlparse(self.fyers_redirect_uri).netloc or "").lower()

    @property
    def redirect_is_loopback(self):
        h = urlparse(self.fyers_redirect_uri).hostname or ""
        return h in LOOPBACK_HOSTS

    @property
    def tunnel_host(self):
        return "" if self.redirect_is_loopback else self.redirect_host

    @property
    def fyers_configured(self):
        return bool(self.fyers_app_id and self.fyers_app_secret)

    @property
    def groq_configured(self):
        return bool(self.groq_api_key)

    @property
    def telegram_ready(self):
        return bool(self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id)

    def secret_values(self):
        return [v for v in (self.fyers_app_secret, self.fyers_access_token, self.flask_secret_key,
                            self.dashboard_access_key, self.groq_api_key, self.telegram_bot_token) if v]
