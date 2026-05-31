# TODO — HW2: AI Agent Debate

> **This file (`docs/TODO_HW2.md`) is the authoritative current
> project status.** It is kept in sync with the repository by
> file / test / runtime evidence — not by intent alone.
>
> See [`PLAN_HW2.md`](PLAN_HW2.md) for architecture and rationale,
> and [`PRD_HW2.md`](PRD_HW2.md) for the high-level brief.
>
> Legend: `[x]` verified done · `[~]` partial (note on line) · `[ ]` not started

---

## Authoritative status summary

| Item | Status |
|------|--------|
| Stages 1–11 | **Verified DONE** (see stage table below) |
| Default mode | **Fake / offline** — `FakeLLMClient` + `FakeSearchClient`; no API keys required |
| Real providers | **Optional, opt-in** — `--real-search` (Tavily), `--real-llm` (OpenAI-compatible), or `--no-fake` (both) |
| Run command | `uv run python -m debate.main` |
| Package layout | `src/debate/` only (`agents/`, `orchestration/`, `sdk/`, `shared/`) |

**Latest gate run** (Monday 2026-06-01, UTC+3 — after Stage 11):

```
uv sync                       Resolved 21 packages / Checked 21 packages
uv run pytest -q              630 passed in ~5.7s
uv run ruff check .           All checks passed!
uv run ruff format --check .  60 files already formatted
uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake
                              winner=pro, scores=pro=50/con=40, transcript runs/<ts>/run.jsonl
```

---

## Global Definition of Done

All items below are verified as of Stage 11:

- [x] `uv sync` succeeds
- [x] `uv run pytest -q` succeeds (630 tests)
- [x] `uv run ruff check .` succeeds
- [x] `uv run ruff format --check .` succeeds
- [x] Fake 2-round demo succeeds (`--motion "…" --rounds 2 --fake`)
- [x] Default 10-round config exists (`config/debate.json`, `rounds: 10`)
- [x] `runs/<timestamp>/run.jsonl` transcript is produced per live run
- [x] Verdict winner is `pro` or `con` only (ties forbidden by schema + Judge tie-break)
- [x] Replay works (`--replay runs/<timestamp>/run.jsonl`, no LLM/search/subprocess)
- [x] No real API keys committed (`.env` gitignored; `.env-example` has empty placeholders)
- [x] README / PROMPTS / PRD / PLAN / TODO updated for Stage 11
- [x] Optional real search / LLM support documented and tested offline (`httpx.MockTransport`)

---

## Requirement Coverage Summary

| Requirement | Implemented in | Evidence |
|-------------|----------------|----------|
| Judge parent process | Stage 9 | `src/debate/orchestration/judge.py`; `tests/unit/test_judge_agent.py` (58 tests) |
| Pro child process | Stage 7 | `src/debate/agents/pro_agent.py`; `tests/unit/test_debater_agent.py` |
| Con child process | Stage 7 | `src/debate/agents/con_agent.py`; `tests/unit/test_debater_agent.py` |
| JSONL IPC | Stage 2 | `src/debate/orchestration/ipc.py`; `tests/unit/test_ipc.py` |
| Gatekeeper | Stage 4 | `src/debate/shared/gatekeeper.py`; `tests/unit/test_gatekeeper.py` |
| ToolRouter / search cache | Stage 4, 9 | `src/debate/shared/router.py` (`.search`, `.call`, LRU cache); `tests/unit/test_router_cache.py` |
| Watchdog | Stage 8 | `src/debate/orchestration/watchdog.py`; `tests/unit/test_watchdog.py` (42 tests) |
| Supervisor | Stage 6 | `src/debate/orchestration/supervisor.py`; `tests/unit/test_supervisor.py` + integration smoke |
| FSM | Stage 5 | `src/debate/orchestration/state_machine.py`; `tests/unit/test_state_machine.py` (58 tests) |
| Structured logs | Stage 3, 10 | `src/debate/shared/logger.py`; transcript events in `tests/integration/test_e2e_debate.py` |
| No direct Pro/Con communication | Stage 9 | Judge mediates all envelopes; `test_judge_agent.py::test_pro_never_receives_con_envelope` |
| Non-tie verdict | Stage 2, 9 | `Verdict.winner ∈ {pro, con}`; retry + `apply_tie_breaker`; `test_verdict_schema.py` |
| Replay mode | Stage 10 | `debate.main --replay`; `tests/unit/test_cli.py`, `tests/integration/test_e2e_debate.py` |
| Fake / offline mode | Stage 4, 10 | `FakeLLMClient`, `FakeSearchClient`; default `--fake`; full suite needs no keys |
| Optional real search API | Stage 11 | `RealSearchClient` (Tavily); `--real-search`; `tests/unit/test_real_search_client.py` (23 tests) |
| Optional real LLM API | Stage 11 | `RealLLMClient` (OpenAI-compatible); `--real-llm`; `tests/unit/test_real_llm_client.py` (22 tests) |
| Tests | Stages 1–11 | 630 tests; unit + integration; offline by default |
| Docs | Stages 1–11 | `README.md`, `PROMPTS.md`, `docs/PRD_HW2.md`, `docs/PLAN_HW2.md`, this file |

---

## Stage progress (verified)

| Stage | Scope | Status |
|-------|-------|--------|
| 1 | Project skeleton, configs, docs | DONE |
| 2 | Pydantic schemas + JSONL IPC helpers | DONE |
| 3 | Config + secrets + structured logging + redaction | DONE |
| 4 | Fake LLM/Search + Ledger + Gatekeeper + ToolRouter | DONE |
| 5 | Pure deterministic debate state machine (FSM) | DONE |
| 6 | Supervisor / child process manager | DONE |
| 7 | BaseAgent, DebaterAgent, ProAgent, ConAgent | DONE |
| 8 | Watchdog / liveness monitor | DONE |
| 9 | Judge debate flow + verdict pipeline | DONE |
| 10 | End-to-end debate loop, transcript, CLI, polish | DONE |
| 11 | Optional real-provider clients (Tavily + OpenAI-compatible LLM) | DONE |

**Closed audit findings (Stage 10):** `runs/.gitkeep` + `.gitignore` rules;
`config/prompts/verdict.schema.json`; `ToolRouter.call` + `UnknownToolError` (Stage 9);
Stage 1 placeholder directories removed. Pinned by `tests/unit/test_housekeeping.py`,
`tests/unit/test_verdict_schema.py`, `tests/unit/test_router_cache.py`.

---

## Stage 1 — Project skeleton

- [x] `pyproject.toml` (uv + pytest + ruff; PEP 735 `[dependency-groups].dev`)
- [x] `src/debate/` package layout, importable
- [x] `uv run python -m debate.main` CLI entry point exits 0
- [x] `tests/test_smoke.py` (version, `--help`, `--replay` paths)
- [x] `README.md`, `PROMPTS.md`, `.env-example`, `.gitignore`
- [x] `config/debate.json`, `config/motions.json`
- [x] `docs/PRD_HW2.md`, `docs/PLAN_HW2.md`, `docs/TODO_HW2.md`
- [x] `tests/unit/` and `tests/integration/` directories
- [x] `pro_agent.py` / `con_agent.py` (Windows `CON` reserved-name fix)
- [x] Only `src/debate/` is the application package (repo-wide grep)
- [x] `runs/.gitkeep`, `.gitignore` `runs/*` + `!runs/.gitkeep` (Stage 10)
- [x] `config/prompts/verdict.schema.json` (Stage 10)

**Evidence:** `pyproject.toml`, `src/debate/main.py`, `tests/test_smoke.py`.

---

## Stage 2 — Pydantic schemas + JSONL IPC

- [x] `src/debate/sdk/schemas.py` — `Role`, `MessageType`, `Phase`, `Message`, `Verdict`
- [x] `Verdict.winner ∈ {pro, con}` (ties forbidden)
- [x] `src/debate/orchestration/ipc.py` — serialize/deserialize, size cap, error hierarchy
- [x] Rejects invalid role/type/version, oversize lines, embedded newlines

**Evidence:** `tests/unit/test_schemas.py`, `tests/unit/test_ipc.py`.

---

## Stage 3 — Config + secrets + logging + redaction

- [x] `DebateConfig` + `Motions` loaders (`src/debate/shared/config.py`)
- [x] `Secrets` + `load_secrets()` from env only (`secrets.py`)
- [x] `RunLogger` → `runs/<run_id>/run.jsonl` with redaction on write (`logger.py`)
- [x] Secret-pattern redaction (`redaction.py`); `.env` gitignored

**Evidence:** `tests/unit/test_config.py`, `test_secrets.py`, `test_logger.py`, `test_redaction.py`.

---

## Stage 4 — Fake LLM/Search + Gatekeeper + Ledger + ToolRouter

- [x] `LLMClient` / `FakeLLMClient` (`sdk/llm_client.py`)
- [x] `SearchClient` / `FakeSearchClient` with sanitization caps (`sdk/search_client.py`)
- [x] `Ledger` + `Gatekeeper` budget enforcement (`shared/ledger.py`, `gatekeeper.py`)
- [x] `ToolRouter` with LRU cache (`shared/router.py`)
- [x] Unknown-tool dispatch — **closed in Stage 9** via `ToolRouter.call()` + `UnknownToolError`
- [x] No real HTTP required by Stage 4 tests

**Evidence:** `tests/unit/test_llm_client.py`, `test_search_client.py`, `test_gatekeeper.py`, `test_router_cache.py`.

---

## Stage 5 — Pure deterministic debate state machine

- [x] `DebateStateMachine` — 15 states, typed events, `IllegalTransitionError`
- [x] Pure FSM (no I/O); static purity test in `test_state_machine.py`
- [x] Happy paths (1 + 10 rounds), illegal transitions, budget abort, verdict retry/tie-break, heartbeat recovery edges

**Evidence:** `tests/unit/test_state_machine.py` (58 tests).

---

## Stage 6 — Supervisor / child process manager

- [x] `Supervisor` — spawn/send/receive/terminate/respawn/terminate_all
- [x] JSONL IPC via Stage 2 helpers; per-role stderr to `pro_stderr.log` / `con_stderr.log`
- [x] Env allow-list + deny-list (search keys stripped; extended in Stage 11)
- [x] Does not import agent, Judge, or Watchdog modules
- [x] Stage 1 placeholder dirs removed in Stage 10; live code lives under `orchestration/`, `agents/`, `sdk/`, `shared/`

**Evidence:** `tests/unit/test_supervisor.py` (57 tests), `tests/integration/test_supervisor_smoke.py` (8 tests).

---

## Stage 7 — BaseAgent + DebaterAgent + ProAgent + ConAgent

- [x] `BaseAgent` — stdin/stdout JSONL loop, ping→pong, shutdown, dispatch
- [x] `DebaterAgent` — prompt→reply, `tool_call` emission, injected `LLMClient`
- [x] `ProAgent` / `ConAgent` — stance-only subclasses
- [x] Agents do not import SearchClient, ToolRouter, Gatekeeper, or HTTP libs

**Evidence:** `tests/unit/test_base_agent.py` (22 tests), `test_debater_agent.py` (62 tests).

---

## Stage 8 — Watchdog / liveness monitor

- [x] `Watchdog` — ping/pong via Supervisor, `on_miss(role)` callback only
- [x] Does not call `respawn` or touch FSM directly (recovery policy deferred — see Known Limitations)
- [x] Unit tests (42) + chaos integration test (`test_recovery_chaos.py`)
- [x] Real implementation at `orchestration/watchdog.py` (Stage 1 placeholder removed in Stage 10)

**Evidence:** `tests/unit/test_watchdog.py`, `tests/integration/test_recovery_chaos.py`.

---

## Stage 9 — Judge debate flow + verdict pipeline

- [x] `Judge` — full debate loop, FSM-driven, mediates all Pro/Con traffic
- [x] `run_turn` routes `tool_call` through `ToolRouter.call()` + Gatekeeper
- [x] Verdict pipeline: LLM → parse → validate → retry once → deterministic tie-break
- [x] Logging: `debate_started` … `verdict_recorded` … `debate_done`
- [x] Stage boundary: no agent imports, no direct subprocess/HTTP/stdio

**Evidence:** `tests/unit/test_judge_agent.py` (58 tests), `tests/integration/test_judge_debate_flow.py` (2 tests), `test_router_cache.py::TestRouterCallDispatcher` (9 tests).

---

## Stage 10 — End-to-end debate loop, transcript, CLI, polish

- [x] `debate.main` CLI — `--motion`, `--rounds`, `--model`, `--seed`, `--fake`, `--replay`, etc.
- [x] End-to-end run with real subprocess children + `FakeLLMClient`
- [x] `runs/<UTC-timestamp>/run.jsonl` with required event types + ledger snapshot
- [x] Replay mode (read-only, no LLM/search/subprocess)
- [x] All four open audit findings closed (see summary above)
- [~] Watchdog → FSM bridge not wired in CLI loop (Watchdog is tested; recovery policy deferred)

**Evidence:** `tests/unit/test_cli.py` (25+), `test_verdict_schema.py` (19), `test_housekeeping.py` (8), `tests/integration/test_e2e_debate.py` (6).

---

## Stage 11 — Optional Real API Support (opt-in)

| Capability | Status | Tests |
|------------|--------|-------|
| Real Search (Tavily) | DONE | `tests/unit/test_real_search_client.py` (23) |
| Real LLM (OpenAI-compatible) | DONE | `tests/unit/test_real_llm_client.py` (22) |
| Default mode | unchanged: `--fake` | Full suite offline, no API keys |

- [x] `RealSearchClient` — Tavily, `SEARCH_API_KEY` / `TAVILY_API_KEY`, typed errors
- [x] `RealLLMClient` — OpenAI-compatible Chat Completions, `LLM_API_KEY` / `OPENAI_API_KEY`
- [x] CLI: `--real-search`, `--real-llm`, `--no-fake` (shorthand for both); `--fake` remains default
- [x] Supervisor deny-list extended; Pro/Con never see search keys
- [x] Child `__main__` blocks use `RealLLMClient` only when `DEBATE_REAL_LLM=1`
- [x] `httpx>=0.27.0`; all real-client tests use `httpx.MockTransport` (no live network)
- [x] Gatekeeper + ToolRouter still wrap every real call

**Evidence:** files listed in Stage 11 gate run; 56 new tests on top of Stage 10's 574.

**Optional real-mode commands** (run only when keys exist; not required for grading):

```bash
# Hybrid — real search, fake LLM
uv run python -m debate.main --motion "…" --rounds 2 --fake --real-search

# Full real-provider mode
uv run python -m debate.main --motion "…" --rounds 2 --no-fake
```

---

## Known Limitations

- **Fake mode is the default** for stable grading, demos, and CI. No API keys are required.
- **Real providers are opt-in** and require environment keys (`SEARCH_API_KEY` / `TAVILY_API_KEY` for search; `LLM_API_KEY` / `OPENAI_API_KEY` for LLM).
- **Real-provider unit tests use mocked HTTP** (`httpx.MockTransport`), not live network calls. Passing pytest does not prove a paid provider works — only that the client wiring, parsing, and error paths are correct.
- **Watchdog → FSM recovery bridge is not wired** in the Stage 10/11 CLI loop. The Watchdog component exists, is unit-tested, and fires `on_miss(role)`, but the CLI does not translate misses into FSM `heartbeat_miss` / orchestrated `respawn` events. Demo runs use deterministic fake children and do not need recovery.
- **Replay is read-only** — it pretty-prints a saved `run.jsonl` and surfaces the recorded verdict. It does not re-run LLM/search calls, re-validate scores, or spawn subprocesses.
- **`--seed`** seeds Python's `random` module only. `FakeLLMClient` is constructor-deterministic; `RealLLMClient` determinism depends on the upstream provider.

---

## Final Acceptance Checklist

Run these before submission:

- [ ] `uv sync`
- [ ] `uv run pytest -q` (expect 630 passed)
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] Fake demo:
      `uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake`
- [ ] *(Optional, if `SEARCH_API_KEY` or `TAVILY_API_KEY` is set)*
      `uv run python -m debate.main --motion "…" --rounds 2 --fake --real-search`
- [ ] *(Optional, if both LLM and search keys are set)*
      `uv run python -m debate.main --motion "…" --rounds 2 --no-fake`
- [ ] `git status` — no unintended files staged; `.env` and `runs/<id>/` artifacts not tracked
- [ ] GitHub remote has the latest commit pushed
