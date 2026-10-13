"""Adapter kind -> factory. Factories are imported lazily: removing an adapter module
breaks only the stores that use that kind."""

from collections.abc import Callable
from importlib import import_module

from shopmcp.adapters.base import AdapterContext, ShopAdapter
from shopmcp.config import AdapterConfig, ConfigError

Factory = Callable[[AdapterConfig, AdapterContext], ShopAdapter]

# Each module exposes `create(cfg: AdapterConfig, ctx: AdapterContext) -> ShopAdapter`.
_MODULES: dict[str, str] = {
    "yml": "shopmcp.adapters.yml.adapter",
    "bitrix": "shopmcp.adapters.bitrix",
    "insales": "shopmcp.adapters.insales",
    "http_json": "shopmcp.adapters.http_json",
}
_REGISTERED: dict[str, Factory] = {}


def register(kind: str, factory: Factory) -> None:
    """Add a kind at runtime (tests, third-party adapters)."""
    _REGISTERED[kind] = factory


def kinds() -> frozenset[str]:
    return frozenset(_MODULES) | frozenset(_REGISTERED)


def create(cfg: AdapterConfig, ctx: AdapterContext) -> ShopAdapter:
    if cfg.kind in _REGISTERED:
        return _REGISTERED[cfg.kind](cfg, ctx)
    module = _MODULES.get(cfg.kind)
    if module is None:
        raise ConfigError(f"adapter.kind: unknown {cfg.kind!r}, expected one of {sorted(kinds())}")
    try:
        factory: Factory = import_module(module).create
    except (ImportError, AttributeError) as e:
        raise ConfigError(f"adapter.kind {cfg.kind!r}: {module}.create is unavailable: {e}") from e
    return factory(cfg, ctx)
