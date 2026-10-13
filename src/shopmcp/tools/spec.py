"""Tool specification: what a tool needs, who may call it, what it returns, how it reads.

Model-facing texts are `L10n` pairs on the spec itself, so a tool module owns its
texts and the stage-1 modules do not share a catalog file. Stores override them by
key (`ToolSpec.texts()`), in the `[texts]` table of `store.toml`.
"""

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from shopmcp.adapters.base import ShopAdapter
from shopmcp.capabilities import Capability
from shopmcp.cart.strategies import CartStrategy
from shopmcp.config import StoreConfig
from shopmcp.i18n import L10n


@dataclass(frozen=True)
class ToolSpec:
    suffix: str  # "products_search" -> tool name f"{cfg.prefix}_{suffix}"
    requires: frozenset[Capability]  # empty = always present
    scopes: tuple[str, ...]  # empty = public; otherwise ToolScopeGate demands a token
    annotations: ToolAnnotations
    output_model: type[BaseModel]
    # Return annotation must be Annotated[CallToolResult, output_model]: that is how
    # the SDK derives outputSchema from a tool that builds its own CallToolResult.
    fn: Callable[..., Awaitable[CallToolResult]]
    title: L10n
    description: L10n
    params: Mapping[str, L10n] = field(default_factory=dict)  # parameter name -> description

    def texts(self) -> Iterator[tuple[str, L10n]]:
        """Every model-facing text of the tool with its override key."""
        yield f"tool.{self.suffix}.title", self.title
        yield f"tool.{self.suffix}.description", self.description
        for name, text in self.params.items():
            yield f"tool.{self.suffix}.param.{name}", text


def with_param_docs[F: Callable[..., Any]](fn: F, docs: Mapping[str, str]) -> F:
    """Copy of `fn` whose parameters carry `docs` as JSON Schema descriptions.

    The SDK builds inputSchema from `inspect.signature(fn)`, which honours
    `__signature__`, so descriptions are localized without touching SDK internals.
    """
    sig = inspect.signature(fn, eval_str=True)
    unknown = set(docs) - set(sig.parameters)
    if unknown:
        raise ValueError(f"{fn.__name__}: no parameters {sorted(unknown)}")
    params = [
        p.replace(annotation=Annotated[p.annotation, Field(description=docs[p.name])])
        if p.name in docs
        else p
        for p in sig.parameters.values()
    ]

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await fn(*args, **kwargs)

    wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


def build_tools(cfg: StoreConfig, adapter: ShopAdapter, cart: CartStrategy) -> list[ToolSpec]:
    """Canonical tool set filtered by `spec.requires <= adapter.capabilities()` and
    `suffix not in cfg.tools.disabled`. Implemented in stage 1B."""
    raise NotImplementedError
