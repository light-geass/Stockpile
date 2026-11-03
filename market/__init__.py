"""Market data feeds, broker integrations, symbol search, news, and calendar events."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "engine", _ROOT / "agents", _ROOT / "market", _ROOT / "services"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from . import data_sources
from . import fyers_client
from . import fyers_auth
from . import instrument_search
from . import news
from . import events
from . import etf
from . import ipo

from .fyers_client import FyersClient, FyersError, RateLimiter
from .fyers_auth import FyersAuth, AuthError, TokenStore
from .instrument_search import InstrumentCatalogue, CatalogueError
