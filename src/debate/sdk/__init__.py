"""Public SDK surface for the debate protocol.

Stage 2 added the wire schemas. Stage 4 added pluggable LLM and
search client interfaces, plus their fake/offline implementations.
Stage 11 adds the optional real-provider clients
(:class:`RealSearchClient` / :class:`RealLLMClient`); they are
imported lazily so the ``httpx`` dependency is only loaded when
they're actually used.
"""

from debate.sdk.llm_client import FakeLLMClient, LLMClient, LLMResponse
from debate.sdk.real_llm_client import (
    DEFAULT_BASE_URL as DEFAULT_LLM_BASE_URL,
)
from debate.sdk.real_llm_client import (
    DEFAULT_MODEL as DEFAULT_LLM_MODEL,
)
from debate.sdk.real_llm_client import (
    LLMProviderError,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
    MissingLLMAPIKeyError,
    RealLLMClient,
    RealLLMError,
)
from debate.sdk.real_search_client import (
    DEFAULT_TAVILY_URL,
    MissingSearchAPIKeyError,
    RealSearchClient,
    RealSearchError,
    SearchProviderError,
    SearchProviderResponseError,
    SearchProviderUnavailableError,
)
from debate.sdk.schemas import (
    SCHEMA_VERSION,
    Message,
    MessageType,
    Phase,
    Role,
    Verdict,
)
from debate.sdk.search_client import (
    MAX_RESULTS_PER_RESPONSE,
    MAX_SNIPPET_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    FakeSearchClient,
    SearchClient,
    SearchResponse,
    SearchResult,
)

__all__ = [
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_TAVILY_URL",
    "MAX_RESULTS_PER_RESPONSE",
    "MAX_SNIPPET_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_URL_CHARS",
    "SCHEMA_VERSION",
    "FakeLLMClient",
    "FakeSearchClient",
    "LLMClient",
    "LLMProviderError",
    "LLMProviderResponseError",
    "LLMProviderUnavailableError",
    "LLMResponse",
    "Message",
    "MessageType",
    "MissingLLMAPIKeyError",
    "MissingSearchAPIKeyError",
    "Phase",
    "RealLLMClient",
    "RealLLMError",
    "RealSearchClient",
    "RealSearchError",
    "Role",
    "SearchClient",
    "SearchProviderError",
    "SearchProviderResponseError",
    "SearchProviderUnavailableError",
    "SearchResponse",
    "SearchResult",
    "Verdict",
]
