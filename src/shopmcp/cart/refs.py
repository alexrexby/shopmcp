"""What a store's cart is keyed by. Internal: never on the wire, the model sees `Product.id`.

`Product.id` is the feed id of a purchasable unit; the cart may want something else (a
parent product plus a variant, named options). The adapter derives a `CartRef` per id and
the cart strategy builds its link from it.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Ids end up in form field names and URLs: a closed alphabet, nothing to escape wrongly.
ID_RE = r"^[A-Za-z0-9._:-]{1,128}$"
MAX_OPTIONS = 20
MAX_OPTION_VALUE = 200

RefId = Annotated[str, StringConstraints(pattern=ID_RE)]
OptionValue = Annotated[str, StringConstraints(max_length=MAX_OPTION_VALUE)]


class CartRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: RefId  # what the engine calls a product
    variant_id: RefId | None = None
    # Ecwid: option name -> value; OpenCart: option_id -> value_id.
    options: Annotated[dict[str, OptionValue], Field(max_length=MAX_OPTIONS)] = {}

    @property
    def id(self) -> str:
        """The most specific id: the `{id}` placeholder."""
        return self.variant_id or self.product_id
