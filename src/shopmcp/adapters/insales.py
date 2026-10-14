"""InSales adapter: a stub. It declares no capabilities, so a store on it exposes only
`store_info`: `cart_link_create` needs `details` to check the ids it hands off.

Planned source, not implemented in iteration 1:
- catalog: API `GET /admin/products.json` (paged, `updated_since` for refresh), variants
  carry price, old price, barcode and stock; `GET /admin/products/{id}.json` for details;
- categories: `GET /admin/collections.json`;
- cart hand-off: the storefront cart URL template (`url_template` strategy in `[cart]`).
Adapter keys in `[adapter]`: `shop` (https URL), `api_key` and `password` (`${ENV}` only).
"""

from shopmcp.adapters.base import AdapterBase, AdapterContext
from shopmcp.config import AdapterConfig


class InsalesAdapter(AdapterBase):
    def __init__(self, cfg: AdapterConfig, ctx: AdapterContext) -> None:
        self.cfg = cfg
        self.ctx = ctx


def create(cfg: AdapterConfig, ctx: AdapterContext) -> InsalesAdapter:
    return InsalesAdapter(cfg, ctx)
