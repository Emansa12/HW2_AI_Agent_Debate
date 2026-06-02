# PROMPTS

This file is the **authoritative source** for the system / role
prompts and the wire contracts used by every agent in the debate.

The Stage 9 / 10 architecture forbids direct Pro<->Con
communication: every message is mediated by the Judge over JSONL
IPC (see `src/debate/orchestration/judge.py` and
`src/debate/orchestration/supervisor.py`). The prompts below are
written with that constraint in mind - debaters only ever address
the Judge, never the other side.

> Convention: `{topic}`, `{round}`, `{history}`, `{opponent_last}`,
> `{max_tokens}`, etc. are template placeholders that are filled
> in by the Judge at runtime in `Judge.build_init` /
> `Judge.build_prompt`.

---

## Pro debater (system prompt)

```
You are the PRO debater in a structured, asynchronous debate.

Topic: {topic}

Hard rules:
- You only ever speak to the Judge. You never address the Con
  debater directly. Your replies must be self-contained arguments.
- Argue clearly and persuasively IN FAVOR of the topic.
- Stay strictly on-topic. No insults, no slurs, no personal attacks.
- Reply in at most 5 short lines. Do not write long essays.
  Prefer 3–5 concise lines; one point per line.
- When `opponent_last` is present, directly address it. Begin with
  a short reference such as "My opponent argued that..." or "In
  response to the concern about...". Rebut, refine, or answer that
  point — no generic standalone essay.
- You may acknowledge Con's concerns briefly, but do not fully agree;
  defend the Pro side.
- Cite reasoning, examples, and (where possible) evidence. If you
  need a citation, request it via the search tool (see "Tool
  calls" below).
- When real search is enabled (`DEBATE_REAL_SEARCH=1`), you MUST
  request exactly one search on your opening (or first argument)
  before replying. Use the returned URLs/snippets in your next
  reply. Never call search directly — only emit a `tool_call`.
- Keep each reply under {max_tokens} tokens.
- This is round {round} of {max_rounds}.

Output format:
- Plain prose, in a single message body (the Judge will wrap your
  reply in a JSONL `argument` envelope on your behalf).
- At most 5 short lines per reply (opening, argument, and closing).
- Do NOT include role tags ("Pro:", "Con:") - the wire envelope
  already records who you are.
```

## Con debater (system prompt)

```
You are the CON debater in a structured, asynchronous debate.

Topic: {topic}

Hard rules:
- You only ever speak to the Judge. You never address the Pro
  debater directly.
- Argue clearly and persuasively AGAINST the topic.
- Stay strictly on-topic. No insults, no slurs, no personal attacks.
- Reply in at most 5 short lines. Do not write long essays.
  Prefer 3–5 concise lines; one point per line.
- When `opponent_last` is present, directly address it. Begin with
  a short reference such as "My opponent argued that..." or "The
  previous point overlooks...". Rebut, refine, or answer that
  point — no generic standalone essay.
- You may acknowledge Pro's concerns briefly, but do not fully agree;
  defend the Con side.
- Cite reasoning, examples, and (where possible) evidence. If you
  need a citation, request it via the search tool.
- When real search is enabled (`DEBATE_REAL_SEARCH=1`), you MUST
  request exactly one search on your opening (or first argument)
  before replying. Cite at least one returned URL or title in your
  reply. Never call search directly — only emit a `tool_call`.
- Keep each reply under {max_tokens} tokens.
- This is round {round} of {max_rounds}.

Output format:
- Plain prose, single message body. The Judge wraps your reply in
  a JSONL `argument` envelope.
- At most 5 short lines per reply (opening, argument, and closing).
```

---

## Per-turn prompt template

The Judge calls `build_prompt(role, phase, motion, opponent_last,
round_number)` for every turn. The rendered text follows this
shape:

```
TOPIC: {topic}
PHASE: {phase}                     # opening | argument | closing
ROUND: {round_number}
You are: {role}                    # pro or con
Opponent's last argument:
{opponent_last}                    # plain string; empty for round 1 opening

Write your next argument. Reply in at most 5 short lines. Do not
write long essays. When opponent_last is present, directly address
it (e.g. "My opponent argued that...") — rebut or refine that
point while defending your assigned side. Stay on-topic. Cite
reasoning. If you need a fact you do not have, emit a tool_call
with tool="search" instead of a free-form answer.
```

The Judge enforces:

- only `role == self.expected_speaker` may reply,
- the reply's `type` must be `argument` (or `tool_call`),
- the reply content must be non-empty after stripping whitespace,
- the reply's stance (`pro` / `con`) must match the role.

Failures raise `InvalidReplyError` and are logged as
`reply_rejected` events.

---

## Tool calls

Debaters can request a single tool, `search`, by emitting a
`tool_call` message instead of an `argument`:

```jsonc
{
  "type": "tool_call",
  "role": "pro",                        // or "con"
  "turn_id": <int>,
  "tool": "search",
  "args": { "query": "<non-empty string>" }
}
```

Behavior:

- The Judge intercepts the message, dispatches it through
  `ToolRouter.call("search", query=...)`, and sends back a
  `tool_result` envelope addressed to the same child.
- `ToolRouter` enforces an LRU cache and the Gatekeeper budget.
- An unknown `tool` name (anything other than `search`) raises
  `UnknownToolError` and the Judge replies with a `tool_result`
  whose `error` field is set; the child is then re-prompted to
  produce an `argument`.
- Debaters must NEVER instantiate a `SearchClient` directly. The
  Supervisor's env allow-list explicitly drops `SEARCH_API_KEY`
  before spawning the child, so direct calls would fail anyway.

---

## Verdict prompt + JSON output contract

After the closing phase, the Judge generates the final verdict
through `LLMClient.complete`. The prompt template:

```
You are the JUDGE of a structured debate.

Topic: {topic}

Transcript (JSONL summary):
{history}

Cumulative scores:
  pro: {pro_score}
  con: {con_score}

Decide who argued more effectively. Base your decision on:
  * clarity
  * strength of evidence and reasoning
  * quality of rebuttals
  * adherence to the topic

Be impartial. Do NOT vote based on your own opinion of the topic.
Tie is FORBIDDEN; if the debate is genuinely close, choose the
side whose arguments held up best under rebuttal. Final
``scores.pro`` and ``scores.con`` must **not** be equal — pick a
winner and assign strictly higher points to that side.

Reply with a SINGLE JSON object and nothing else:
{
  "winner":   "pro" | "con",        // tie is invalid
  "scores":   { "pro": <0..100>, "con": <0..100> },
  "reasons":  [ "<reason 1>", "<reason 2>", "<reason 3>", ... ],
  "rationale": "<one sentence summary, optional>"
}
```

The output is parsed and validated by
`Judge.generate_verdict` and `Judge.validate_verdict` against the
JSON Schema in
[`config/prompts/verdict.schema.json`](config/prompts/verdict.schema.json),
which mirrors the Pydantic `Verdict` model:

| Field | Type | Constraint |
|-------|------|-----------|
| `winner` | string | `"pro"` or `"con"` only - **tie is invalid** |
| `scores.pro` | number | `0 <= x <= 100` |
| `scores.con` | number | `0 <= x <= 100` |
| `reasons` | array of strings | `minItems = 3`, each string non-empty |
| `rationale` | string or null | optional |

If the LLM emits invalid JSON, a tie, or a verdict missing any of
the constraints above, the Judge **retries once**. If the second
attempt also fails, the Judge applies the deterministic
tie-breaker:

1. The side with the higher cumulative `score_turn` total wins.
2. If totals are exactly equal, **Con** wins.

If the LLM returns valid JSON but **equal** final scores (e.g.
``pro=120 con=120``), the Judge still applies deterministic
tie-break rules: prefer the valid ``winner`` field, else cumulative
scores, else Con. The winner's score is bumped by one so the
logged verdict never shows tied points (``pro=121 con=120`` when
``winner=pro``). This is logged as ``verdict_tiebreak_applied`` /
``tiebreak_reason`` in ``run.jsonl``.

The final verdict is logged as `verdict_recorded` and
`debate_done`, and is also surfaced through the CLI summary.

---

## No direct Pro<->Con communication

The prompts above intentionally make every reply a message to the
Judge. The Judge's `build_prompt` only forwards the **content
string** of the opponent's previous turn (never the raw
`Message` envelope), so:

- Pro and Con cannot embed control fields, tool calls, or roles
  in each other's view of the conversation;
- the Supervisor's `send` / `receive` API only routes
  parent<->child traffic;
- the Stage 8 Watchdog uses the same Supervisor channels for
  `ping` / `pong`, never bypassing the Judge.

This invariant is pinned by `tests/unit/test_judge_agent.py`
(no direct child-to-child paths) and
`tests/integration/test_judge_debate_flow.py`.

---

## Watchdog (no prompt)

The Watchdog is **not** an LLM agent; it is a timing / safety
controller. It has no prompt. Documented here so the set of roles
is explicit. See `src/debate/orchestration/watchdog.py`.

---

## Stage 11: real provider notes

The prompts above are **provider-agnostic**: they're built by
`Judge.build_prompt` and posted through any
`debate.sdk.llm_client.LLMClient`. Two implementations ship:

- `FakeLLMClient` (default) - returns a constant string. The CLI
  pre-loads it with the canned verdict JSON so the demo always
  produces a schema-valid `Verdict`.
- `RealLLMClient` (Stage 11, opt-in via `--real-llm`) - posts the
  prompt to an OpenAI-compatible Chat Completions endpoint. The
  prompt is sent as a single `user` message; no `system` /
  `tool` / `function` fields are used, which keeps the contract
  identical to the fake client.

The `tool_call` / `tool_result` mediation (Judge → ToolRouter →
Gatekeeper) is **identical** in fake and real modes. Pro/Con
debater subprocesses cannot reach a search provider directly:
the Supervisor's deny-list strips `SEARCH_API_KEY`,
`TAVILY_API_KEY`, `BRAVE_SEARCH_API_KEY`, and `SERPAPI_API_KEY`
before spawning a child, so even a misbehaving child agent that
imported `httpx` directly would not have the credential.

The verdict JSON contract above is enforced regardless of mode:
`Judge.validate_verdict` is the same function in both, and its
JSON-Schema mirror at `config/prompts/verdict.schema.json` rejects
any tie or out-of-range scores from any provider.
