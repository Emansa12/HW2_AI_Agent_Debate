"""Child-process environment allowlist / denylist and builder."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from debate.orchestration.supervisor_io import ChildProcess, reader_loop

_DEFAULT_CHILD_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
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
        "TZ",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "DEBATE_REAL_LLM",
        "DEBATE_REAL_SEARCH",
        "DEBATE_ROUNDS",
        "DEBATE_MAX_TOKENS",
        "DEBATE_MOTION",
    }
)

_DENIED_CHILD_ENV_KEYS: frozenset[str] = frozenset(
    {
        "SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "SERPAPI_API_KEY",
    }
)


def build_child_env(
    role: str,
    env_source: Mapping[str, str],
    env_allowlist: Iterable[str],
    denied_env_keys: Iterable[str],
) -> dict[str, str]:
    """Build the env dict passed to a child process."""
    out: dict[str, str] = {}
    for key in env_allowlist:
        if key in env_source:
            out[key] = env_source[key]
    for key in denied_env_keys:
        out.pop(key, None)
    out["DEBATE_ROLE"] = role
    return out


def spawn_child(
    *,
    role: str,
    restart_count: int,
    runs_dir: Path,
    stderr_filename: str,
    command: list[str],
    child_env: dict[str, str],
    popen: Callable[..., subprocess.Popen],
    clock: Callable[[], float],
) -> ChildProcess:
    stderr_path = runs_dir / stderr_filename
    stderr_fh = stderr_path.open("ab", buffering=0)
    try:
        process = popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_fh,
            env=child_env,
            bufsize=0,
        )
    except Exception:
        stderr_fh.close()
        raise
    cp = ChildProcess(
        role=role,
        process=process,
        stdin=process.stdin,
        stdout=process.stdout,
        stderr_path=stderr_path,
        start_time=clock(),
        restart_count=restart_count,
        stderr_fh=stderr_fh,
    )
    cp.reader_thread = threading.Thread(
        target=reader_loop,
        args=(cp,),
        name=f"supervisor-reader-{role}",
        daemon=True,
    )
    cp.reader_thread.start()
    return cp
