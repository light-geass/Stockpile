"""Multi-agent debate engine, scoring pipeline, evidence collection, and execution playbook."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "engine", _ROOT / "agents", _ROOT / "market", _ROOT / "services"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from . import pipeline
from . import scoring
from . import evidence
from . import playbook
from . import sizing

from .pipeline import Pipeline, AGENTS
from .scoring import evaluate_deterministic, scores3
from .evidence import assemble_bundle, market_session, technicals_from_daily, IST
from .sizing import guidance
