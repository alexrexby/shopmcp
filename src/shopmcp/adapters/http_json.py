"""Generic HTTP+JSON adapter: a stub. It declares no capabilities, so a store on it
exposes only the tools every store has (`cart_link_create`, `store_info`).

Planned source, not implemented in iteration 1: any store API that returns JSON. The
store config maps it to the domain model with JSONPath expressions, e.g.
`[adapter.endpoints] search = "https://shop.ru/api/search?q={q}&page={page}"` and
`[adapter.map] items = "$.data.items[*]"`, `id = "$.sku"`, `price = "$.price.value"`.
Capabilities are declared by which endpoints are configured. Credentials via `${ENV}`.
"""

from shopmcp.adapters.base import AdapterBase, AdapterContext
from shopmcp.config import AdapterConfig


class HttpJsonAdapter(AdapterBase):
    def __init__(self, cfg: AdapterConfig, ctx: AdapterContext) -> None:
        self.cfg = cfg
        self.ctx = ctx


def create(cfg: AdapterConfig, ctx: AdapterContext) -> HttpJsonAdapter:
    return HttpJsonAdapter(cfg, ctx)
