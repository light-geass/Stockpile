"""Platform services: configuration, persistence, session security, and alerts."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "engine", _ROOT / "agents", _ROOT / "market", _ROOT / "services"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from . import config
from . import storage
from . import security
from . import watchlists
from . import notifier

from .config import Settings, load_env
from .storage import Store
from .security import install_log_redaction, redact, scrub_obj
from .watchlists import WatchlistStore, WatchlistError, UNIVERSE_SEED
from .notifier import Telegram
