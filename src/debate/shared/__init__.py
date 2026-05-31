"""Shared infrastructure used by both the parent and child processes.

Stage 3 surface:
    - config:    typed loaders for `config/debate.json` / `motions.json`
    - secrets:   env-only secret reader + optional dotenv preloader
    - logger:    per-run JSONL transcript writer (`runs/<id>/run.jsonl`)
    - redaction: structural redaction of sensitive keys before logging

Stage 4 surface:
    - ledger:     cumulative usage counter (requests / tokens / USD)
    - gatekeeper: budget + rate-limit policy wrapped around every
                  external LLM/search call
    - router:     ToolRouter (LRU-cached search), routes through
                  the Gatekeeper
"""

from debate.shared.config import (
    DebateConfig,
    Motion,
    Motions,
    default_debate_config_path,
    default_motions_path,
    load_debate_config,
    load_motions,
)
from debate.shared.gatekeeper import (
    BudgetExceededError,
    BudgetKind,
    Gatekeeper,
    GatekeeperPolicy,
)
from debate.shared.ledger import Ledger
from debate.shared.logger import RunLogger
from debate.shared.redaction import (
    REDACTION_PLACEHOLDER,
    SENSITIVE_KEY_TOKENS,
    is_sensitive_key,
    redact,
)
from debate.shared.router import DEFAULT_CACHE_SIZE, ToolRouter
from debate.shared.secrets import Secrets, load_secrets, maybe_load_dotenv

__all__ = [
    "DEFAULT_CACHE_SIZE",
    "REDACTION_PLACEHOLDER",
    "SENSITIVE_KEY_TOKENS",
    "BudgetExceededError",
    "BudgetKind",
    "DebateConfig",
    "Gatekeeper",
    "GatekeeperPolicy",
    "Ledger",
    "Motion",
    "Motions",
    "RunLogger",
    "Secrets",
    "ToolRouter",
    "default_debate_config_path",
    "default_motions_path",
    "is_sensitive_key",
    "load_debate_config",
    "load_motions",
    "load_secrets",
    "maybe_load_dotenv",
    "redact",
]
