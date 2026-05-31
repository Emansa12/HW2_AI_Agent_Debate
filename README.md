# HW2 - AI Agent Debate

A multi-agent debate system where a **Pro** agent and a **Con**
agent argue a single motion. The **Judge / Parent process** is the
central controller; Pro and Con are sandboxed child processes that
**never communicate directly** - every message between them is
routed through the Judge over JSONL IPC on stdin/stdout.

> **Status: Stage 11 done.** End-to-end debate runs from the
> terminal, writes a JSONL transcript under
> `runs/<timestamp>/run.jsonl`, supports replay, and ships with a
> JSON Schema for verdicts. Default mode is **fully offline** (fake
> LLM / fake search), so no API keys are required - that is the
> grading path. Stage 11 added **opt-in** real-provider clients
> (Tavily search + OpenAI-compatible LLM) behind ``--real-search``
> and ``--real-llm`` flags.

## Quick start

```bash
uv sync

# Run a fresh demo debate (offline, fake LLM, 2 rounds per side)
uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake

# Replay a previous transcript
uv run python -m debate.main --replay runs/<timestamp>/run.jsonl

# Tests + lint
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

### Optional: real-provider modes (Stage 11)

```bash
# Hybrid: real Tavily search, fake LLM (cheapest sanity check)
uv run python -m debate.main --motion "..." --rounds 2 --fake --real-search

# Full real-provider mode: real OpenAI-compatible LLM + real search
uv run python -m debate.main --motion "..." --rounds 2 --no-fake

# Equivalent to --no-fake, but explicit:
uv run python -m debate.main --motion "..." --rounds 2 --real-llm --real-search
```

Real modes read keys from the environment only (`LLM_API_KEY` /
`OPENAI_API_KEY` for the LLM, `SEARCH_API_KEY` /
`TAVILY_API_KEY` for search). See [Security](#security) below and
the comments in `.env-example`.

## HW2 assignment context

This homework asks for a **multi-agent debate** with at least two
agents (Pro and Con) and a Judge that decides the winner. The
project has the following hard requirements (see
`docs/PRD_HW2.md` for the full list):

- Pro and Con must run as **separate processes** and may not
  communicate directly.
- All inter-process traffic uses **JSONL** messages with a fixed
  Pydantic schema (see `src/debate/sdk/schemas.py`).
- A **Gatekeeper** must enforce per-turn / per-debate budgets on
  LLM and search calls.
- A **search tool** with a cache must be available to the
  debaters; the cache and budget are enforced by the parent.
- A **Watchdog** must detect dead / stuck children.
- The final **Verdict** must pick one side (`pro` or `con`) - tie
  is forbidden by the schema.
- A complete debate transcript must be written to disk as JSONL.

## Architecture

```
                +------------------------+
                |   debate.main (CLI)    |
                |  --motion / --rounds   |
                |  --replay / --quiet    |
                +-----------+------------+
                            |
                            v
            +-------------------------------+
            |     Judge  (parent process)   |
            |  - drives DebateStateMachine  |
            |  - validates every message    |
            |  - routes tool_call -> Router |
            |  - generates + validates      |
            |    verdict (retry + tie-break)|
            +---+--------+--------+---------+
                |        |        |
                v        v        v
            Supervisor  Router  Gatekeeper
                |          |          |
   +------------+          v          v
   |   spawns          search      ledger
   |   real            (cached)    (tokens / USD / RPM)
   v   subprocesses
+--------+   +--------+
| Pro    |   | Con    |  <-- BaseAgent + DebaterAgent
| agent  |   | agent  |      (FakeLLMClient by default)
+--------+   +--------+
   |             |
   +--JSONL IPC--+
        (stdin/stdout, never to each other)

      Watchdog -> ping/pong via Supervisor
      RunLogger -> runs/<id>/run.jsonl
```

### Component roles

| Component | Module | Responsibility |
|-----------|--------|----------------|
| **Judge** | `debate.orchestration.judge` | Parent / central controller. Spawns children, alternates Pro/Con turns, validates replies, routes tool calls, scores turns, generates the final verdict, applies the deterministic tie-breaker. |
| **Pro / Con agents** | `debate.agents.{pro,con}_agent` + `debate.agents.debater_agent` | Child subprocesses. Receive `prompt` / `tool_result` / `ping`, reply with `argument` / `tool_call` / `pong`. Stance-only subclass on top of `DebaterAgent`. |
| **Supervisor** | `debate.orchestration.supervisor` | Owns the JSONL stdin/stdout pipes for each child. `spawn` / `send` / `receive` / `terminate` / `respawn`. Filters env to a strict allow-list (no `SEARCH_API_KEY` ever). |
| **State machine** | `debate.orchestration.state_machine` | Pure FSM. Drives the legal sequence of debate states (init -> openings -> rounds -> closings -> verdict). |
| **Watchdog** | `debate.orchestration.watchdog` | Liveness monitor. Sends `ping`, expects `pong`, calls `on_miss(role)` if the child is unresponsive. Does not own the recovery policy. |
| **JSONL IPC** | `debate.orchestration.ipc` | `serialize_message` / `deserialize_message`. Length-checked, schema-validated. |
| **Gatekeeper** | `debate.shared.gatekeeper` | Budget gate. Enforces tokens/turn, tokens/debate, USD/debate, RPM. Updates a structured `Ledger`. Wraps every LLM and search call. |
| **ToolRouter** | `debate.shared.router` | Single dispatch surface for tool calls (`call(tool_name, **kw)`). Currently knows only `search`. Wraps a `SearchClient` with an LRU cache. Raises `UnknownToolError` for any other tool. |
| **RunLogger** | `debate.shared.logger` | Structured JSONL logger. One record per event. Stamped with `ts`, `role`, `turn_id`, `event_type`. Redacts known secret patterns. |
| **DebateConfig** | `debate.shared.config` | Loads `config/debate.json` and `config/motions.json`. Numeric-bounds-validated Pydantic model. |
| **CLI** | `debate.main` | `argparse` wrapper that wires every component above and writes the per-run transcript directory. |

### Verdict rules (Stage 9)

The Stage 9 verdict pipeline is strict:

1. The Judge calls the LLM once for a verdict.
2. The response is parsed as JSON. If parsing or validation fails,
   the Judge **retries once**.
3. If the second attempt is still invalid (or the LLM tries to
   declare a tie), the Judge applies a **deterministic tie-break**:
   the side with the higher cumulative score wins; if cumulative
   scores are exactly equal, **Con** wins.
4. The final `Verdict` is logged as `verdict_recorded` and
   immediately followed by `debate_done`.

The `winner` field is constrained at the schema level
(`Literal["pro", "con"]`); ties cannot survive even a malicious
LLM response.

## Project layout

```
HW2_AI_Agent_Debate/
├── pyproject.toml             # uv / pytest / ruff config
├── README.md                  # this file
├── PROMPTS.md                 # authoritative agent prompts + verdict contract
├── .env-example               # placeholders only, NEVER real keys
├── .gitignore                 # ignores runs/* but keeps runs/.gitkeep
├── config/
│   ├── debate.json            # default DebateConfig (10 rounds, budgets, timeouts)
│   ├── motions.json           # bundled debate motions
│   └── prompts/
│       └── verdict.schema.json  # JSON Schema mirror of Verdict + validate_verdict
├── docs/
│   ├── PRD_HW2.md             # product requirements (hard / soft)
│   ├── PLAN_HW2.md            # architecture + per-stage plan
│   └── TODO_HW2.md            # per-stage checklist with evidence
├── runs/
│   └── .gitkeep               # directory tracked, contents ignored
├── src/
│   └── debate/
│       ├── __init__.py
│       ├── __main__.py        # `python -m debate` -> debate.main:main
│       ├── main.py            # CLI / end-to-end wiring (Stage 10)
│       ├── sdk/               # public wire schemas + LLM/Search clients
│       ├── shared/            # config, gatekeeper, router, logger, redaction
│       ├── orchestration/     # judge, supervisor, state machine, watchdog, ipc
│       └── agents/            # base_agent, debater_agent, pro_agent, con_agent
└── tests/
    ├── conftest.py
    ├── test_smoke.py
    ├── unit/                  # pure / fast tests (~520 tests)
    └── integration/           # subprocess + e2e tests (~50 tests)
```

## Requirements

- Python **>= 3.11**
- [uv](https://docs.astral.sh/uv/) for environment + dependency management
- `pytest` for tests, `ruff` for lint / format

## Setup

```bash
# 1. Install uv (see https://docs.astral.sh/uv/)
# 2. Create the venv and install dev deps
uv sync

# 3. Optional - copy the env template (no real keys required to run)
cp .env-example .env       # PowerShell:  Copy-Item .env-example .env
```

## Running a debate

The CLI lives in `debate.main` (also reachable as `python -m debate`).

```bash
# Default - fake LLM + fake search, 10 rounds per side, motion from config/motions.json
uv run python -m debate.main

# Custom motion, 2 rounds, fully offline
uv run python -m debate.main --motion "Is AI good for education?" --rounds 2 --fake

# Replay a saved transcript (no LLM, no search, no subprocess spawn)
uv run python -m debate.main --replay runs/<timestamp>/run.jsonl

# Quiet mode (suppresses banner + summary; transcript is still written)
uv run python -m debate.main --rounds 2 --quiet
```

### Useful flags

| Flag | Default | Description |
|------|---------|-------------|
| `--motion <text>` | first entry of `config/motions.json` | Debate topic |
| `--rounds <int>` | `DebateConfig.rounds` (10) | Argument rounds per side, capped at 100 |
| `--model <id>` | `fake` | LLM model identifier. Used when `--real-llm` is set; passed through to RealLLMClient. |
| `--seed <int>` | unset | Optional Python `random.seed` for reproducibility |
| `--fake` | on | Use offline FakeLLMClient + FakeSearchClient. **Default.** |
| `--no-fake` | off | Shorthand for `--real-llm --real-search`. Requires both API keys. |
| `--real-search` | off | Stage 11: use Tavily-backed `RealSearchClient`. Requires `SEARCH_API_KEY` (or `TAVILY_API_KEY`). Combinable with `--fake`. |
| `--real-llm` | off | Stage 11: use OpenAI-compatible `RealLLMClient` for Judge + Pro/Con. Requires `LLM_API_KEY` (or `OPENAI_API_KEY`). |
| `--config <path>` | `config/debate.json` | Override DebateConfig location |
| `--motions-file <path>` | `config/motions.json` | Override motions file |
| `--runs-root <path>` | `runs/` | Where the per-run directory is created |
| `--run-id <id>` | UTC timestamp | Force a specific run id |
| `--replay <path>` | unset | Replay a saved `run.jsonl` and exit |
| `--quiet` | off | Suppress banner / summary output |
| `--version` | - | Print package version |

### Transcript

Every live run writes:

```
runs/<run_id>/
├── run.jsonl            # JSONL event log (one JSON object per line)
├── pro_stderr.log       # stderr captured from the Pro subprocess
└── con_stderr.log       # stderr captured from the Con subprocess
```

`run.jsonl` records contain at minimum `ts`, `role`, `turn_id`, and
`event_type`. The required event types are:

`cli_invoked`, `debate_started`, `children_spawned`, `init_sent`,
`prompt_sent`, `reply_received`, `tool_call_received` (when used),
`tool_result_sent` (when used), `score_recorded`,
`verdict_llm_response`, `verdict_recorded`, `debate_done`,
`cli_finished` (with the gatekeeper ledger snapshot).

Replay mode (`--replay`) reads only this file - it never spawns a
subprocess and never imports `LLMClient` or `SearchClient`.

## Testing

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

The full suite is **fake / offline by default**. Integration tests
spawn real Pro/Con subprocesses but those subprocesses use
`FakeLLMClient`, so no network and no API keys are required.

## Security

- `.env` is in `.gitignore`. `.env-example` ships with **empty**
  placeholders only - tests pin this with a regex sweep for
  `sk-…`, `AKIA…`, and Google API key shapes
  (see `tests/unit/test_housekeeping.py`).
- API keys are read from the **environment only**. They never
  appear in source, config, prompts, transcripts, or logs.
  `RunLogger` redacts known secret patterns (`sk-…`, `AKIA…`,
  Google keys, JWT-shaped tokens) before writing to disk.
- Real-provider clients (Stage 11) only ever pass the key as the
  `Authorization: Bearer …` request header to `httpx`. The key is
  never embedded in URLs, request bodies, or error messages.
- The `Supervisor` filters the child env to an explicit allow-list
  AND applies a deny-list. Pro and Con processes never see any
  search key (`SEARCH_API_KEY` / `TAVILY_API_KEY` /
  `BRAVE_SEARCH_API_KEY` / `SERPAPI_API_KEY` are blocked) - search
  is **always** brokered by the parent's Judge → ToolRouter →
  Gatekeeper.
- The default demo and the entire test suite never require any
  real API key. Real-provider tests use `httpx.MockTransport` for
  synthetic responses, so CI works offline.

## Current limitations

- Stage 11 shipped real clients for **search** (Tavily) and the
  **LLM** (OpenAI-compatible Chat Completions). Both default to
  off; the full pipeline still works on `FakeLLMClient` /
  `FakeSearchClient` for the grading run.
- The OpenAI-compatible RealLLMClient was tested against the
  Chat Completions JSON schema only. Streaming responses, tool
  calls, and JSON-mode are not used by the Judge prompt; if you
  swap in a provider that requires those, you may need a small
  client subclass.
- `--seed` only seeds the `random` module. `FakeLLMClient` is
  already deterministic, and `RealLLMClient`'s determinism
  depends entirely on the upstream provider's `temperature`
  (default 0.2). The seed is recorded in the transcript for
  reproducibility book-keeping.
- `--real-llm` swaps the LLM for both the Judge AND the Pro/Con
  subprocesses (so the children also call the real provider).
  This obviously costs money and goes through the Gatekeeper
  budget; set the budgets in `config/debate.json` accordingly.

## Documentation

- [`docs/PRD_HW2.md`](docs/PRD_HW2.md) - hard requirements,
  architecture, protocol, runtime defaults, success criteria.
- [`docs/PLAN_HW2.md`](docs/PLAN_HW2.md) - architecture diagram,
  component responsibilities, per-stage execution plan, design
  notes.
- [`docs/TODO_HW2.md`](docs/TODO_HW2.md) - per-stage checklist
  with evidence.
- [`PROMPTS.md`](PROMPTS.md) - authoritative agent prompts +
  verdict JSON contract.
