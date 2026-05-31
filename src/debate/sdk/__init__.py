"""Public SDK surface for the debate protocol.

Stage 2 added the wire schemas. Stage 4 adds pluggable LLM and
search client interfaces, plus their fake/offline implementations.
"""

from debate.sdk.llm_client import FakeLLMClient, LLMClient, LLMResponse
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
    "MAX_RESULTS_PER_RESPONSE",
    "MAX_SNIPPET_CHARS",
    "MAX_TITLE_CHARS",
    "MAX_URL_CHARS",
    "SCHEMA_VERSION",
    "FakeLLMClient",
    "FakeSearchClient",
    "LLMClient",
    "LLMResponse",
    "Message",
    "MessageType",
    "Phase",
    "Role",
    "SearchClient",
    "SearchResponse",
    "SearchResult",
    "Verdict",
]
