# PLAN - HW2: AI Agent Debate

> Execution plan: architecture + per-stage progress + design notes.

## 1. Architecture

The Judge / Parent Process is the central controller. Pro and Con
are sandboxed child processes; they never talk to each other - all
messaging passes through the Supervisor on the Judge side via JSONL
IPC.

```
+------------------------------------------------------------+
|                Judge / Parent Process                       |
|                (central controller)                         |
|                                                             |
|  +-----------+  +-----------+  +-----------+  +----------+ |
|  | Gatekeep. |  | ToolRouter|  | Watchdog  |  |  Logger  | |
|  | LLM/search|  | search +  |  |  child    |  | runs/    | |
|  |  policy   |  |  cache    |  |  recovery |  | <ts>/    | |
|  +-----------+  +-----------+  +-----------+  | run.jsonl| |
|                                               +----------+ |
|                +--------------------+                       |
|                |    Supervisor      |                       |
|                | (JSONL IPC owner)  |                       |
|                +---+-----------+----+                       |
+--------------------|-----------|----------------------------+
                     |           |
            JSONL IPC|           | JSONL IPC
                     |           |
            +--------v---+   +---v--------+
            | Pro child  |   | Con child  |
            | process    |   | process    |
            +------------+   +------------+

         Pro and Con never communicate directly.
         Every Pro <-> Con exchange goes through the Supervisor.
```

### 1.1 Component responsibilities

- **Judge / Parent Process** - owns the debate loop, the turn
  counter, the phase machine (opening / argument / closing), and
  the final verdict. Single source of truth.
- **Supervisor** - spawns Pro and Con child processes, owns the two
  JSONL pipes, marshals every message between them, and shuts the
  children down cleanly.
- **Gatekeeper** - gate for all LLM and search calls made on behalf
  of either side; enforces turn order, response format, length,
  and basic content rules (no insults, on-topic). Rejected outputs
  produce an `event` and either a re-ask or a strike.
- **ToolRouter** - the only path to external tools. Handles search
  with an in-memory + on-disk cache so repeat queries are cheap.
- **Watchdog** - per-turn timeout, total wall-clock budget, child
  liveness probes via `ping`/`pong`. On hang it kills and may
  restart the child; on repeated failure it escalates and aborts.
- **Logger** - writes the run transcript and runtime events to
  `runs/<timestamp>/run.jsonl`. Exactly one JSONL file per run.

### 1.2 Run output

Every run writes a single JSONL transcript:

```
runs/<UTC-timestamp>/run.jsonl
```

The last record in that file is always a `verdict` message with
`winner in {"pro","con"}`. Ties are forbidden by schema.

## 2. Stage progress

| Stage | Scope                                                                  | Status      |
|-------|------------------------------------------------------------------------|-------------|
| 1     | Project skeleton, configs, placeholders, docs.                         | DONE        |
| 2     | Pydantic schemas, JSONL IPC helpers, unit tests.                       | DONE        |
| 3     | Pro / Con child processes talking via JSONL IPC, no moderation yet.    | NOT STARTED |
| 4     | Gatekeeper (turn order, format, content rules, LLM/search gating).     | NOT STARTED |
| 5     | ToolRouter (search + cache) and Supervisor (process orchestration).    | NOT STARTED |
| 6     | Watchdog (per-turn + total timeouts, child recovery, graceful abort).  | NOT STARTED |
| 7     | Judge (final verdict + written justification, no-tie enforcement).     | NOT STARTED |
| 8     | End-to-end run, transcript persistence, evaluation, polish.            | NOT STARTED |

### Stage 1 - skeleton (DONE)

- `pyproject.toml` with `uv` / `pytest` / `ruff` config.
- `src/debate/` package layout with subpackages for `agents/`,
  `gatekeeper/`, `watchdog/`, `judge/`, `ipc/` (legacy stub),
  `config/`, `prompts/`, `utils/`.
- Entry point `debate.main` reachable as
  `uv run python -m debate.main`.
- `tests/test_smoke.py` proves the package layout is importable.

### Stage 2 - schemas + IPC (DONE)

- `src/debate/sdk/schemas.py`
  - `Role` (judge / pro / con)
  - `MessageType` (11 closed values)
  - `Phase` (opening / argument / closing)
  - `Verdict` (winner must be `pro` or `con`; tie forbidden)
  - `Message` envelope: `v, ts, turn_id, role, type, payload`
  - Pydantic v2, `StrEnum`, `extra="forbid"` on the envelope.
- `src/debate/orchestration/ipc.py`
  - `MAX_MESSAGE_BYTES = 64 * 1024`
  - `serialize_message(msg)` returns exactly one newline-terminated line.
  - `deserialize_message(line)` validates size, multiline, version,
    JSON shape, and Pydantic schema.
  - Error hierarchy under `IPCError`: `OversizeError`,
    `MultilineError`, `SchemaVersionError`, `MalformedMessageError`.
- `tests/unit/test_schemas.py` + `tests/unit/test_ipc.py` (42 tests).

### Stage 3 - Pro / Con child processes (TODO)

- `subprocess.Popen` based child launcher (one per side).
- Child-side worker that reads JSONL from stdin and writes JSONL to
  stdout using the Stage 2 helpers.
- Turn scheduler in the Judge that alternates Pro / Con and tags
  the current `phase`.
- Default cadence: 10 turns per side (configurable later).
- No Gatekeeper / Watchdog yet - just two children talking through
  the parent.

### Stage 4 - Gatekeeper (TODO)

- Turn-order check (who should speak next).
- Format / length check on every `reply`.
- Content rules (no insults, on-topic, language policy).
- Strike counter; reject as `event` and re-ask, or abort after N
  strikes.

### Stage 5 - ToolRouter + Supervisor (TODO)

- ToolRouter: single entry point for `tool_call` / `tool_result`,
  with an in-memory LRU cache backed by an on-disk store under
  `runs/<timestamp>/cache/`.
- Supervisor: process lifecycle (spawn, pipe, drain, terminate);
  the only component that holds the child handles.

### Stage 6 - Watchdog (TODO)

- Per-turn timeout (default 30 s) and total wall-clock budget
  (default 300 s); both override-able.
- Cooperative cancellation primitive (raises in the Judge loop on
  timeout).
- Hard-kill fallback for unresponsive children; restart-or-abort
  policy.

### Stage 7 - Judge (TODO)

- Judge prompt with JSON-only output.
- Tie outputs from the LLM are coerced to a side or rejected with a
  re-ask. The on-the-wire `verdict` cannot be a tie.
- Final `verdict` is the last record in `run.jsonl`.

### Stage 8 - End-to-end (TODO)

- Transcript writer (`runs/<timestamp>/run.jsonl`).
- CLI flags: `--topic`, `--turns-per-side`, `--model`.
- End-to-end smoke run with a mock LLM.
- Final README / PRD / PLAN / TODO pass.

## 3. Design notes (open questions captured up front)

- **Message schema**: settled - `role`, `turn_id`, `type`, `payload`,
  envelope-versioned by `v`. Phase is carried inside payloads, not
  the envelope.
- **IPC transport**: stdin/stdout pipes between the parent and each
  child. Trivially upgrades to OS sockets later without changing
  the wire format.
- **Gatekeeper policy**: hard reject + re-ask (no silent
  rewrites). Re-asks are themselves on-the-wire `event` messages so
  the transcript is honest.
- **Watchdog cancellation**: cooperative first
  (`asyncio.CancelledError` or a `threading.Event`), then kill with
  a small grace window. Children must drain quickly on shutdown.
- **Judge rubric**: clarity, evidence quality, rebuttal strength,
  on-topic-ness. Encoded in the Judge prompt; verdict JSON has
  `winner` + `rationale`.
- **Determinism**: temperature + seed pinned per role; the full
  prompt and parameters are logged into `run.jsonl` so a run can be
  replayed.
