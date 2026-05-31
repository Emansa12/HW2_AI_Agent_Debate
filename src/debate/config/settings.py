"""Settings loader (placeholder).

Stage 1: empty placeholder. In later stages this will load values from
environment variables (see `.env-example`) and provide a typed
`Settings` object to the rest of the system.
"""

from __future__ import annotations


def load_settings() -> dict[str, str]:
    """Return an empty settings dict.

    Will be replaced by a real implementation in a later stage.
    """
    return {}
