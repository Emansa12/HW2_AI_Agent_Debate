"""DebaterAgent: shared Pro / Con debater behavior.

This is the only place where Pro- and Con-specific logic lives. The
concrete :class:`debate.agents.pro_agent.ProAgent` and
:class:`debate.agents.con_agent.ConAgent` exist only to set the
``STANCE`` class attribute - they must not contain any logic of
their own.

A DebaterAgent is a :class:`debate.agents.base_agent.BaseAgent`
extended with:

- a ``STANCE`` ("pro" / "con") that is encoded into every outgoing
  reply payload, so the parent can verify the child stayed in role;
- a ``motion`` and ``selected_context`` it is bound to at init time
  (and which can be refreshed by an ``init`` envelope);
- an ``opponent_last`` shadow updated from each ``prompt`` envelope;
- an LLM client injected at construction (the offline
  :class:`debate.sdk.llm_client.FakeLLMClient` is used for Stage 7
  tests, but any object satisfying the
  :class:`debate.sdk.llm_client.LLMClient` Protocol works);
- a :meth:`request_search` helper that emits a ``tool_call``
  envelope **instead** of calling search itself - search must
  always be brokered by the parent's :class:`ToolRouter`
  (Gatekeeper-controlled and cached).

DebaterAgent never imports :mod:`debate.sdk.search_client`, the
:mod:`debate.shared.router` module, or the
:mod:`debate.shared.gatekeeper` module. A static test in
``tests/unit/test_debater_agent.py`` enforces this.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import IO, Any, ClassVar

from debate.agents.base_agent import BaseAgent
from debate.sdk.llm_client import LLMClient, LLMResponse
from debate.sdk.schemas import Message, MessageType, Phase, Role

DEFAULT_MAX_TOKENS: int = 400
"""Per-turn token cap mirrored from the debate config defaults.

Real value comes from the ``init`` envelope at runtime."""

SEARCH_TOOL_NAME: str = "search"

MAX_REPLY_LINES: int = 5
"""Maximum number of lines allowed in each debater reply."""


def _stance_to_role(stance: str) -> Role:
    if stance == "pro":
        return Role.PRO
    if stance == "con":
        return Role.CON
    raise TypeError(f"DebaterAgent subclasses must set STANCE to 'pro' or 'con', got {stance!r}")


class DebaterAgent(BaseAgent):
    """Shared debater logic for Pro and Con.

    Concrete subclasses (``ProAgent``, ``ConAgent``) only set
    ``STANCE``; they must not override behavior. The role
    (:class:`debate.sdk.schemas.Role`) is derived from ``STANCE``.
    """

    STANCE: ClassVar[str] = ""
    """Subclasses set this to ``"pro"`` or ``"con"``. Empty is rejected."""

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        motion: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        selected_context: list[str] | None = None,
        search_enabled: bool = False,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        role = _stance_to_role(self.STANCE)
        super().__init__(role=role, stdin=stdin, stdout=stdout, clock=clock)
        self._llm: LLMClient = llm_client
        self.motion: str = motion
        self.max_tokens: int = int(max_tokens)
        self.selected_context: list[str] = list(selected_context or [])
        self.opponent_last: str | None = None
        self.previous_tool_results: list[dict[str, Any]] = []
        self._search_enabled: bool = search_enabled
        self._search_completed: bool = False
        self._pending_reply_phase: Phase | None = None

    # ----- public attributes / properties --------------------------------

    @property
    def stance(self) -> str:
        return self.STANCE

    # ----- BaseAgent dispatch override -----------------------------------

    def handle(self, msg: Message) -> None:
        if msg.type is MessageType.INIT:
            self._on_init(msg)
        elif msg.type is MessageType.PROMPT:
            self._on_prompt(msg)
        elif msg.type is MessageType.TOOL_RESULT:
            self._on_tool_result(msg)

    # ----- per-type handlers --------------------------------------------

    def _on_init(self, msg: Message) -> None:
        payload = msg.payload
        if "stance" in payload and payload["stance"] != self.STANCE:
            raise ValueError(
                f"stance mismatch on init: expected {self.STANCE!r}, got {payload['stance']!r}"
            )
        if isinstance(payload.get("motion"), str):
            self.motion = payload["motion"]
        if isinstance(payload.get("max_tokens"), int):
            self.max_tokens = payload["max_tokens"]
        ctx = payload.get("selected_context")
        if isinstance(ctx, list):
            self.selected_context = [str(item) for item in ctx]

    def _on_prompt(self, msg: Message) -> None:
        phase = self._extract_phase(msg.payload)
        if "opponent_last" in msg.payload:
            opp = msg.payload["opponent_last"]
            self.opponent_last = opp if isinstance(opp, str) else None
        if self._should_request_search(phase, msg.payload):
            self._pending_reply_phase = phase
            self.request_search(self._default_search_query())
            return
        reply = self.generate_reply(phase)
        self.send(reply)

    def _on_tool_result(self, msg: Message) -> None:
        self.previous_tool_results.append(dict(msg.payload))
        if self._pending_reply_phase is not None:
            phase = self._pending_reply_phase
            self._pending_reply_phase = None
            self._search_completed = True
            reply = self.generate_reply(phase)
            self.send(reply)

    # ----- prompt / reply construction ----------------------------------

    def build_prompt(self, phase: Phase) -> str:
        """Assemble the prompt string fed to the LLM.

        Includes - always - the motion, the stance, the phase, reply
        format rules (at most :data:`MAX_REPLY_LINES` short lines),
        and a clear stance instruction. The opponent's last message
        is included for ``argument`` and ``closing`` phases when the
        Judge supplied ``opponent_last``. Selected context and recorded
        tool results are appended when present.
        """
        sections: list[str] = [
            f"Motion: {self.motion}",
            f"Stance: {self.STANCE}",
            f"Phase: {phase.value}",
        ]
        if phase in (Phase.ARGUMENT, Phase.CLOSING) and self.opponent_last:
            sections.append(f"Opponent said: {self.opponent_last}")
        if self.selected_context:
            ctx_block = "\n".join(f"- {item}" for item in self.selected_context)
            sections.append("Context:\n" + ctx_block)
        if self.previous_tool_results:
            tools_block = "\n".join(
                f"- {self._format_tool_result(item)}" for item in self.previous_tool_results
            )
            sections.append("Previous tool results:\n" + tools_block)
            sections.append(
                "Use at least one search hit above in your reply. Cite the source URL "
                "or title when referencing evidence."
            )
        if self._search_enabled and not self._search_completed:
            sections.append(self._search_instruction())
        sections.append(self._reply_format_instruction(phase))
        sections.append(self._stance_instruction())
        return "\n\n".join(sections)

    def generate_reply(self, phase: Phase) -> Message:
        """Run the LLM and return a `Message` of type ``reply``."""
        prompt = self.build_prompt(phase)
        resp: LLMResponse = self._llm.complete(prompt=prompt, max_tokens=self.max_tokens)
        content = _truncate_reply_lines(resp.text, max_lines=MAX_REPLY_LINES)
        return self.make_message(
            MessageType.REPLY,
            {
                "phase": phase.value,
                "stance": self.STANCE,
                "content": content,
                "tokens_in": resp.tokens_in,
                "tokens_out": resp.tokens_out,
            },
        )

    # ----- tool calls ----------------------------------------------------

    def request_search(self, query: str) -> Message:
        """Emit a ``tool_call`` envelope asking the parent to search.

        The child agent never imports SearchClient or ToolRouter; the
        parent is the only process allowed to actually call search,
        through its Gatekeeper-bounded ToolRouter. The returned
        envelope is also written to stdout.
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be a non-empty string")
        msg = self.make_message(
            MessageType.TOOL_CALL,
            {"tool": SEARCH_TOOL_NAME, "query": query},
        )
        self.send(msg)
        return msg

    # ----- helpers -------------------------------------------------------

    def _stance_instruction(self) -> str:
        if self.STANCE == "pro":
            return (
                "You argue IN FAVOR of the motion. Stay strictly in this "
                "stance for the entire debate. You may acknowledge concerns "
                "briefly, but do not fully agree with Con — defend Pro."
            )
        return (
            "You argue AGAINST the motion. Stay strictly in this stance "
            "for the entire debate. You may acknowledge concerns briefly, "
            "but do not fully agree with Pro — defend Con."
        )

    def _reply_format_instruction(self, phase: Phase) -> str:
        lines = [
            "Reply format:",
            f"- Reply in at most {MAX_REPLY_LINES} short lines.",
            "- Do not write long essays.",
            "- Prefer 3–5 concise lines; one point per line.",
        ]
        if self.opponent_last and phase in (Phase.ARGUMENT, Phase.CLOSING):
            lines.extend(
                [
                    "- Directly address opponent_last when present.",
                    '- Begin with a short reference such as "My opponent argued that...", '
                    '"In response to the concern about...", or '
                    '"The previous point overlooks...".',
                    "- Rebut, refine, or answer the opponent's previous point — "
                    "no generic standalone essay.",
                ]
            )
        elif phase is Phase.OPENING:
            lines.append(
                "- Opening turn: state your side's case concisely; "
                "no opponent reply to address yet."
            )
        return "\n".join(lines)

    def _search_instruction(self) -> str:
        return (
            "Search protocol: on your opening (or first argument if opening "
            "passed without search), you MUST request exactly one search via "
            "the parent's tool_call channel before your reply. Never call "
            'search directly — emit tool="search" with a focused query, wait '
            "for tool_result, then reply using the returned hits."
        )

    def _should_request_search(self, phase: Phase, payload: dict[str, Any]) -> bool:
        """Return True when this prompt should emit a search tool_call first.

        Only active when ``search_enabled`` is set (real-search mode).
        Each debater searches at most once, on opening or first argument.
        """
        if not self._search_enabled or self._search_completed:
            return False
        if self._pending_reply_phase is not None:
            return False
        rnd = payload.get("round", 0)
        if not isinstance(rnd, int):
            try:
                rnd = int(rnd)
            except (TypeError, ValueError):
                rnd = 0
        if phase is Phase.OPENING:
            return True
        return phase is Phase.ARGUMENT and rnd == 0

    def _default_search_query(self) -> str:
        """Deterministic search query derived from motion + stance."""
        topic = self.motion.strip() or "debate topic"
        if self.STANCE == "pro":
            return f"{topic} benefits evidence supporting pro side"
        return f"{topic} risks evidence supporting con side"

    @staticmethod
    def _format_tool_result(item: dict[str, Any]) -> str:
        tool = item.get("tool", "?")
        if "results" in item:
            results = item["results"]
            if isinstance(results, list):
                parts: list[str] = []
                for hit in results:
                    if isinstance(hit, dict):
                        title = hit.get("title", "")
                        url = hit.get("url", "")
                        snippet = hit.get("snippet", "")
                        parts.append(f"{title} ({url}): {snippet}")
                    else:
                        parts.append(str(hit))
                return f"{tool}: " + "; ".join(parts) if parts else f"{tool}: {results}"
            return f"{tool}: {results}"
        return f"{tool}: {item}"

    @staticmethod
    def _extract_phase(payload: dict[str, Any]) -> Phase:
        raw = payload.get("phase", Phase.ARGUMENT.value)
        if isinstance(raw, Phase):
            return raw
        try:
            return Phase(raw)
        except ValueError as exc:
            raise ValueError(f"unknown phase: {raw!r}") from exc


def _truncate_reply_lines(text: str, *, max_lines: int) -> str:
    """Keep at most ``max_lines`` non-empty lines from a debater reply."""
    if max_lines < 1:
        max_lines = 1
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text.strip()
    return "\n".join(lines[:max_lines]).strip()


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "MAX_REPLY_LINES",
    "SEARCH_TOOL_NAME",
    "DebaterAgent",
]
