# PRD - HW2: AI Agent Debate

> Product Requirements Document for HW2.
> Living document - revised as stages land.

## 1. Goal

Build a multi-agent debate system in which two LLM-backed agents
(**Pro** and **Con**) argue a single topic under the supervision of
a central **Judge / Parent Process** and produce a final verdict
that picks a single winner.

## 2. Tooling (mandatory)

- Python package name: `src/debate/` (no alternative application
  package name is allowed anywhere in the repo).
- Run command (final, user-facing):

  ```
  uv run python -m debate.main
  ```

- Dependency / environment management: **uv**.
- Tests: **pytest**.
- Lint and format: **ruff** (`ruff check` and `ruff format --check`).

## 3. Architecture (mandatory)

- The **Judge / Parent Process** is the central controller. It owns
  the debate loop and is the only process that talks to either side.
- **Pro** and **Con** are **child processes** spawned by the Judge.
  They do useful work in isolation and have no awareness of each
  other.
- **Pro and Con never communicate directly.** Every message between
  the two sides is routed through the Judge side via **JSONL IPC**
  (one UTF-8 JSON object per line, terminated by exactly one `\n`).
- All Judge-side subsystems live inside or are controlled by the
  Judge / Parent Process:
  - **Gatekeeper** - controls all LLM and search calls (decides
    whether a child may call out, enforces format / length / content
    policy).
  - **ToolRouter** - handles search and the search cache.
  - **Supervisor** - owns the JSONL pipes to each child, marshals
    messages, and forwards them.
  - **Watchdog** - per-turn / total timeouts; handles child
    recovery (restart on crash, kill on hang, escalate to abort).
  - **Logger** - writes the full transcript and runtime events.

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

## 4. Protocol (mandatory)

- Roles: `judge`, `pro`, `con`.
- Message types: `init`, `prompt`, `reply`, `tool_call`,
  `tool_result`, `ping`, `pong`, `score`, `verdict`, `event`,
  `shutdown`.
- Phases: `opening`, `argument`, `closing`.
- Wire envelope fields: `v`, `ts`, `turn_id`, `role`, `type`,
  `payload`.
- Each message is one UTF-8 JSON object on a single line, terminated
  by exactly one `\n`. Embedded newlines / CR are forbidden.
  Oversized lines are rejected at both ends.
- `verdict.winner` must be exactly `pro` or `con`. **A tie is
  forbidden** at the schema level - the Judge must pick a side.

## 5. Runtime defaults (mandatory)

- Default debate length: **10 turns per side** (Pro and Con each get
  10 turns). Configurable but defaults are honored when no override
  is given.
- Run output goes to `runs/<timestamp>/run.jsonl` - one JSONL file
  per run, named by a UTC timestamp.
- One run produces exactly one `verdict` message; the run cannot end
  without a winner (no ties).

## 6. Non-goals (for this HW)

- A web UI.
- Multi-topic / batch tournaments.
- Persistence beyond on-disk JSONL transcripts under `runs/`.
- Tool use beyond a minimal search stub routed via ToolRouter.

## 7. Success criteria

- `uv run python -m debate.main` runs end-to-end without unhandled
  errors and writes a `runs/<timestamp>/run.jsonl` file whose last
  verdict-bearing record is a `verdict_recorded` event with
  `winner in {"pro","con"}`.
- All unit and integration tests pass under `uv run pytest -q`.
- `uv run ruff check .` and `uv run ruff format --check .` are clean.
- The only application package present is `debate`.
- The default demo runs **fully offline** (FakeLLMClient +
  FakeSearchClient) and requires no real API key.
- A saved transcript can be re-displayed via
  `uv run python -m debate.main --replay runs/<timestamp>/run.jsonl`
  without any LLM / search call.

## 8. Stages

See [`PLAN_HW2.md`](PLAN_HW2.md) for the execution plan and
[`TODO_HW2.md`](TODO_HW2.md) for the granular checklist. As of
Stage 11, the ten core stages are DONE plus optional real-provider
support (Tavily search + OpenAI-compatible LLM) is wired in
behind ``--real-search`` / ``--real-llm`` / ``--no-fake``. The
default mode and the entire test suite remain offline - Stage 11
adds **opt-in** capability without changing the grading path.
