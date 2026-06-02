"""Route child ``tool_call`` envelopes through the ToolRouter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from debate.orchestration import judge_bookkeeping as bk
from debate.orchestration.judge_logging import log
from debate.orchestration.judge_types import InvalidReplyError
from debate.sdk.schemas import Message, MessageType, Role
from debate.shared.gatekeeper import BudgetExceededError
from debate.shared.router import UnknownToolError

if TYPE_CHECKING:
    from debate.orchestration.judge import Judge


def handle_tool_call(judge: Judge, role: str, tool_msg: Message) -> None:
    if tool_msg.role is not Role(role):
        log(
            judge,
            "tool_call_role_mismatch",
            target_role=role,
            actual_role=tool_msg.role.value,
        )
        raise InvalidReplyError(
            f"tool_call from {role!r} carried sender role {tool_msg.role.value!r}"
        )

    payload = tool_msg.payload
    tool_name = payload.get("tool")
    tool_call_payload = dict(payload) if isinstance(payload, dict) else {"tool": tool_name}
    log(
        judge,
        "tool_call_received",
        target_role=role,
        tool=tool_name,
        tool_call_turn_id=tool_msg.turn_id,
        tool_call_payload=tool_call_payload,
    )

    result_payload: dict[str, Any]
    if not isinstance(tool_name, str) or not tool_name:
        result_payload = {
            "tool": tool_name if isinstance(tool_name, str) else "",
            "error": "invalid_arguments",
            "message": "tool_call payload missing 'tool' name",
        }
    else:
        kwargs = {k: v for k, v in payload.items() if k != "tool"}
        try:
            result_payload = judge._router.call(tool_name, **kwargs)
        except UnknownToolError as exc:
            result_payload = {
                "tool": tool_name,
                "error": "unknown_tool",
                "message": str(exc),
            }
        except BudgetExceededError as exc:
            result_payload = {
                "tool": tool_name,
                "error": "budget_exceeded",
                "kind": exc.kind.value,
                "message": str(exc),
            }
        except ValueError as exc:
            result_payload = {
                "tool": tool_name,
                "error": "invalid_arguments",
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - never crash the debate
            result_payload = {
                "tool": tool_name,
                "error": "tool_error",
                "message": str(exc),
            }

    result_msg = bk.make_judge_message(judge, MessageType.TOOL_RESULT, result_payload)
    judge._supervisor.send(role, result_msg)
    log(
        judge,
        "tool_result_sent",
        target_role=role,
        tool=tool_name,
        tool_result_turn_id=result_msg.turn_id,
        error=result_payload.get("error"),
        tool_result_payload=dict(result_payload),
    )
