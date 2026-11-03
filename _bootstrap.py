"""Module identity bridge: aliases domain packages to top-level names for seamless imports."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "engine", ROOT / "agents", ROOT / "market", ROOT / "services"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

import services.config
import services.watchlists
import services.storage
import services.security
import services.notifier

import engine.pipeline
import engine.scoring
import engine.evidence
import engine.playbook
import engine.sizing

import agents.chart_patterns
import agents.regime
import agents.grounding
import agents.llm
import agents.predict
import agents.outcomes

import market.data_sources
import market.fyers_client
import market.fyers_auth
import market.instrument_search
import market.news
import market.events
import market.etf
import market.ipo

_ALIASES = {
    "config": services.config,
    "watchlists": services.watchlists,
    "storage": services.storage,
    "security": services.security,
    "notifier": services.notifier,
    "pipeline": engine.pipeline,
    "scoring": engine.scoring,
    "evidence": engine.evidence,
    "playbook": engine.playbook,
    "sizing": engine.sizing,
    "chart_patterns": agents.chart_patterns,
    "regime": agents.regime,
    "grounding": agents.grounding,
    "llm": agents.llm,
    "predict": agents.predict,
    "outcomes": agents.outcomes,
    "data_sources": market.data_sources,
    "fyers_client": market.fyers_client,
    "fyers_auth": market.fyers_auth,
    "instrument_search": market.instrument_search,
    "news": market.news,
    "events": market.events,
    "etf": market.etf,
    "ipo": market.ipo,
}

for name, mod in _ALIASES.items():
    sys.modules[name] = mod
