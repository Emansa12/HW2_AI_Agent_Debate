"""JSONL inter-process / inter-agent communication helpers.

Wire format: each message is **one UTF-8 JSON object on a single
line, terminated by exactly one `\\n`**. This makes the channel
trivially pipeable over stdin/stdout, sockets, or files.

Public API:
    serialize_message(msg)   -> str  (single newline-terminated line)
    deserialize_message(ln)  -> Message
    MAX_MESSAGE_BYTES        -> hard cap per line (UTF-8 encoded)

Exceptions (all inherit from `IPCError`):
    OversizeError          - line exceeds MAX_MESSAGE_BYTES
    MultilineError         - line contains an embedded newline / CR
    SchemaVersionError     - `v` does not match SCHEMA_VERSION
    MalformedMessageError  - line is not valid JSON, has the wrong
                             root type, or fails Pydantic validation
                             (unknown role / type / extra fields / tie
                             verdict / etc.)
"""

from __future__ import annotations

import json
from json import JSONDecodeError

from pydantic import ValidationError

from debate.sdk.schemas import SCHEMA_VERSION, Message

MAX_MESSAGE_BYTES: int = 64 * 1024
"""Hard cap on a single line in bytes (UTF-8 encoded).

Lines larger than this are rejected on both serialize and deserialize
to prevent runaway payloads from clogging the IPC channel.
"""


class IPCError(Exception):
    """Base class for IPC errors raised by this module."""


class OversizeError(IPCError):
    """Raised when a message exceeds `MAX_MESSAGE_BYTES`."""


class MultilineError(IPCError):
    """Raised when a message body contains a newline or carriage return."""


class SchemaVersionError(IPCError):
    """Raised when the `v` field does not match `SCHEMA_VERSION`."""


class MalformedMessageError(IPCError):
    """Raised when a line is not valid JSON, has the wrong root type,
    or fails schema validation (unknown role / type / verdict tie /
    forbidden extra fields, etc.).
    """


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def serialize_message(msg: Message) -> str:
    """Encode `msg` as a single newline-terminated JSON line.

    Raises:
        MultilineError: encoded body contains a newline / CR (should
            never happen with the standard JSON encoder, but we guard
            against custom encoders).
        OversizeError: encoded line exceeds `MAX_MESSAGE_BYTES`.
    """
    body = msg.model_dump_json()
    if "\n" in body or "\r" in body:
        raise MultilineError("serialized message contains an embedded newline/CR")
    line = body + "\n"
    size = _byte_len(line)
    if size > MAX_MESSAGE_BYTES:
        raise OversizeError(f"serialized message is {size} bytes, max is {MAX_MESSAGE_BYTES}")
    return line


def deserialize_message(line: str) -> Message:
    """Parse a single JSONL line into a validated `Message`.

    `line` may be passed with or without its trailing `\\n`.

    Raises:
        OversizeError: line exceeds `MAX_MESSAGE_BYTES`.
        MultilineError: body contains an embedded newline / CR (more
            than one logical record on the wire).
        SchemaVersionError: `v` does not match `SCHEMA_VERSION`.
        MalformedMessageError: invalid JSON, wrong root type, or
            failed schema validation.
    """
    if _byte_len(line) > MAX_MESSAGE_BYTES:
        raise OversizeError(f"incoming line is {_byte_len(line)} bytes, max is {MAX_MESSAGE_BYTES}")

    body = line[:-1] if line.endswith("\n") else line
    if "\n" in body or "\r" in body:
        raise MultilineError("incoming message contains an embedded newline/CR")

    try:
        data = json.loads(body)
    except JSONDecodeError as exc:
        raise MalformedMessageError(f"not valid JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise MalformedMessageError("JSON root must be an object")

    if data.get("v") != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"unsupported schema version: {data.get('v')!r} (expected {SCHEMA_VERSION})"
        )

    try:
        return Message.model_validate(data)
    except ValidationError as exc:
        raise MalformedMessageError(f"schema validation failed: {exc.errors()}") from exc
