"""1C-Bitrix adapter: a stub. It declares no capabilities, so a store on it exposes only
`store_info`: `cart_link_create` needs `details` to check the ids it hands off.

Planned source, not implemented in iteration 1 (method names to be checked against the
store's Bitrix edition before implementing):
- catalog: REST `catalog.product.list` (with offers), ids are `XML_ID`;
- cart hand-off (SHARE_LINK): a basket built through `sale.basket` and shared by link;
- orders (ORDERS): the orders of the user behind the access token.
Adapter keys in `[adapter]`: `portal` (https URL), `webhook` (`${ENV}` only, never inline).
"""

from shopmcp.adapters.base import AdapterBase, AdapterContext
from shopmcp.config import AdapterConfig


class BitrixAdapter(AdapterBase):
    def __init__(self, cfg: AdapterConfig, ctx: AdapterContext) -> None:
        self.cfg = cfg
        self.ctx = ctx


def create(cfg: AdapterConfig, ctx: AdapterContext) -> BitrixAdapter:
    return BitrixAdapter(cfg, ctx)
