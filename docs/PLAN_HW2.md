# PLAN - HW2: AI Agent Debate

> Execution plan: architecture + per-stage progress + design notes.
> Living document. Updated as stages land.

## 1. Architecture

The Judge / Parent process is the central controller. Pro and Con
are sandboxed child subprocesses; they never talk to each other -
all messaging passes through the Supervisor on the Judge side via
JSONL IPC.

```
+------------------------------------------------------------+
|                Judge / Parent process                       |
|                (central controller)                         |
|                                                             |
|  +-----------+  +-----------+  +-----------+  +----------+ |
|  | Gatekeep. |  | ToolRouter|  | Watchdog  |  |  Logger  | |
|  | LLM/search|  | search +  |  |  child    |  | runs/    | |
|  |  policy   |  |  cache    |  |  liveness |  | <ts>/    | |
|  +-----------+  +-----------+  +-----------+  | run.jsonl| |
|                                               +----------+ |
|       +------------------+    +-----------------+          |
|       |  StateMachine    |    |  Judge          |          |
|       | (pure FSM, all   |    | (controls the   |          |
|       |  legal events)   |    |  debate loop)   |          |
|       +------------------+    +-----------------+          |
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

- **CLI** (`debate.main`) - argparse entry point. Loads
  `DebateConfig` and `Motion`, builds every component below, drives
  one debate end-to-end, writes a `runs/<id>/run.jsonl` transcript,
  and supports `--replay` of a saved transcript.
- **Judge / parent process** (`debate.orchestration.judge`) - owns
  the debate loop, validates every child message, alternates Pro
  and Con turns, scores each turn, generates the final verdict
  (with retry + deterministic tie-break), and is the *only*
  component that imports both Supervisor and ToolRouter.
- **DebateStateMachine** (`debate.orchestration.state_machine`) -
  pure FSM. The Judge fires events (`START`,
  `CHILDREN_READY`, `SENT_OPENINGS`, ...) and the FSM enforces
  the legal sequence; no I/O lives here.
- **Supervisor** (`debate.orchestration.supervisor`) - spawns Pro
  and Con as `python -m debate.agents.pro_agent` /
  `con_agent` subprocesses, owns the JSONL pipes, marshals every
  message between them, captures per-role stderr to disk, and
  shuts the children down cleanly. Filters child env to a strict
  allow-list (always strips `SEARCH_API_KEY`).
- **Gatekeeper** (`debate.shared.gatekeeper`) - gate for all LLM
  and search calls. Enforces tokens/turn, tokens/debate,
  USD/debate, and RPM. Updates a `Ledger`. Wraps every
  `LLMClient.complete` and `SearchClient.search` call.
- **ToolRouter** (`debate.shared.router`) - the only path to
  external tools. `call(tool_name, **kw)` dispatches to a
  `SearchClient` (with LRU cache) and raises
  `UnknownToolError` for any tool other than `search`.
- **Watchdog** (`debate.orchestration.watchdog`) - per-turn
  liveness probe. Sends `ping`, expects `pong`, calls
  `on_miss(role)` when a child is unresponsive. Does **not** own
  the recovery policy - that belongs to the Judge / FSM.
- **RunLogger** (`debate.shared.logger`) - writes the run
  transcript and runtime events to `runs/<timestamp>/run.jsonl`.
  One JSONL file per run. Redacts known secret patterns before
  writing.
- **Pro / Con agents** (`debate.agents.{pro,con}_agent` +
  `debate.agents.debater_agent` + `debate.agents.base_agent`) -
  child subprocesses. Receive `prompt` / `tool_result` / `ping`,
  reply with `argument` / `tool_call` / `pong`. Stance-only
  subclass on top of `DebaterAgent`. Use a pluggable
  `LLMClient` (default `FakeLLMClient`) and never see search
  credentials.

### 1.2 Run output

Every live run writes:

```
runs/<UTC-timestamp>/
├── run.jsonl            # JSONL transcript (one JSON object per line)
├── pro_stderr.log       # Pro subprocess stderr capture
└── con_stderr.log       # Con subprocess stderr capture
```

The transcript includes (at minimum) `cli_invoked`,
`debate_started`, `children_spawned`, `init_sent`, `prompt_sent`,
`reply_received`, `score_recorded`, `verdict_recorded`,
`debate_done`, `cli_finished`. Each record carries `ts`, `role`,
`turn_id`, `event_type`, plus event-specific fields. The last
verdict-bearing record is `verdict_recorded`; ties are forbidden
by schema (the Judge always picks `pro` or `con`).

`runs/` itself is tracked via `runs/.gitkeep`; the contents are
gitignored (`runs/*` + `!runs/.gitkeep`).

## 2. Stage progress

| Stage | Scope                                                                  | Status |
|-------|------------------------------------------------------------------------|--------|
| 1     | Project skeleton, configs, placeholders, docs.                         | DONE   |
| 2     | Pydantic schemas, JSONL IPC helpers, unit tests.                       | DONE   |
| 3     | DebateConfig, Motion loader, secret redaction, dotenv hygiene.         | DONE   |
| 4     | LLMClient + SearchClient interfaces, fakes, ToolRouter cache.          | DONE   |
| 5     | DebateStateMachine (pure FSM with legal events / transitions).         | DONE   |
| 6     | Supervisor (subprocess spawn, JSONL pipes, stderr capture, env filter).| DONE   |
| 7     | BaseAgent + DebaterAgent + Pro/Con stance subclasses.                  | DONE   |
| 8     | Watchdog (ping/pong, on_miss callback, no recovery policy).            | DONE   |
| 9     | Judge debate flow + verdict pipeline (retry + deterministic tie-break).| DONE   |
| 10    | CLI, end-to-end run, transcript, replay, doc/cleanup polish.           | DONE   |

### Stage 1 - skeleton (DONE)

- `pyproject.toml` with uv / pytest / ruff config.
- `src/debate/` package with `__main__.py` and a placeholder
  `main.py`.
- Smoke test that the package imports and the entry point exits 0.

### Stage 2 - schemas + IPC (DONE)

- `debate.sdk.schemas`: `Role`, `MessageType` (11 closed values),
  `Phase`, `Verdict` (`winner` is `Literal["pro","con"]`),
  `Message` envelope.
- `debate.orchestration.ipc`: `serialize_message` /
  `deserialize_message` with size, multiline, version, schema
  validation; structured error hierarchy.

### Stage 3 - config / motions / redaction (DONE)

- `debate.shared.config.DebateConfig` + `Motion` loader.
- `debate.shared.redaction` for known secret patterns.
- `.env-example` / `.gitignore` hygiene + tests.

### Stage 4 - LLM/Search/Router fakes (DONE)

- `LLMClient` / `FakeLLMClient`.
- `SearchClient` / `FakeSearchClient`.
- `ToolRouter` with LRU cache + Gatekeeper integration.

### Stage 5 - state machine (DONE)

- `DebateStateMachine` is a pure FSM. Drives every legal event
  transition (start, openings, rounds, closings, verdict, plus
  recovery edges). Owns no I/O.

### Stage 6 - Supervisor (DONE)

- Subprocess spawning with explicit env allow-list (drops
  `SEARCH_API_KEY` always).
- Per-role stderr captured to disk (`<role>_stderr.log`).
- `spawn` / `send` / `receive` / `terminate` / `respawn` /
  `terminate_all`.
- Real subprocess integration tests with `echo_child.py`.

### Stage 7 - debater agents (DONE)

- `BaseAgent` reads JSONL from stdin, writes JSONL to stdout,
  validates everything against the wire schema.
- `DebaterAgent` adds prompt -> argument / tool_call behavior on
  top of `BaseAgent`.
- `ProAgent` / `ConAgent` are stance-only one-line subclasses.

### Stage 8 - Watchdog (DONE)

- Liveness monitor. `start` / `stop` / `check_once`.
- Sends `ping`, expects `pong`, fires `on_miss(role)` for missing
  pongs, malformed pongs, dead children, send/recv errors.
- Strict stage-boundary tests pin that Watchdog never imports
  agent or Judge modules and never calls `Supervisor.respawn`.

### Stage 9 - Judge debate flow + verdict pipeline (DONE)

- `debate.orchestration.judge.Judge` is the central controller.
  Drives the FSM, mediates Pro/Con through the Supervisor,
  validates every reply (role / type / non-empty / stance),
  routes `tool_call` through `ToolRouter.call`, scores each
  turn, accumulates scores, and produces the final verdict.
- Verdict pipeline: LLM -> JSON parse -> validate -> on failure,
  retry once -> on second failure, deterministic tie-break
  (higher cumulative score wins; exact tie -> Con).
- `ToolRouter.call(tool_name, **kw)` + `UnknownToolError` close
  the open audit finding for unknown-tool dispatching.

### Stage 10 - CLI + end-to-end + polish (DONE)

- `debate.main` is now a real CLI with `--motion`, `--rounds`,
  `--model`, `--seed`, `--fake / --no-fake`, `--config`,
  `--motions-file`, `--runs-root`, `--run-id`, `--replay`,
  `--quiet`, `--version`.
- End-to-end run: spawns real Pro/Con subprocesses (using
  `FakeLLMClient`), drives the full FSM, produces a parseable
  `runs/<id>/run.jsonl`, and writes per-role stderr.
- Replay mode reads a saved transcript without ever
  instantiating an LLM, search client, or subprocess.
- `config/prompts/verdict.schema.json` ships as the
  language-agnostic mirror of the `Verdict` Pydantic contract;
  unit-tested end-to-end.
- `runs/.gitkeep` tracks the directory; `.gitignore` excludes
  generated artifacts.
- All Stage 1 placeholder folders
  (`src/debate/{config,gatekeeper,ipc,judge,prompts,utils,watchdog}`)
  are removed.
- README / PROMPTS / PRD / PLAN / TODO refreshed to match the
  Stage 10 surface.

## 3. Design notes

- **Pure FSM**: the state machine has no I/O. The Judge calls
  `transition(event)` and reacts to the resulting `State`.
  Trying to use the FSM as a pseudo-controller would couple
  recovery to the legal-event surface; we deliberately keep them
  apart.
- **Tie handling**: the schema cannot represent a tie. The
  Judge's `_verdict_with_retry` therefore always returns either a
  validated LLM verdict or a tie-broken fallback verdict. Both
  paths produce the same `verdict_recorded` event with `source`
  set to `"llm"` or `"tie_break"`.
- **Subprocess vs in-process child**: the Supervisor spawns real
  subprocesses; the unit-test layer uses an in-memory
  `FakeSupervisor`. The CLI demo uses the real Supervisor with
  `FakeLLMClient` inside the children, so the integration test
  exercises real OS pipes plus the full Stage 9 pipeline.
- **Where `Judge` lives**: `debate.orchestration.judge`, alongside
  `state_machine`, `supervisor`, `watchdog`, and `ipc`. The
  Stage 1 placeholder `src/debate/judge/` was a layout sketch
  only and was removed in Stage 10.
- **Determinism**: `FakeLLMClient` is constructor-deterministic
  (returns its `response_text`), the Stage 4 search cache is
  insertion-ordered, and the FSM is purely event-driven, so the
  same `--seed` + `--motion` + `--rounds` produces a transcript
  that differs only in `ts` fields.
- **Replay scope**: replay is intentionally narrow. It reads the
  transcript line-by-line, prints a per-event summary, surfaces
  the recorded verdict, and exits. It does not re-validate
  scores, re-run prompts, or call any client. Anything richer
  would require keeping prompts/replies in the transcript with
  no redaction loss, which we explicitly avoid.
