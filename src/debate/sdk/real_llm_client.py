"""Optional real-provider :class:`LLMClient` (Stage 11).

Implements the same :class:`debate.sdk.llm_client.LLMClient`
Protocol as :class:`FakeLLMClient`, but talks to an
**OpenAI-compatible** Chat Completions endpoint
(`POST {base_url}/chat/completions`). The OpenAI shape is supported
verbatim by OpenAI itself plus most "OpenAI-compatible" gateways
(Together, Groq, OpenRouter, Anthropic via proxy, Azure OpenAI,
local LM Studio / Ollama / vLLM bridges, ...). For HW2 we only
ever invoke this from the parent CLI when the user passes
``--real-llm`` and an ``LLM_API_KEY`` (or ``OPENAI_API_KEY``) is
present.

Hard rules - identical to :class:`RealSearchClient`:

- Defaults are still :class:`FakeLLMClient`. Tests stay offline:
  the suite drives this client with a :class:`httpx.MockTransport`
  for synthetic responses.
- The API key is read from the environment **only**
  (``LLM_API_KEY`` is the canonical name; ``OPENAI_API_KEY`` is
  accepted as a provider-specific alias). The key only ever
  appears as the ``Authorization`` header passed to ``httpx``,
  never in URLs, prompts, or transcript fields.
- Every call still goes through the Stage 4
  :class:`debate.shared.gatekeeper.Gatekeeper`; the Judge in
  Stage 9 wraps each ``llm.complete`` call accordingly.
- :class:`LLMResponse` enforces non-negative token counts, so a
  malformed upstream response is rejected before it reaches the
  Ledger.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from debate.sdk.llm_client import LLMResponse

DEFAULT_BASE_URL: str = "https://api.openai.com/v1"
"""OpenAI-compatible base URL. Override via ``base_url=`` for a
proxy / alternate provider (e.g. ``https://api.groq.com/openai/v1``)."""

DEFAULT_MODEL: str = "gpt-4o-mini"
"""Default model identifier. Cheap, fast, and supports the JSON
output the Stage 9 verdict pipeline expects."""

DEFAULT_TEMPERATURE: float = 0.2
"""Low temperature keeps the verdict JSON well-formed; debaters can
override via ``temperature=`` in the constructor."""

DEFAULT_TIMEOUT_SECONDS: float = 60.0
"""Per-request timeout. LLM calls are slower than search so we
allow more headroom."""

DEFAULT_PRICE_PER_1K_INPUT_USD: float = 0.00015
"""Default per-1K-input-token price (gpt-4o-mini, June 2026).
Override per-instance for other models / providers."""

DEFAULT_PRICE_PER_1K_OUTPUT_USD: float = 0.00060
"""Default per-1K-output-token price (gpt-4o-mini, June 2026).
Output tokens are typically 4x input cost; this constant is used
when the upstream usage block is missing."""

_KEY_ENV_NAMES: tuple[str, ...] = ("LLM_API_KEY", "OPENAI_API_KEY")
"""Environment variables read in priority order. ``LLM_API_KEY`` is
the generic name (so a single ``.env`` works for any provider);
``OPENAI_API_KEY`` is accepted as a convention-friendly alias."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RealLLMError(RuntimeError):
    """Base class for typed errors raised by :class:`RealLLMClient`."""

    provider: str = "openai-compatible"


class MissingLLMAPIKeyError(RealLLMError):
    """Raised when neither ``LLM_API_KEY`` nor ``OPENAI_API_KEY`` is
    set in the environment."""

    def __init__(self, env_names: tuple[str, ...] = _KEY_ENV_NAMES) -> None:
        names = " or ".join(env_names)
        super().__init__(
            f"missing LLM API key: set {names} in your environment "
            "(e.g. via .env or `--real-llm` will fail without it)."
        )


class LLMProviderError(RealLLMError):
    """Raised on non-2xx responses from the upstream LLM provider."""

    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"LLM provider returned HTTP {status_code}: {reason}")


class LLMProviderUnavailableError(RealLLMError):
    """Raised on transport-layer failures (DNS, TLS, connect refused,
    timeouts). Wraps the underlying :class:`httpx.HTTPError`."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"LLM provider unavailable: {type(cause).__name__}: {cause}")


class LLMProviderResponseError(RealLLMError):
    """Raised when the upstream returns 2xx but the body is not the
    JSON shape we expect (no ``choices[0].message.content``)."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class RealLLMClient:
    """Real HTTP-backed :class:`LLMClient` for OpenAI-compatible APIs.

    Construct via :meth:`from_env` or pass ``api_key=`` directly.
    Tests inject an :class:`httpx.MockTransport` via the ``client=``
    constructor argument so no real HTTP call ever leaves the
    process.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        price_per_1k_input_usd: float = DEFAULT_PRICE_PER_1K_INPUT_USD,
        price_per_1k_output_usd: float = DEFAULT_PRICE_PER_1K_OUTPUT_USD,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise MissingLLMAPIKeyError()
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if temperature < 0:
            raise ValueError("temperature must be >= 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if price_per_1k_input_usd < 0 or price_per_1k_output_usd < 0:
            raise ValueError("price_per_1k_*_usd must be >= 0")

        self._api_key: str = api_key.strip()
        self._model: str = model.strip()
        self._base_url: str = base_url.rstrip("/")
        self._temperature: float = temperature
        self._price_in: float = price_per_1k_input_usd
        self._price_out: float = price_per_1k_output_usd

        if client is not None:
            self._client: httpx.Client = client
            self._owns_client: bool = False
        else:
            self._client = httpx.Client(
                timeout=timeout_seconds,
                transport=transport,
            )
            self._owns_client = True

    @classmethod
    def from_env(
        cls,
        *,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> RealLLMClient:
        """Build from environment variables.

        Reads ``LLM_API_KEY`` (preferred) or ``OPENAI_API_KEY``
        from ``env`` (defaults to :data:`os.environ`). Raises
        :class:`MissingLLMAPIKeyError` if neither is set.
        Picks up ``OPENAI_BASE_URL`` and ``OPENAI_MODEL`` if
        provided, so a single ``.env`` configures the whole client.
        """
        source = env if env is not None else os.environ

        api_key: str | None = None
        for name in _KEY_ENV_NAMES:
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                api_key = value.strip()
                break
        if api_key is None:
            raise MissingLLMAPIKeyError()

        base_url = source.get("OPENAI_BASE_URL")
        model = source.get("OPENAI_MODEL")
        if isinstance(base_url, str) and base_url.strip():
            kwargs.setdefault("base_url", base_url.strip())
        if isinstance(model, str) and model.strip():
            kwargs.setdefault("model", model.strip())

        return cls(api_key=api_key, **kwargs)

    # -- Context-manager / cleanup -----------------------------------------

    def close(self) -> None:
        """Close the underlying ``httpx`` client if we own it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RealLLMClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- LLMClient.complete ------------------------------------------------

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
        """Run a single completion against the upstream provider."""
        if max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")

        url = f"{self._base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": self._temperature,
        }
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMProviderUnavailableError(exc) from exc

        if resp.status_code >= 400:
            raise LLMProviderError(resp.status_code, _short_reason(resp))

        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMProviderResponseError(f"upstream response was not JSON: {exc}") from exc

        text, tokens_in, tokens_out = _parse_chat_completion(payload)
        usd = (tokens_in / 1000.0) * self._price_in + (tokens_out / 1000.0) * self._price_out

        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=usd,
        )


# ---------------------------------------------------------------------------
# Helpers (module-level so they are unit-testable in isolation)
# ---------------------------------------------------------------------------


def _short_reason(resp: httpx.Response) -> str:
    """Best-effort short reason string for an error response.

    Never returns request headers or the API key.
    """
    reason = resp.reason_phrase or ""
    text = (resp.text or "").strip()
    if text:
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        return f"{reason} - {text}" if reason else text
    return reason or "(no reason)"


def _parse_chat_completion(payload: Any) -> tuple[str, int, int]:
    """Extract ``(text, tokens_in, tokens_out)`` from an OpenAI Chat
    Completions JSON body. Raises :class:`LLMProviderResponseError`
    on any structural problem.
    """
    if not isinstance(payload, dict):
        raise LLMProviderResponseError("upstream JSON root must be an object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMProviderResponseError("upstream JSON missing non-empty 'choices' array")

    first = choices[0]
    if not isinstance(first, dict):
        raise LLMProviderResponseError("choices[0] must be an object")

    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMProviderResponseError("choices[0].message must be an object")

    text = message.get("content")
    if not isinstance(text, str):
        # Some providers stream the content into delta chunks
        # instead of message.content; we only support the
        # non-streaming path here, so this is an error.
        raise LLMProviderResponseError("choices[0].message.content must be a string")

    usage = payload.get("usage")
    if isinstance(usage, dict):
        tokens_in = _coerce_non_negative_int(
            usage.get("prompt_tokens"),
            field="usage.prompt_tokens",
        )
        tokens_out = _coerce_non_negative_int(
            usage.get("completion_tokens"),
            field="usage.completion_tokens",
        )
    else:
        tokens_in, tokens_out = 0, 0

    return text, tokens_in, tokens_out


def _coerce_non_negative_int(value: Any, *, field: str) -> int:
    """Coerce a number-shaped JSON value into a non-negative ``int``."""
    if value is None:
        return 0
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python; reject so we
        # never silently turn ``True`` into 1 token.
        raise LLMProviderResponseError(f"{field} must be a non-negative integer, got bool")
    if not isinstance(value, (int, float)):
        raise LLMProviderResponseError(f"{field} must be a number, got {type(value).__name__}")
    ivalue = int(value)
    if ivalue < 0:
        raise LLMProviderResponseError(f"{field} must be non-negative, got {ivalue}")
    return ivalue
