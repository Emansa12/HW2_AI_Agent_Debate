"""Prompt helpers for DebaterAgent."""

from __future__ import annotations

from typing import Any

from debate.agents.reply_format import MAX_REPLY_LINES
from debate.sdk.schemas import Phase


def stance_instruction(stance: str) -> str:
    if stance == "pro":
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


def reply_format_instruction(*, phase: Phase, opponent_last: str | None) -> str:
    lines = [
        "Reply format:",
        f"- Reply in at most {MAX_REPLY_LINES} short lines.",
        "- Do not write long essays.",
        "- Prefer 3–5 concise lines; one point per line.",
    ]
    if opponent_last and phase in (Phase.ARGUMENT, Phase.CLOSING):
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
            "- Opening turn: state your side's case concisely; no opponent reply to address yet."
        )
    return "\n".join(lines)


def search_instruction() -> str:
    return (
        "Search protocol: on your opening (or first argument if opening "
        "passed without search), you MUST request exactly one search via "
        "the parent's tool_call channel before your reply. Never call "
        'search directly — emit tool="search" with a focused query, wait '
        "for tool_result, then reply using the returned hits."
    )


def format_tool_result(item: dict[str, Any]) -> str:
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


def default_search_query(*, motion: str, stance: str) -> str:
    topic = motion.strip() or "debate topic"
    if stance == "pro":
        return f"{topic} benefits evidence supporting pro side"
    return f"{topic} risks evidence supporting con side"


def build_prompt(
    *,
    motion: str,
    stance: str,
    phase: Phase,
    opponent_last: str | None,
    selected_context: list[str],
    previous_tool_results: list[dict[str, Any]],
    search_enabled: bool,
    search_completed: bool,
) -> str:
    sections: list[str] = [
        f"Motion: {motion}",
        f"Stance: {stance}",
        f"Phase: {phase.value}",
    ]
    if phase in (Phase.ARGUMENT, Phase.CLOSING) and opponent_last:
        sections.append(f"Opponent said: {opponent_last}")
    if selected_context:
        ctx_block = "\n".join(f"- {item}" for item in selected_context)
        sections.append("Context:\n" + ctx_block)
    if previous_tool_results:
        tools_block = "\n".join(format_tool_result(item) for item in previous_tool_results)
        sections.append("Previous tool results:\n" + tools_block)
        sections.append(
            "Use at least one search hit above in your reply. "
            "Cite the source URL or title when referencing evidence."
        )
    if search_enabled and not search_completed:
        sections.append(search_instruction())
    sections.append(reply_format_instruction(phase=phase, opponent_last=opponent_last))
    sections.append(stance_instruction(stance))
    return "\n\n".join(sections)


def should_request_search(
    *,
    search_enabled: bool,
    search_completed: bool,
    pending_reply_phase: Phase | None,
    phase: Phase,
    payload: dict[str, Any],
) -> bool:
    if not search_enabled or search_completed:
        return False
    if pending_reply_phase is not None:
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


def extract_phase(payload: dict[str, Any]) -> Phase:
    raw = payload.get("phase", Phase.ARGUMENT.value)
    if isinstance(raw, Phase):
        return raw
    try:
        return Phase(raw)
    except ValueError as exc:
        raise ValueError(f"unknown phase: {raw!r}") from exc
