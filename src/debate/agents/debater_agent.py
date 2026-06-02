"""DebaterAgent: shared Pro / Con debater behavior (no direct search/router imports)."""

from __future__ import annotations

from collections.abc import Callable
from typing import IO, Any, ClassVar

from debate.agents.base_agent import BaseAgent
from debate.agents.prompting import build_prompt as _build_prompt
from debate.agents.prompting import default_search_query, extract_phase, should_request_search
from debate.agents.reply_format import MAX_REPLY_LINES
from debate.agents.reply_format import truncate_reply_lines as _truncate_reply_lines
from debate.sdk.llm_client import LLMClient, LLMResponse
from debate.sdk.schemas import Message, MessageType, Phase, Role

DEFAULT_MAX_TOKENS: int = 400
SEARCH_TOOL_NAME: str = "search"


def _stance_to_role(stance: str) -> Role:
    if stance == "pro":
        return Role.PRO
    if stance == "con":
        return Role.CON
    raise TypeError(f"DebaterAgent subclasses must set STANCE to 'pro' or 'con', got {stance!r}")


class DebaterAgent(BaseAgent):
    STANCE: ClassVar[str] = ""

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

    @property
    def stance(self) -> str:
        return self.STANCE

    def handle(self, msg: Message) -> None:
        if msg.type is MessageType.INIT:
            self._on_init(msg)
        elif msg.type is MessageType.PROMPT:
            self._on_prompt(msg)
        elif msg.type is MessageType.TOOL_RESULT:
            self._on_tool_result(msg)

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
        phase = extract_phase(msg.payload)
        if "opponent_last" in msg.payload:
            opp = msg.payload["opponent_last"]
            self.opponent_last = opp if isinstance(opp, str) else None
        if should_request_search(
            search_enabled=self._search_enabled,
            search_completed=self._search_completed,
            pending_reply_phase=self._pending_reply_phase,
            phase=phase,
            payload=msg.payload,
        ):
            self._pending_reply_phase = phase
            self.request_search(default_search_query(motion=self.motion, stance=self.STANCE))
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

    def build_prompt(self, phase: Phase) -> str:
        return _build_prompt(
            motion=self.motion,
            stance=self.STANCE,
            phase=phase,
            opponent_last=self.opponent_last,
            selected_context=self.selected_context,
            previous_tool_results=self.previous_tool_results,
            search_enabled=self._search_enabled,
            search_completed=self._search_completed,
        )

    def generate_reply(self, phase: Phase) -> Message:
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

    def request_search(self, query: str) -> Message:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be a non-empty string")
        msg = self.make_message(
            MessageType.TOOL_CALL,
            {"tool": SEARCH_TOOL_NAME, "query": query},
        )
        self.send(msg)
        return msg


__all__ = ["DEFAULT_MAX_TOKENS", "MAX_REPLY_LINES", "SEARCH_TOOL_NAME", "DebaterAgent"]
