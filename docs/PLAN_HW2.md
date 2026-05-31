# PLAN - HW2: AI Agent Debate

> Execution plan: architecture + per-stage progress + design notes.
> Living document. Updated as stages land.

## 1. Architecture

The Judge / Parent process is the central controller. Pro and Con
are sandboxed child subprocesses; they never talk to each other -
all messaging passes through the Supervisor on the Judge side via
JSONL IPC.

The only Python application package is **`src/debate/`**. No
alternative application package names are used anywhere in the
tree. The user-facing run command is
always:

```
uv run python -m debate.main
```

### 1.0 Current package layout

```
src/debate/
├── __init__.py
├── __main__.py               # forwards to debate.main:main
├── main.py                   # CLI entry point (Stage 10 + 11)
├── agents/                   # child-side debater code
│   ├── base_agent.py         #   stdin/stdout JSONL agent base
│   ├── debater_agent.py      #   prompt -> argument / tool_call
│   ├── pro_agent.py          #   stance="pro"
│   └── con_agent.py          #   stance="con"
├── orchestration/            # parent-side controllers
│   ├── ipc.py                #   serialize/deserialize_message
│   ├── state_machine.py      #   pure deterministic FSM
│   ├── supervisor.py         #   subprocess + JSONL pipes
│   ├── watchdog.py           #   ping/pong liveness probe
│   └── judge.py              #   debate loop + verdict pipeline
├── sdk/                      # public schemas + provider clients
│   ├── schemas.py            #   Message/Role/MessageType/Phase/Verdict
│   ├── llm_client.py         #   LLMClient + FakeLLMClient
│   ├── search_client.py      #   SearchClient + FakeSearchClient
│   ├── real_llm_client.py    #   Stage 11: RealLLMClient (opt-in)
│   └── real_search_client.py #   Stage 11: RealSearchClient (opt-in)
└── shared/                   # cross-cutting infra
    ├── config.py             #   DebateConfig + Motion loader
    ├── gatekeeper.py         #   tokens/usd/rpm policy + Ledger
    ├── logger.py             #   RunLogger -> runs/<id>/run.jsonl
    ├── redaction.py          #   secret-pattern redactor
    └── router.py             #   ToolRouter (LRU cache + Gatekeeper)
```

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
  allow-list. Deny-list always strips every search-key shape
  (`SEARCH_API_KEY`, `TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`,
  `SERPAPI_API_KEY`) so Pro / Con can never reach a search
  provider directly even in Stage 11 real-mode.
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
| 11    | Optional real-provider clients (Tavily search + OpenAI-compatible LLM).| DONE   |

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

- Subprocess spawning with explicit env allow-list. Deny-list
  always strips search-key shapes (`SEARCH_API_KEY` from Stage 6;
  `TAVILY_API_KEY` / `BRAVE_SEARCH_API_KEY` / `SERPAPI_API_KEY`
  added in Stage 11) regardless of allow-list growth.
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

- `debate.main` is a real CLI with `--motion`, `--rounds`,
  `--model`, `--seed`, `--fake` / `--no-fake`,
  `--real-search`, `--real-llm` *(both added in Stage 11 but
  surfaced as part of the same CLI namespace)*, `--config`,
  `--motions-file`, `--runs-root`, `--run-id`, `--replay`,
  `--quiet`, `--version`. The user-facing run command is
  always `uv run python -m debate.main`.
- End-to-end run: spawns real Pro/Con subprocesses (using
  `FakeLLMClient` by default), drives the full FSM, produces a
  parseable `runs/<id>/run.jsonl`, and writes per-role stderr.
- Replay mode reads a saved transcript without ever
  instantiating an LLM, search client, or subprocess.
- `config/prompts/verdict.schema.json` ships as the
  language-agnostic mirror of the `Verdict` Pydantic contract;
  unit-tested end-to-end.
- `runs/.gitkeep` tracks the directory; `.gitignore` excludes
  generated artifacts (`runs/*` + `!runs/.gitkeep`).
- All Stage 1 placeholder layout-sketch folders
  (`src/debate/{config,gatekeeper,ipc,judge,prompts,utils,watchdog}`)
  were removed in Stage 10. Those names are **not** used and
  never were used by any production code; they were only ever
  empty directories. The current layout is the single source of
  truth - see [§ 1.0](#10-current-package-layout).
- README / PROMPTS / PRD / PLAN / TODO refreshed to match.

### Stage 11 - Optional Real API Support (DONE, opt-in)

Stage 11 adds two **opt-in** real-provider clients without
changing the default fake-mode behaviour. The grading path is
unchanged: `--fake` is still the default, the entire test suite
is offline, and no real API key is required for `pytest`,
`ruff`, or the demo command.

- `debate.sdk.real_search_client.RealSearchClient` - HTTP-backed
  `SearchClient` for **Tavily** (`https://api.tavily.com/search`).
  Reads `SEARCH_API_KEY` (canonical) or `TAVILY_API_KEY` (alias)
  from the env. Sends `Authorization: Bearer …`; never embeds
  the key in URLs, request bodies, or logs. Maps upstream errors
  to typed exceptions (`MissingSearchAPIKeyError`,
  `SearchProviderError`, `SearchProviderUnavailableError`,
  `SearchProviderResponseError`). Routes the response through
  the same `SearchResult` / `SearchResponse` Pydantic models so
  sanitisation and size caps match the fake client.
- `debate.sdk.real_llm_client.RealLLMClient` - HTTP-backed
  `LLMClient` for **OpenAI-compatible** Chat Completions
  endpoints (OpenAI itself, Together, Groq, OpenRouter, Azure
  OpenAI, local vLLM / LM Studio bridges). Reads `LLM_API_KEY`
  (canonical) or `OPENAI_API_KEY` (alias). Computes USD cost
  from the upstream `usage` block via configurable per-1K-token
  prices. Same typed-error hierarchy
  (`MissingLLMAPIKeyError`, `LLMProviderError`,
  `LLMProviderUnavailableError`, `LLMProviderResponseError`).
- New runtime dependency: `httpx >= 0.27.0` (used only by the
  two real clients; tests use `httpx.MockTransport` for
  offline coverage).
- New CLI flags (default off): `--real-search`, `--real-llm`,
  and `--no-fake` (shorthand for both). `--fake` (default) is
  unchanged. Flags can be combined - "fake LLM + real search"
  is a legitimate hybrid mode.
- `Supervisor` allow-list adds `LLM_API_KEY` and
  `DEBATE_REAL_LLM`; deny-list extends to `TAVILY_API_KEY`,
  `BRAVE_SEARCH_API_KEY`, `SERPAPI_API_KEY` so Pro / Con can
  never see a search key.
- `pro_agent.py` / `con_agent.py` `__main__` blocks switch to
  `RealLLMClient.from_env()` only when `DEBATE_REAL_LLM=1` is
  present in their env (set by the parent CLI on `--real-llm`).
  Default stays `FakeLLMClient`, so every existing test path is
  untouched.
- Every real call still flows through
  Judge → ToolRouter → Gatekeeper for tools, and
  Judge → Gatekeeper → LLMClient for completions. The Stage 4
  redaction, ledger, and rate limiting paths are identical for
  fake and real clients.

**Run command examples** (cross-checked with README §
"Optional: real-provider modes"):

```
# Default - offline fake mode (used for grading, demos, tests)
uv run python -m debate.main --motion "..." --rounds 2 --fake

# Hybrid - real Tavily search, fake LLM
uv run python -m debate.main --motion "..." --rounds 2 --fake --real-search

# Full real-provider mode (requires both API keys)
uv run python -m debate.main --motion "..." --rounds 2 --no-fake
# equivalent, more explicit:
uv run python -m debate.main --motion "..." --rounds 2 --real-llm --real-search
```

### Design note - why two extra clients (Stage 11)

The HW2 grading run uses fake clients only - the assignment asks
for a working multi-agent debate, not a demo of a paid provider.
Stage 11 was added because the protocol is already provider-neutral
(`LLMClient` and `SearchClient` are bare Protocols), and gluing in
real HTTP backends costs ~300 LOC each and demonstrates that the
abstraction is real. Crucially:

- The default mode and the test suite still need **zero** keys.
- All gatekeeping, redaction, and JSON-Schema validation paths
  fire identically for fake and real clients.
- The ``httpx.MockTransport`` test fixtures cover request shape,
  response parsing, error mapping, and missing-key behaviour so a
  reviewer can read the unit tests instead of running a paid
  provider.

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

## 4. Cross-document consistency

This file is kept in lock-step with the other Stage 11 docs. If
any of these statements is ever violated, please open an issue:

- The only application package is `src/debate/` (matches
  [`PRD_HW2.md`](PRD_HW2.md) § 2 "Tooling (mandatory)" and
  [`README.md`](../README.md) "Project layout"). No alternative
  application package names are used anywhere in the tree.
- The user-facing run command is always
  `uv run python -m debate.main` (matches PRD § 2 and the
  README "Quick start").
- Default mode is `--fake`; `--real-search`, `--real-llm`,
  and `--no-fake` are documented as opt-in real-provider modes
  in README, [`PROMPTS.md`](../PROMPTS.md) "Stage 11" section,
  and the Stage 11 evidence block in
  [`TODO_HW2.md`](TODO_HW2.md).
- Tests stay offline by default; real-client tests use
  `httpx.MockTransport`. This is asserted in TODO_HW2.md's
  Stage 11 evidence block and in README "Security".
- The on-disk module layout in § 1.0 above must match the actual
  contents of `src/debate/`. The Stage 1 placeholder folders
  `src/debate/{config,gatekeeper,ipc,judge,prompts,utils,watchdog}`
  are layout sketches that were never used; they were deleted
  in Stage 10 and are not part of the live architecture.
