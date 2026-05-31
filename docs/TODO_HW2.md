# TODO — HW2: AI Agent Debate

> Per-stage checklist, kept in sync with the actual repository state.
> See [`PLAN_HW2.md`](PLAN_HW2.md) for architecture and rationale, and
> [`PRD_HW2.md`](PRD_HW2.md) for the high-level brief.
>
> Legend:
> `[x]` = verified done (file / test / runtime check passes)
> `[~]` = partial — see note on the same line
> `[ ]` = not started yet
>
> The build order in this file is the order things were actually
> shipped, which is **not** the same as the old planning numbering.

## Current verified status

| Stage | Scope                                                                            | Status |
|-------|----------------------------------------------------------------------------------|--------|
| 1     | Project skeleton, configs, placeholders, docs                                    | DONE   |
| 2     | Pydantic schemas + JSONL IPC helpers                                             | DONE   |
| 3     | Config + secrets + structured logging + redaction                                | DONE   |
| 4     | Fake LLM / Search clients + Ledger + Gatekeeper + ToolRouter (with LRU cache)    | DONE   |
| 5     | Pure deterministic debate state machine (FSM)                                    | DONE   |
| 6     | Supervisor / child process manager + integration smoke                           | DONE   |
| 7     | BaseAgent, DebaterAgent, ProAgent, ConAgent                                      | DONE   |
| 8     | Watchdog / liveness monitor                                                      | DONE   |
| 9     | Judge debate flow + verdict pipeline                                             | DONE   |
| 10    | End-to-end debate loop, transcript, CLI, polish                                  | DONE   |

**Latest gate run** (Sunday 2026-05-31, 23:55 UTC+3 — after Stage 10):

```
uv sync                       Resolved 15 packages in 1ms / Checked 15 packages in 2ms
uv run pytest -q              574 passed in 5.40s
uv run ruff check .           All checks passed!
uv run ruff format --check .  56 files already formatted
uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake
                              winner=pro, scores=pro=50/con=40, transcript runs/<ts>/run.jsonl
```

Stage 10 added **59 new tests** on top of the 515 from Stages 1–9
(test counts updated where existing files grew):

- `tests/unit/test_cli.py` — 25 tests (parser, motion / config
  resolution, builder helpers, replay path, dispatch & namespace
  shape).
- `tests/unit/test_verdict_schema.py` — 19 tests (`config/prompts/verdict.schema.json`
  shape + behavioral constraints — winner enum, score bounds,
  reasons array minimum, no extra fields).
- `tests/unit/test_housekeeping.py` — 8 tests (`runs/.gitkeep`
  + `.gitignore` rules, dead placeholder folders gone, real
  subpackages still present, `verdict.schema.json` parses,
  `.env-example` has no real-looking secrets).
- `tests/integration/test_e2e_debate.py` — 6 tests (real-subprocess
  end-to-end run with FakeLLMClient, transcript / verdict /
  ledger assertions, replay round-trip, quiet mode, seed flag,
  spec demo command).
- `tests/test_smoke.py` was retargeted to the new CLI surface
  (replay-only path, no banner string match).

Stage 9 added **69 new tests** on top of the 446 from Stages 1–8:

- `tests/unit/test_judge_agent.py` — 58 tests (construction, init /
  prompt building, reply validation, scoring, verdict
  parse / validate / tie-break, run_turn dispatcher, full
  `run_debate` E2E with FakeSupervisor, verdict retry path,
  stage-boundary import / IO checks).
- `tests/integration/test_judge_debate_flow.py` — 2 tests (offline
  2-round debate driving a real `RunLogger` to disk + clean
  cleanup).
- `tests/unit/test_router_cache.py::TestRouterCallDispatcher` —
  9 tests for the new `ToolRouter.call()` + `UnknownToolError`
  surface.

**Open audit findings** — all four are now **CLOSED** as of Stage 10:

1. ~~`runs/.gitkeep` and a `runs/* except !runs/.gitkeep` rule in
   `.gitignore` were never created.~~ **CLOSED in Stage 10.**
   `runs/.gitkeep` is committed and `.gitignore` excludes
   `runs/*` while whitelisting `!runs/.gitkeep`. Pinned by
   `tests/unit/test_housekeeping.py::TestRunsDir`.
2. ~~`config/prompts/verdict.schema.json` does not exist.~~
   **CLOSED in Stage 10.** The JSON Schema mirror is now committed
   at `config/prompts/verdict.schema.json` — winner enum is
   `["pro","con"]`, scores are bounded `0..100`, `reasons.minItems`
   is `3`, root has `additionalProperties: false`. Pinned by
   `tests/unit/test_verdict_schema.py` (19 tests covering shape +
   behavioral constraints).
3. ~~`ToolRouter` exposes only `.search(query)`; there is no
   `router.call(tool_name, ...)` dispatcher with an explicit
   `UnknownToolError`.~~ **CLOSED in Stage 9.** `ToolRouter.call(...)`
   is the typed dispatcher used by the Judge to route `tool_call`
   envelopes from children; unknown names raise
   `UnknownToolError` (subclass of `ValueError`); covered by
   `tests/unit/test_router_cache.py::TestRouterCallDispatcher`.
4. ~~The Stage 1 layout sketch folders
   `src/debate/{config,gatekeeper,ipc,judge,prompts,utils,watchdog}/`
   still exist with short placeholder modules.~~ **CLOSED in
   Stage 10.** All seven Stage 1 placeholder directories were
   removed; the real homes are
   `src/debate/{sdk,shared,orchestration,agents}/`. Pinned by
   `tests/unit/test_housekeeping.py::TestPlaceholderCleanup`.

---

## Stage 1 — Project skeleton

- [x] `pyproject.toml` (uv + pytest + ruff; PEP 735 `[dependency-groups].dev`)
- [x] `src/debate/` package layout, importable
- [x] `uv run python -m debate.main` placeholder banner returns 0
- [x] `tests/test_smoke.py` (Stage 10: retargeted to verify
      version + CLI `--help` / `--version` / `--replay` paths)
- [x] `README.md`
- [x] `PROMPTS.md`
- [x] `.env-example` (only placeholder values, no real secrets)
- [x] `.gitignore` ignores `.env`, `.venv/`, caches, `.ruff_cache/`,
      `.pytest_cache/`
- [x] `config/debate.json`
- [x] `config/motions.json`
- [x] `docs/PRD_HW2.md`, `docs/PLAN_HW2.md`, `docs/TODO_HW2.md`
- [x] `tests/unit/` and `tests/integration/` directories
- [x] `pro.py` / `con.py` renamed to `pro_agent.py` / `con_agent.py`
      (Windows reserved-name fix for `CON`)
- [x] No `debate_app` references anywhere in the tree
      (verified by repo-wide grep)
- [x] `runs/.gitkeep` exists (closed in Stage 10)
- [x] `.gitignore` excludes `runs/*` except `runs/.gitkeep`
      (closed in Stage 10)
- [x] `config/prompts/verdict.schema.json` exists
      (closed in Stage 10)

**Evidence — Stage 1**

- Files: `pyproject.toml`, `README.md`, `PROMPTS.md`, `.env-example`,
  `.gitignore`, `config/debate.json`, `config/motions.json`,
  `src/debate/__init__.py`, `src/debate/__main__.py`,
  `src/debate/main.py`, `docs/PRD_HW2.md`, `docs/PLAN_HW2.md`.
- Tests: `tests/test_smoke.py::test_package_has_version`,
  `tests/test_smoke.py::test_main_returns_zero`.
- Latest run: `uv run python -m debate.main` exits 0, prints
  "Stage 1 OK".

---

## Stage 2 — Pydantic schemas + JSONL IPC

- [x] `src/debate/sdk/schemas.py` with:
  - [x] `Role` (`judge`, `pro`, `con`) as `StrEnum`
  - [x] `MessageType` covering `init`, `prompt`, `reply`, `tool_call`,
        `tool_result`, `ping`, `pong`, `score`, `verdict`, `event`,
        `shutdown`
  - [x] `Phase` covering `opening`, `argument`, `closing`
  - [x] `Message` envelope with `v`, `ts`, `turn_id`, `role`, `type`,
        `payload` (`extra="forbid"`)
  - [x] `Verdict` payload with `winner ∈ {pro, con}` (ties forbidden)
- [x] `src/debate/orchestration/ipc.py` with:
  - [x] `serialize_message` → single `\n`-terminated UTF-8 line
  - [x] `deserialize_message` validates schema + version
  - [x] `MAX_MESSAGE_BYTES` size cap
  - [x] Error hierarchy: `IPCError`, `OversizeError`,
        `MultilineError`, `SchemaVersionError`,
        `MalformedMessageError`
- [x] Rejects invalid role / type / version / extra fields
- [x] Rejects verdict ties
- [x] Rejects oversize / embedded-newline lines

**Evidence — Stage 2**

- Files: `src/debate/sdk/schemas.py`,
  `src/debate/orchestration/ipc.py`.
- Tests: `tests/unit/test_schemas.py`,
  `tests/unit/test_ipc.py` (all passing in the latest run).

---

## Stage 3 — Config + secrets + logging + redaction

- [x] `src/debate/shared/config.py`
  - [x] `DebateConfig` Pydantic model with bounded fields
  - [x] `Motions` Pydantic model with bounded ID/topic lengths
  - [x] `load_debate_config(path)` and `load_motions(path)` validators
- [x] `src/debate/shared/secrets.py`
  - [x] `Secrets` dataclass, frozen
  - [x] `load_secrets()` reads only from `os.environ`
  - [x] `maybe_load_dotenv()` opt-in dev convenience
- [x] `src/debate/shared/logger.py`
  - [x] `RunLogger` creates `runs/<run_id>/` lazily
  - [x] `run.jsonl` is one JSON object per line, redacted on write
  - [x] Stderr capture paths exposed: `pro_stderr.log`,
        `con_stderr.log` (underscores, not dots — Windows `CON`
        reserved-name fix)
  - [x] Records always include `ts`, `role`, `turn_id`, `event_type`
- [x] `src/debate/shared/redaction.py`
  - [x] Substring (case-insensitive) match on `api_key`, `token`,
        `secret`, `password`, `authorization`
  - [x] Recurses through dict / list / tuple, deep-copies, never mutates
  - [x] Replaces values with `<redacted>` (not keys)
- [x] `.env` is ignored by Git (Stage 1)
- [x] `.env-example` has no real secret values
- [x] Normal debate text (no sensitive keys) is not over-redacted
- [x] Loaders reject bad config values (out-of-range, missing,
      malformed JSON)

**Evidence — Stage 3**

- Files: `src/debate/shared/config.py`,
  `src/debate/shared/secrets.py`, `src/debate/shared/logger.py`,
  `src/debate/shared/redaction.py`.
- Tests: `tests/unit/test_config.py`, `tests/unit/test_secrets.py`,
  `tests/unit/test_logger.py`, `tests/unit/test_redaction.py`
  (all passing).

---

## Stage 4 — Fake LLM/Search clients + Gatekeeper + Ledger + ToolRouter

- [x] `src/debate/sdk/llm_client.py`
  - [x] `LLMClient` runtime-checkable `Protocol`
  - [x] `LLMResponse` Pydantic model (`text`, `tokens_in`,
        `tokens_out`, `usd`)
  - [x] `FakeLLMClient` offline, deterministic, no API key
  - [x] Client itself owns no budget logic
- [x] `src/debate/sdk/search_client.py`
  - [x] `SearchClient` runtime-checkable `Protocol`
  - [x] `SearchResult` with `title`, `url`, `snippet`
  - [x] URL must be absolute `http(s)://`
  - [x] Length caps: `MAX_TITLE_CHARS`, `MAX_URL_CHARS`,
        `MAX_SNIPPET_CHARS`, `MAX_RESULTS_PER_RESPONSE`
  - [x] Sanitization strips control chars and trims whitespace
  - [x] `FakeSearchClient` offline, deterministic, no API key
  - [x] Client itself owns no budget logic
- [x] `src/debate/shared/ledger.py`
  - [x] Tracks `requests`, `tokens_in`, `tokens_out`, `usd_spent`
  - [x] Sliding-window `requests_in_window` for rate limits
  - [x] `record(...)` / `reserve_request(...)` / `add_usage(...)` API
- [x] `src/debate/shared/gatekeeper.py`
  - [x] `GatekeeperPolicy` Pydantic model with bounded limits
  - [x] `BudgetKind` enum + `BudgetExceededError` typed exception
  - [x] `Gatekeeper.call_llm(...)` checks per-turn tokens,
        per-debate tokens, USD per debate, requests-per-minute
  - [x] `Gatekeeper.call_search(...)` checks the same budgets
  - [x] On budget refusal the underlying callable is **not invoked**
  - [x] On success the ledger is updated atomically
- [x] `src/debate/shared/router.py`
  - [x] `ToolRouter` wraps Gatekeeper + SearchClient + LRU cache
  - [x] `ToolRouter.search(query)` normalizes (lowercase + whitespace
        collapse) before keying the cache
  - [x] Cache hit returns a copy without calling SearchClient or the
        Gatekeeper
  - [x] Cache eviction is LRU with configurable `cache_size`
  - [x] Results are capped to `MAX_RESULTS_PER_RESPONSE`
  - [x] `BudgetExceededError` is propagated unchanged
  - [x] `clear_cache()` resets state
- [x] No real HTTP / API call is required by any Stage 4 test
- [~] Unknown tools are rejected — **PARTIAL**: implicit only.
      `ToolRouter` exposes only `.search()`; there is no
      `call(tool, ...)` dispatcher that raises on unknown tool
      names. The explicit rejection will land with the debate loop's
      `tool_call` handler. See open finding #3.

**Evidence — Stage 4**

- Files: `src/debate/sdk/llm_client.py`,
  `src/debate/sdk/search_client.py`,
  `src/debate/shared/ledger.py`,
  `src/debate/shared/gatekeeper.py`,
  `src/debate/shared/router.py`.
- Tests: `tests/unit/test_llm_client.py`,
  `tests/unit/test_search_client.py`,
  `tests/unit/test_gatekeeper.py` (covers Ledger + Gatekeeper +
  budget refusal not invoking the callable),
  `tests/unit/test_router_cache.py` (covers cache hit bypassing
  Gatekeeper, LRU eviction, BudgetExceededError propagation).

---

## Stage 5 — Pure deterministic debate state machine

- [x] `src/debate/orchestration/state_machine.py`
- [x] States: `INIT`, `SPAWNING`, `OPENING`, `PRO_TURN`, `SCORE_PRO`,
      `CON_TURN`, `SCORE_CON`, `NEXT_ROUND`, `CLOSING`, `VERDICT`,
      `VALIDATE_VERDICT`, `TIE_BREAK`, `RECOVER`, `ABORT`, `DONE`
- [x] Events: `start`, `children_ready`, `sent_openings`, `pro_reply`,
      `con_reply`, `scored`, `round_limit_reached`,
      `closings_received`, `judge_reply`, `invalid_or_tie`,
      `valid_non_tie`, `heartbeat_miss`, `respawned`,
      `restarts_exhausted`, `budget_exhausted`, `spawn_failed`
- [x] `transition(event, data=None) -> State` with typed
      `IllegalTransitionError`
- [x] `is_terminal()` for `DONE` / `ABORT`
- [x] FSM is **pure**: imports only stdlib (`dataclasses`, `enum`,
      `typing`, `__future__`); a static test in
      `test_state_machine.py::TestPurity` blocks `subprocess`,
      `httpx`, `requests`, `openai`, `anthropic`, `urllib`, `socket`,
      `asyncio`, agent / supervisor / watchdog / LLM / search / IPC /
      gatekeeper / router imports, and forbidden side-effect call
      sites (`open(`, `Path(`, `subprocess.`, `os.system`, `os.popen`,
      `tempfile.`)
- [x] Tracks `current_round`, `verdict_retry_count`,
      `last_missed_role`, `remembered_turn_state`, `max_rounds`
- [x] Happy path for 1 round
- [x] Happy path for 10 rounds
- [x] Illegal transitions rejected
- [x] `budget_exhausted` aborts from every non-terminal state
- [x] `spawn_failed` aborts from `SPAWNING`
- [x] `invalid_or_tie` retries once then escalates to `TIE_BREAK`
- [x] `TIE_BREAK` advances to `DONE`
- [x] `heartbeat_miss` from `PRO_TURN` / `CON_TURN` → `RECOVER`
      with `last_missed_role` and `remembered_turn_state` set
- [x] `respawned` returns to the remembered turn (counter preserved)
- [x] `restarts_exhausted` aborts from `RECOVER`
- [x] `is_terminal()` works for both `DONE` and `ABORT`

**Evidence — Stage 5**

- Files: `src/debate/orchestration/state_machine.py`.
- Tests: `tests/unit/test_state_machine.py` — 58 tests across
  construction, happy paths (1 + 10 rounds), illegal transitions,
  budget exhaustion (parametrized over 13 non-terminal states),
  spawn failure, verdict retry / tie-break, heartbeat recovery,
  `is_terminal`, and static purity checks.

---

## Stage 6 — Supervisor / child process manager

- [x] `src/debate/orchestration/supervisor.py`
- [x] `ChildProcess` dataclass with `role`, `process`, `stdin`,
      `stdout`, `stderr_path`, `start_time`, `restart_count`,
      plus internal `read_queue`, `reader_thread`, `stderr_fh`
- [x] `Supervisor.spawn(role)` accepts only `pro` / `con`
- [x] `Supervisor.send(role, message)` uses
      `debate.orchestration.ipc.serialize_message`; no manual JSON
- [x] `Supervisor.receive(role, timeout)` uses
      `debate.orchestration.ipc.deserialize_message`; timeout raises
      `ChildReceiveTimeoutError`; EOF raises `ChildStreamClosedError`
- [x] stdin / stdout pipes (`subprocess.PIPE`); per-child reader
      thread drains stdout into a `queue.Queue`
- [x] Stderr captured to per-role file:
      `pro_stderr.log` / `con_stderr.log` (underscores — Windows
      `CON` reserved-name fix)
- [x] `terminate(role)` closes stdin, tries `proc.terminate()`,
      hard-kills on `TimeoutExpired`, joins reader thread
- [x] `terminate_all()` and context-manager `__exit__`
- [x] `respawn(role)` increments `restart_count`, replaces the
      `Popen` and the `pid`
- [x] `build_child_env(role)` is an allow-list + deny-list of env
      vars; `SEARCH_API_KEY` is dropped explicitly and is not in the
      allow-list (defense in depth)
- [x] Supervisor does **not** import Pro/Con agent modules, the
      Judge module, or the Watchdog module
      (`test_supervisor.py::TestIPCBoundary::test_supervisor_module_does_not_import_agent_modules`
      and the import-grep in this audit confirm this)
- [x] Watchdog is not implemented (still a 4-line Stage 1
      placeholder at `src/debate/watchdog/watchdog.py`)
- [x] Judge flow is not implemented (still a 4-line Stage 1
      placeholder at `src/debate/judge/judge.py`)

**Evidence — Stage 6**

- Files: `src/debate/orchestration/supervisor.py`.
- Tests:
  - `tests/unit/test_supervisor.py` — 57 unit tests using
    `FakePopen` with real `os.pipe()` (role validation, spawn,
    send/receive, terminate-graceful + force-kill, terminate_all,
    respawn, env filtering, IPC boundary).
  - `tests/integration/test_supervisor_smoke.py` — 8 smoke tests
    that actually spawn `python tests/integration/echo_child.py`
    via the Supervisor and round-trip a real `Message`.
- Stage boundary verified: `src/debate/orchestration/` contains
  only `ipc.py` (Stage 2), `state_machine.py` (Stage 5), and
  `supervisor.py` (Stage 6) — no loop / scheduler / watchdog file.

---

## Stage 7 — BaseAgent + DebaterAgent + ProAgent + ConAgent

- [x] `src/debate/agents/base_agent.py`
  - [x] `BaseAgent` reads bytes from injectable stdin,
        writes bytes to injectable stdout
  - [x] Uses `debate.orchestration.ipc.serialize_message` and
        `deserialize_message`; never imports `json`
  - [x] Handles `ping` → `pong` (with `in_reply_to` for correlation)
  - [x] Handles `shutdown` by flipping `_running = False`
  - [x] Dispatches `init`, `prompt`, `tool_result` to `handle(msg)`
  - [x] Loop is defensive: `IPCError` → `_on_ipc_error`, handler
        exception → `_on_handler_error`; the loop continues
  - [x] EOF / closed pipe / `shutdown` → `run() → 0`
- [x] `src/debate/agents/debater_agent.py`
  - [x] `DebaterAgent(BaseAgent)`
  - [x] `STANCE: ClassVar[str]`; invalid / unset stance raises
        `TypeError` at construction
  - [x] State: `motion`, `max_tokens`, `opponent_last`,
        `selected_context`, `previous_tool_results`
  - [x] `_on_init` updates motion / context / max_tokens, rejects
        stance mismatch
  - [x] `_on_prompt` reads phase + `opponent_last`, generates and
        sends reply
  - [x] `_on_tool_result` appends to `previous_tool_results`
  - [x] `build_prompt(phase)` includes motion, stance, phase,
        opponent_last (only for argument / closing), selected
        context, recorded tool results, stance instruction
  - [x] `generate_reply(phase)` calls the injected `LLMClient`
        (`FakeLLMClient` in tests); never touches a real provider
  - [x] Reply payload is schema-valid and includes
        `phase`, `stance`, `content`, `tokens_in`, `tokens_out`
  - [x] `request_search(query)` emits a valid `tool_call` envelope
        with `payload={"tool": "search", "query": query}` and
        rejects empty / whitespace queries
  - [x] Does **not** import `SearchClient`, `FakeSearchClient`,
        `ToolRouter`, `Gatekeeper`, `Ledger`, `subprocess`,
        `requests`, `httpx`, `openai`, or `anthropic` anywhere in
        the agents package
- [x] `src/debate/agents/pro_agent.py` — `class ProAgent(DebaterAgent):
      STANCE = "pro"` (no extra methods or attributes)
- [x] `src/debate/agents/con_agent.py` — `class ConAgent(DebaterAgent):
      STANCE = "con"` (no extra methods or attributes)
- [x] `vars(ProAgent)` / `vars(ConAgent)` contains only `STANCE`
      among non-dunder names (enforced by test)

**Evidence — Stage 7**

- Files: `src/debate/agents/base_agent.py`,
  `src/debate/agents/debater_agent.py`,
  `src/debate/agents/pro_agent.py`,
  `src/debate/agents/con_agent.py`, `src/debate/agents/__init__.py`.
- Tests:
  - `tests/unit/test_base_agent.py` — 22 tests (construction,
    heartbeat ping→pong, shutdown, dispatch over routed types,
    malformed input swallowed by loop, outgoing JSONL wire format,
    multi-ping correctness).
  - `tests/unit/test_debater_agent.py` — 62 tests across subclass
    shape, prompt building, reply generation per phase, stance
    discipline, search `tool_call` emission, init / prompt /
    tool_result dispatch via the run loop, and static
    import-boundary assertions (`SearchClient`, `ToolRouter`,
    `Gatekeeper`, `subprocess`, `requests`, `httpx`, `openai` are
    forbidden in the agents package).

---

## Stage 8 — Watchdog / liveness monitor

- [x] `src/debate/orchestration/watchdog.py`
  - [x] `Watchdog` class with the contract specified in PRD:
        `__init__(supervisor, heartbeat_interval_sec,
        heartbeat_timeout_sec, on_miss, ...)`, `start()`, `stop()`,
        `check_once()`, `is_running` property
  - [x] Active `ping` / `pong` liveness probe per role (`pro` /
        `con`), going through the Stage 6 Supervisor's JSONL pipes
        - never serializes JSON itself
  - [x] Outgoing pings are real `Message` envelopes
        (`role=Role.JUDGE`, `type=MessageType.PING`, monotonic
        `turn_id`, payload `{"watchdog_ping_id": <turn_id>}`)
  - [x] Heartbeat-miss detection on: missing child, dead child,
        send failure, receive timeout, stream EOF, IPC error, any
        non-`PONG` reply (typed `MissReason` strings for logs)
  - [x] `on_miss(role)` callback is the **only** outward signal;
        Watchdog does not call `supervisor.respawn` and does not
        touch the FSM directly (Stage 9/10 will translate
        `on_miss` into `heartbeat_miss` / `respawned` /
        `restarts_exhausted` FSM events)
  - [x] Background daemon thread driven by `threading.Event` for
        clean cancellation; `start()` is idempotent;
        `stop(timeout=...)` joins; context-manager support
  - [x] Logger duck-typed (`RunLogger`-compatible
        `log(role, turn_id, event_type, **fields)`); ping / pong /
        miss events emitted on the `"watchdog"` channel; logger
        failures swallowed defensively
- [x] No imports from `debate.agents.*`, `debate.judge.*`, or
      `debate.orchestration.state_machine` (enforced by static
      `inspect.getsource(...)` checks in
      `tests/unit/test_watchdog.py::TestStageBoundary`)
- [x] Unit tests with fake supervisor / fake clock:
      `tests/unit/test_watchdog.py` — 42 tests covering happy
      pings for pro and con, every miss path
      (`no_child` / `child_not_alive` / `send_failed` / `timeout` /
      `stream_closed` / `ipc_error` / `not_a_pong` /
      `supervisor_error` / `unexpected_error`), determinism (role
      order, repeated calls), ping envelope shape and injected
      clock, threaded `start` / `stop` / idempotence /
      loop-survives-exception / context manager, logger event
      emission and logger-blowup containment, and the stage-boundary
      static checks
- [x] Lightweight chaos integration test
      (`tests/integration/test_recovery_chaos.py`) — spawns a
      tiny `heartbeat_child.py` (no `debate.*` imports, so no
      PYTHONPATH plumbing needed) via the real `Supervisor`,
      confirms a healthy ping/pong cycle records no miss, then
      `Supervisor.terminate("pro")` and confirms both
      `check_once()` and the background loop report the miss
- [x] The Stage 1 placeholder at `src/debate/watchdog/watchdog.py`
      is intentionally left in place (still 3 lines: docstring +
      `from __future__`). It is still unused by any import path;
      cleanup is queued for Stage 10 (see open finding #4)

**Evidence — Stage 8**

- Files: `src/debate/orchestration/watchdog.py`,
  `tests/unit/test_watchdog.py`,
  `tests/integration/test_recovery_chaos.py`,
  `tests/integration/heartbeat_child.py`.
- Public exports updated in
  `src/debate/orchestration/__init__.py` (`Watchdog`,
  `MissReason`, `OnMissCallback`,
  `DEFAULT_HEARTBEAT_INTERVAL_SEC`,
  `DEFAULT_HEARTBEAT_TIMEOUT_SEC`, `DEFAULT_ROLES`).
- Tests passing: 44 new Stage 8 tests (42 unit + 2 integration)
  on top of the 402 from Stages 1–7 = **446 passed in 3.07s**.



## Stage 9 — Judge debate flow + verdict pipeline

- [x] `src/debate/orchestration/judge.py`
  - [x] `Judge` class - parent / central controller; the only
        process that talks to Pro and Con. Children never
        communicate directly: every byte that reaches a child first
        passed through the Judge as a `Role.JUDGE` envelope. Tested
        by
        `test_judge_agent.py::TestRunDebate::test_pro_never_receives_con_envelope`
        which inspects every `supervisor.send` call in a full debate
        and asserts they all carry `role=Role.JUDGE`
  - [x] Constructor takes `supervisor`, `fsm`, `router`,
        `gatekeeper`, `llm_client`, optional `logger`,
        `motion`, `max_tokens_per_turn`, `per_turn_timeout_sec`,
        `receive_max_iters`, `clock` (matches the API requested by
        the spec)
  - [x] `run_debate(motion, rounds=None) -> Verdict` drives the FSM
        through every transition in
        `state_machine.py`: `START` → `CHILDREN_READY` → openings
        → `SENT_OPENINGS` → N × (`PRO_REPLY` → `SCORED` →
        `CON_REPLY` → `SCORED` → `SCORED`/`ROUND_LIMIT_REACHED`)
        → closings → `CLOSINGS_RECEIVED` → verdict pipeline
  - [x] `build_init(role, motion)` and `build_prompt(role, phase,
        context, opponent_last)` build `Message` envelopes; the
        Judge always sends as `Role.JUDGE`. `build_prompt` rejects
        being passed a `Message` as `opponent_last` (TypeError) so
        the only thing the other side ever sees is the *content
        string*, not the original envelope
  - [x] `run_turn(role, phase, opponent_last)` sends a prompt and
        receives one reply, servicing in-line `tool_call`
        iterations through `ToolRouter.call(...)` (Stage 9 added
        the `call` dispatcher with `UnknownToolError`); bounded by
        `DEFAULT_RECEIVE_MAX_ITERS` so a babbling child cannot
        burn budget forever
  - [x] `validate_child_reply(message, expected_role)` rejects:
        wrong sender role, wrong message type, empty / whitespace
        content, stance mismatch, invalid expected role - tested
        with one test per failure mode
  - [x] `score_turn(role, reply, round_number)` is deterministic
        and content-length-derived, capped so a single huge reply
        cannot dominate (covered by
        `TestScoring::test_score_capped`)
  - [x] `generate_verdict()` calls the LLM through the Gatekeeper
        and parses strict JSON (handles ```json fences and prose
        wrappers); failure is surfaced as `InvalidVerdictError`
        so the FSM `invalid_or_tie` retry path engages
  - [x] `validate_verdict(verdict)` enforces: winner ∈ {pro, con}
        (already schema-forbidden but double-checked), `scores`
        dict with both sides as numerics, ≥ `MIN_VERDICT_REASONS`
        (= 3) non-empty reason strings
  - [x] `apply_tie_breaker(scores)` — higher cumulative score
        wins; on exact tie `con` wins by deterministic rule;
        always returns a verdict that itself passes
        `validate_verdict`
  - [x] Verdict pipeline retries once on invalid output, then
        falls back to `apply_tie_breaker`; covered by
        `TestVerdictRetryPath` (`first_invalid_then_valid`,
        `two_invalid_triggers_tie_breaker`,
        `tie_break_with_equal_cumulative_picks_con`)
- [x] `tool_call` routing through Gatekeeper + ToolRouter (closes
      open finding #3)
  - [x] `src/debate/shared/router.py::ToolRouter.call(tool_name,
        ...)` — single typed dispatch entry point
  - [x] `UnknownToolError` (subclass of `ValueError`) for any
        tool name not in `KNOWN_TOOLS` (Stage 9 only knows
        `"search"`); `tool_result` payloads stamp
        `error="unknown_tool"`
  - [x] Children **cannot** call `SearchClient` directly: the
        Stage 6 supervisor env allow-list strips
        `SEARCH_API_KEY`, and the Stage 7 import-boundary tests
        confirm `debate.agents.*` does not import
        `debate.sdk.search_client` or `debate.shared.router`. The
        Judge is the only process that ever instantiates a
        `ToolRouter`
- [x] Logging via the duck-typed `RunLogger`-compatible logger
      (Judge events are written on the `"judge"` channel):
      `debate_started`, `children_spawned`, `init_sent`,
      `prompt_sent`, `reply_received`, `tool_call_received`,
      `tool_result_sent`, `score_recorded`, `verdict_llm_response`,
      `verdict_invalid`, `verdict_recorded`, `debate_done`,
      `turn_failed`, `spawn_failed`. Logger errors are swallowed
      defensively so a buggy logger cannot crash a debate. No
      secrets are emitted, and the existing
      `debate.shared.redaction` substring scrubber would catch any
      accidental sensitive key anyway
- [x] Stage boundary enforced statically:
  - [x] `judge.py` does not import `debate.agents.*` (verified by
        AST walk in `TestStageBoundary::test_judge_does_not_import_agent_modules`)
  - [x] `judge.py` does not import `subprocess`, `socket`,
        `httpx`, `requests`, or `urllib`
  - [x] `judge.py` does not call `json.dumps` (every outgoing
        envelope goes through the Stage 2 IPC helpers via
        `Supervisor.send`)
  - [x] `judge.py` does not touch `sys.stdin`/`sys.stdout`/
        `sys.stderr` directly
- [x] Verdict envelope is schema-validated; `Verdict.winner` is
      `Literal["pro", "con"]` so ties are flat-out impossible on
      the wire (Stage 2 invariant, exercised again by
      `test_judge_agent.py::TestVerdictParseAndValidate::test_tie_winner_rejected_at_schema`)

**Evidence — Stage 9**

- Files:
  - `src/debate/orchestration/judge.py` (new, 642 LOC after
    formatting)
  - `src/debate/orchestration/__init__.py` (re-exports `Judge`,
    `DebateHistory`, `TurnRecord`, `JudgeError`,
    `InvalidReplyError`, `InvalidVerdictError`, and the new
    Stage 9 default constants)
  - `src/debate/shared/router.py` (added `ToolRouter.call`,
    `UnknownToolError`, `KNOWN_TOOLS`, `SEARCH_TOOL_NAME`)
  - `src/debate/shared/__init__.py` (re-exports the new symbols)
- Tests:
  - `tests/unit/test_judge_agent.py` (new, 58 tests)
  - `tests/integration/test_judge_debate_flow.py` (new, 2 tests
    — offline 2-round debate writing a real `RunLogger`
    transcript to a tmp dir)
  - `tests/unit/test_router_cache.py::TestRouterCallDispatcher`
    (new, 9 tests covering the `ToolRouter.call` /
    `UnknownToolError` surface)
- Latest gate run (2026-05-31 23:24 UTC+3): **515 passed in 2.68s**
  (446 carried over from Stages 1–8 + 69 new Stage 9 tests),
  ruff `All checks passed!`, ruff format `66 files already
  formatted`
- Stage boundary verified: the Judge has no imports from
  `debate.agents.*`, no direct `subprocess` / `httpx` / `requests`
  imports, no `json.dumps`, no `sys.stdin`/`sys.stdout` access.
  The `src/debate/judge/judge.py` Stage 1 placeholder is still
  the unused 4-line stub (cleanup deferred to Stage 10 — see
  open finding #4).

## Stage 10 — End-to-end debate loop, transcript, CLI, polish (DONE)

- [x] `src/debate/main.py` rewritten as a real `argparse` CLI with
      `--motion`, `--rounds`, `--model`, `--seed`, `--fake` /
      `--no-fake`, `--config`, `--motions-file`, `--runs-root`,
      `--run-id`, `--replay`, `--quiet`, `--version`. `python -m
      debate.main` and `python -m debate` both reach the same
      entry point.
- [x] End-to-end debate runs on real `subprocess.Popen` children
      via `python -m debate.agents.{pro,con}_agent`, both of which
      use `FakeLLMClient`; the parent verdict-LLM is also a
      `FakeLLMClient` seeded with a canned valid verdict JSON.
- [x] `runs/<UTC-timestamp>/run.jsonl` is produced per run with
      every required event type
      (`cli_invoked`, `debate_started`, `children_spawned`,
      `init_sent`, `prompt_sent`, `reply_received`,
      `tool_call_received` / `tool_result_sent` when applicable,
      `score_recorded`, `verdict_llm_response`,
      `verdict_recorded`, `debate_done`, `cli_finished`). Each
      record carries `ts`, `role`, `turn_id`, `event_type`. Final
      `cli_finished` includes a Gatekeeper ledger snapshot.
- [x] Replay mode: `--replay <path>` reads only the saved
      `run.jsonl`, never instantiates `LLMClient` /
      `SearchClient` / `Supervisor`, prints per-event summaries,
      and exits with the recorded verdict.
- [x] Open finding #1 closed: `runs/.gitkeep` committed,
      `.gitignore` has `runs/*` + `!runs/.gitkeep`. Pinned by
      `tests/unit/test_housekeeping.py::TestRunsDir`.
- [x] Open finding #2 closed: `config/prompts/verdict.schema.json`
      mirrors the `Verdict` Pydantic model (winner enum
      `pro|con`; scores `0..100`; reasons `minItems = 3`;
      `additionalProperties: false` at the root). Pinned by
      `tests/unit/test_verdict_schema.py` (19 tests).
- [x] Open finding #3 stays closed (Stage 9). `ToolRouter.call` +
      `UnknownToolError` continue to be exercised by
      `tests/unit/test_router_cache.py::TestRouterCallDispatcher`.
- [x] Open finding #4 closed: all seven Stage 1 placeholder
      directories (`src/debate/{config,gatekeeper,ipc,judge,prompts,utils,watchdog}/`)
      were removed; `tests/unit/test_housekeeping.py::TestPlaceholderCleanup`
      pins both their absence and the presence of the real
      `src/debate/{agents,orchestration,sdk,shared}/` subpackages.
- [x] Final docs pass: `README.md`, `PROMPTS.md`, `docs/PRD_HW2.md`,
      `docs/PLAN_HW2.md`, and `docs/TODO_HW2.md` all rewritten to
      describe the actual Stage 10 surface.
- [~] Watchdog → FSM bridge (`Watchdog.on_miss` →
      `Event.HEARTBEAT_MISS` → orchestrated `respawn`) is **not
      wired** in the Stage 10 CLI loop. The Stage 8 Watchdog
      remains an injectable component with deterministic unit
      tests; the Stage 10 demo runs do not need recovery
      because the children are deterministic FakeLLMClient
      subprocesses. Wiring the bridge is intentionally deferred -
      it requires a recovery policy (max retries, escalation),
      which is beyond the HW2 scope.

**Evidence — Stage 10**

- Files (new):
  - `src/debate/main.py` (rewritten Stage 1 placeholder into a
    full CLI: `build_parser`, `replay`, `run_live`,
    `_build_judge_llm`, `_build_gatekeeper`, `_build_router`,
    `_build_runs_dir`, `_build_child_env`, `main`)
  - `config/prompts/verdict.schema.json` (Verdict JSON Schema mirror)
  - `runs/.gitkeep` (tracks the runs/ dir)
  - `tests/unit/test_cli.py` (25 tests)
  - `tests/unit/test_verdict_schema.py` (19 tests)
  - `tests/unit/test_housekeeping.py` (8 tests)
  - `tests/integration/test_e2e_debate.py` (6 tests)
- Files (changed):
  - `.gitignore` (added `runs/*` + `!runs/.gitkeep`)
  - `tests/test_smoke.py` (retargeted to the new CLI surface)
  - `tests/unit/test_watchdog.py` (`_FORBIDDEN_IMPORT_TOKENS`
    updated to point at the real Judge home and CLI module)
  - `README.md`, `PROMPTS.md`, `docs/PRD_HW2.md`,
    `docs/PLAN_HW2.md`, `docs/TODO_HW2.md`
- Files (removed):
  - `src/debate/config/` (settings.py + __init__.py)
  - `src/debate/gatekeeper/` (gatekeeper.py + __init__.py)
  - `src/debate/ipc/` (channel.py + messages.py + __init__.py)
  - `src/debate/judge/` (judge.py + __init__.py)
  - `src/debate/prompts/` (__init__.py)
  - `src/debate/utils/` (logger.py + __init__.py)
  - `src/debate/watchdog/` (watchdog.py + __init__.py)
- Latest gate run (2026-05-31 23:55 UTC+3): **574 passed in
  5.40s** (515 carried over + 59 new Stage 10 tests), ruff `All
  checks passed!`, ruff format `56 files already formatted`.
- Demo run (offline, fake LLM, real subprocess Pro/Con):
  ```
  uv run python -m debate.main \
      --motion "Is AI good for education?" --rounds 2 --fake
  ```
  → winner `pro`, scores `pro=50 con=40`, 3 reasons, 33-line
  parseable transcript at `runs/<UTC-ts>/run.jsonl`.
