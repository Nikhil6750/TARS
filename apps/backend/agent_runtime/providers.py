from __future__ import annotations

from collections.abc import Iterable

from agent_runtime.contracts import IntelligenceProvider
from agent_runtime.errors import ProviderUnavailableError


class IntelligenceProviderRegistry:
    def __init__(self, providers: Iterable[IntelligenceProvider] = ()) -> None:
        self._providers: dict[str, IntelligenceProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: IntelligenceProvider, *, replace: bool = False) -> None:
        if not isinstance(provider, IntelligenceProvider):
            raise TypeError("provider does not satisfy IntelligenceProvider")
        name = getattr(provider, "name", "")
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("provider name must be a non-empty, trimmed string")
        if name in self._providers and not replace:
            raise ValueError(f"intelligence provider {name!r} is already registered")
        self._providers[name] = provider

    def require(self, name: str) -> IntelligenceProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderUnavailableError(
                f"intelligence provider {name!r} is not configured"
            ) from exc
