"""What a store's cart is keyed by. Internal: never on the wire, the model sees `Product.id`.

`Product.id` is the feed id of a purchasable unit; the cart may want something else (a
parent product plus a variant, named options). The adapter derives a `CartRef` per id and
the cart strategy builds its link from it.
"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from shopmcp.errors import InvalidInput

# Any printable text: Cyrillic, `/` and spaces are real store ids. Each strategy escapes
# the id for where it goes (URL path/query/fragment, JSON, form field), so no alphabet here.
MAX_ID_LENGTH = 128
MAX_OPTIONS = 20
MAX_OPTION_VALUE = 200


def _cart_id(value: str) -> str:
    # InvalidInput, not ValueError: pydantic passes it through as is, so cart_link_create
    # tells the model which id cannot go into a link instead of failing with internal_error.
    if not 1 <= len(value) <= MAX_ID_LENGTH:
        raise InvalidInput(details={"invalid_cart_id": value, "reason": "length"})
    if not value.isprintable():  # control, format (bidi) and line separator characters
        raise InvalidInput(details={"invalid_cart_id": value, "reason": "control_character"})
    return value


RefId = Annotated[str, AfterValidator(_cart_id)]
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
