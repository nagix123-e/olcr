"""Provider adapters for explicit, guarded software release operations."""

from .base import (
    CommandResult,
    CommandSpec,
    ProviderApiTransport,
    ProviderRequest,
    ProviderResponse,
    ReleaseAdapter,
    ReleaseCommandRunner,
)
from .github import GitHubReleaseAdapter

__all__ = [
    "CommandResult",
    "CommandSpec",
    "GitHubReleaseAdapter",
    "ProviderApiTransport",
    "ProviderRequest",
    "ProviderResponse",
    "ReleaseAdapter",
    "ReleaseCommandRunner",
]
