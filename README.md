# HW2 - AI Agent Debate

A multi-agent debate system where a **Pro** agent and a **Con**
agent argue a single topic. The **Judge / Parent Process** is the
central controller; Pro and Con are sandboxed child processes that
**never communicate directly** - every message between them is
routed through the Judge side over JSONL IPC.

Inside the Judge side: a **Gatekeeper** gates all LLM and search
calls, a **ToolRouter** handles search and cache, a **Supervisor**
owns the JSONL pipes to each child, a **Watchdog** handles per-turn
/ total timeouts and child recovery, and a **Logger** writes the
full transcript.

The default debate is **10 turns per side**; every run produces a
single JSONL transcript under `runs/<timestamp>/run.jsonl` whose
last record is a `verdict` message with `winner in {"pro","con"}`
(**ties are forbidden by the schema**).

> **Status: Stage 2 done.** The wire schemas and JSONL IPC helpers
> are implemented and unit-tested. Pro / Con agents, Gatekeeper,
> ToolRouter, Watchdog, Supervisor, and Judge logic are not yet
> wired up.

## Project layout

```
HW2_AI_Agent_Debate/
├── pyproject.toml         # uv / pytest / ruff config
├── README.md              # this file
├── PROMPTS.md             # prompt drafts
├── .env-example
├── .gitignore
├── docs/
│   ├── PRD_HW2.md         # product requirements
│   ├── PLAN_HW2.md        # architecture + per-stage plan
│   └── TODO_HW2.md        # granular per-stage checklist
├── src/
│   └── debate/            # the only package
│       ├── __init__.py
│       ├── __main__.py    # enables `python -m debate`
│       ├── main.py        # entry point
│       ├── sdk/           # public wire schemas (Stage 2)
│       │   └── schemas.py
│       ├── orchestration/ # JSONL IPC helpers (Stage 2)
│       │   └── ipc.py
│       ├── agents/        # pro_agent.py, con_agent.py (placeholders)
│       ├── gatekeeper/    # placeholder
│       ├── watchdog/      # placeholder
│       ├── judge/         # placeholder
│       ├── ipc/           # legacy stub (will be removed)
│       ├── config/        # placeholder
│       ├── prompts/       # placeholder
│       └── utils/         # placeholder
└── tests/
    ├── conftest.py
    ├── test_smoke.py
    └── unit/
        ├── test_schemas.py
        └── test_ipc.py
```

## Requirements

- Python **>= 3.11**
- [uv](https://docs.astral.sh/uv/) for environment + dependency
  management
- `pytest` for tests
- `ruff` for lint / format

## Setup

```bash
# 1. Install uv (see https://docs.astral.sh/uv/)
# 2. Install deps (creates .venv, installs dev group too)
uv sync

# 3. Copy environment template (real keys not required yet)
cp .env-example .env     # PowerShell:  Copy-Item .env-example .env
```

## Run

```bash
uv run python -m debate.main
```

This is the final user-facing command for every stage. Today it
prints a Stage 1 banner; later stages will run the full debate.

Expected output today:

```
======================================================================
  HW2 - AI Agent Debate
  Stage 1 skeleton  -  no debate logic implemented yet.
  Version: 0.1.0
======================================================================
Stage 1 OK: project skeleton is in place.
Next stages will add: Pro/Con agents, Gatekeeper, Watchdog, Judge, IPC.
```

## Planned CLI (later stages)

Future flags will look approximately like:

```bash
uv run python -m debate.main \
    --topic "Should AI-generated content require labeling?" \
    --turns-per-side 10 \
    --model gpt-4o-mini
```

These flags are **not** implemented yet. Default cadence will be
10 turns per side, and each run will write
`runs/<UTC-timestamp>/run.jsonl`.

## Test

```bash
uv run pytest -q
```

## Lint and format

```bash
uv run ruff check .
uv run ruff format --check .
```

## Documentation

- [`docs/PRD_HW2.md`](docs/PRD_HW2.md) - hard requirements,
  architecture, protocol, runtime defaults, success criteria.
- [`docs/PLAN_HW2.md`](docs/PLAN_HW2.md) - architecture diagram,
  component responsibilities, per-stage execution plan, design notes.
- [`docs/TODO_HW2.md`](docs/TODO_HW2.md) - granular per-stage
  checklist.
- [`PROMPTS.md`](PROMPTS.md) - draft prompts for Pro / Con /
  Gatekeeper / Judge.
