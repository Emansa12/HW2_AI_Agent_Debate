"""Environment dict builder for child agent subprocesses."""

from __future__ import annotations

import os
from pathlib import Path


def _build_child_env(*, real_llm: bool = False, real_search: bool = False) -> dict[str, str]:
    """Return a minimal env dict for child agent subprocesses.

    The :class:`Supervisor` further filters this through its own
    allow-list and removes search API keys (defense in depth).
    We deliberately copy through the bits Python needs to import
    ``debate.agents.pro_agent`` / ``con_agent`` (PYTHONPATH for the
    ``src/`` layout, plus Windows-essential vars).

    When ``real_llm=True`` (Stage 11) we also forward the
    ``LLM_API_KEY`` / ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
    ``OPENAI_MODEL`` values from the parent env (so the child's
    ``RealLLMClient.from_env()`` finds them) and set
    ``DEBATE_REAL_LLM=1`` to flip the child's ``__main__`` block
    to the real client.

    When ``real_search=True``, set ``DEBATE_REAL_SEARCH=1`` so each
    debater emits one brokered search ``tool_call`` on opening /
    first argument. Search keys stay in the parent only.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    passthrough_keys = (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOMEPATH",
        "HOMEDRIVE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    )
    for key in passthrough_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    if real_llm:
        env["DEBATE_REAL_LLM"] = "1"
        for key in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
            if key in os.environ:
                env[key] = os.environ[key]

    if real_search:
        env["DEBATE_REAL_SEARCH"] = "1"

    src_path = Path(__file__).resolve().parents[1]
    if src_path.is_dir():
        existing = env.get("PYTHONPATH", "")
        sep = os.pathsep
        env["PYTHONPATH"] = f"{src_path}{sep}{existing}" if existing else str(src_path)
    return env
