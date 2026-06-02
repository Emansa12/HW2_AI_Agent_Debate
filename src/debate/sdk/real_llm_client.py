"""Optional real-provider :class:`LLMClient` (Stage 11, OpenAI-compatible)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from debate.sdk.llm_client import LLMResponse
from debate.sdk.llm_response_parser import (
    _parse_chat_completion,
    _short_reason,
)

DEFAULT_BASE_URL: str = "https://api.openai.com/v1"
DEFAULT_MODEL: str = "gpt-4o-mini"
DEFAULT_TEMPERATURE: float = 0.2
DEFAULT_TIMEOUT_SECONDS: float = 60.0
DEFAULT_PRICE_PER_1K_INPUT_USD: float = 0.00015
DEFAULT_PRICE_PER_1K_OUTPUT_USD: float = 0.00060

_KEY_ENV_NAMES: tuple[str, ...] = ("LLM_API_KEY", "OPENAI_API_KEY")


class RealLLMError(RuntimeError):
    """Base class for typed errors raised by :class:`RealLLMClient`."""

    provider: str = "openai-compatible"


class MissingLLMAPIKeyError(RealLLMError):
    def __init__(self, env_names: tuple[str, ...] = _KEY_ENV_NAMES) -> None:
        names = " or ".join(env_names)
        super().__init__(
            f"missing LLM API key: set {names} in your environment "
            "(e.g. via .env or `--real-llm` will fail without it)."
        )


class LLMProviderError(RealLLMError):
    def __init__(self, status_code: int, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"LLM provider returned HTTP {status_code}: {reason}")


class LLMProviderUnavailableError(RealLLMError):
    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(f"LLM provider unavailable: {type(cause).__name__}: {cause}")


class LLMProviderResponseError(RealLLMError):
    """Raised when the upstream returns 2xx but the body is not the expected JSON shape."""


class RealLLMClient:
    """Real HTTP-backed :class:`LLMClient` for OpenAI-compatible APIs."""

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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> RealLLMClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def complete(self, *, prompt: str, max_tokens: int) -> LLMResponse:
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
