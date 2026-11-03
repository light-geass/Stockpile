"""Specialized market analysis agents, LLM debate engines, and prediction models."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "engine", _ROOT / "agents", _ROOT / "market", _ROOT / "services"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from . import chart_patterns
from . import regime
from . import grounding
from . import llm
from . import predict
from . import outcomes
