"""Provider type registry. Every module in factory/providers/ is imported on first use,
so a new adapter only needs `@register_provider("<type>")` and an entry in models.yaml.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable

from factory.providers.base import LLMProvider, ProviderError

_REGISTRY: dict[str, type[LLMProvider]] = {}
_discovered = False
_NOT_ADAPTERS = {"base", "registry", "resolver", "tool_protocol"}


def register_provider(type_name: str) -> Callable[[type[LLMProvider]], type[LLMProvider]]:
    def decorator(cls: type[LLMProvider]) -> type[LLMProvider]:
        existing = _REGISTRY.get(type_name)
        if existing is not None and existing is not cls:
            raise ValueError(f"provider type {type_name!r} already registered by {existing}")
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def discover() -> None:
    global _discovered
    if _discovered:
        return
    import factory.providers as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name not in _NOT_ADAPTERS and not info.name.startswith("_"):
            importlib.import_module(f"{pkg.__name__}.{info.name}")
    _discovered = True


def available_types() -> list[str]:
    discover()
    return sorted(_REGISTRY)


def get_provider_class(type_name: str) -> type[LLMProvider]:
    discover()
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise ProviderError(
            f"no adapter registered for provider type {type_name!r}; "
            f"available: {', '.join(available_types())}"
        ) from None
