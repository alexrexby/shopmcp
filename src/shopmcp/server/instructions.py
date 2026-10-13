"""Server `instructions`: the store's own text plus rules generated from its tool set."""

from collections.abc import Sequence

from shopmcp.config import StoreConfig
from shopmcp.i18n import Texts, define
from shopmcp.tools.spec import ToolSpec

# Placeholders are substituted with str.replace, so an override may omit or repeat them.
_INTRO = define(
    "instructions.intro",
    ru="Ты работаешь с каталогом интернет-магазина {brand} ({site}).",
    en="You are working with the catalog of the online store {brand} ({site}).",
)
_CART = define(
    "instructions.cart",
    ru="Заказ здесь не оформляется и не оплачивается. Собери список товаров и вызови "
    "{cart_tool}: он вернёт ссылку, по которой покупатель сам оформит и оплатит заказ на "
    "сайте магазина. Покажи покупателю эту ссылку.",
    en="Orders are not placed or paid here. Collect the product list and call {cart_tool}: it "
    "returns a link where the customer places and pays for the order on the store website. "
    "Show the customer that link.",
)
_SIGN_IN = define(
    "instructions.sign_in",
    ru="Инструменты {personal_tools} требуют входа в аккаунт магазина. Если они вернули 401, "
    "попроси покупателя войти через клиент и повтори вызов; данные аккаунта не запрашивай.",
    en="The tools {personal_tools} require sign-in to the store account. If they return 401, "
    "ask the customer to sign in through the client and retry; never ask for account "
    "credentials.",
)


def _fill(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    return text


def build_instructions(cfg: StoreConfig, specs: Sequence[ToolSpec], texts: Texts) -> str:
    def name(spec: ToolSpec) -> str:
        return f"{cfg.prefix}_{spec.suffix}"

    values = {"brand": cfg.brand, "site": str(cfg.site)}
    parts = [texts.get("instructions.intro", _INTRO)]
    if cfg.instructions.strip():
        parts.append(cfg.instructions.strip())
    cart = [name(s) for s in specs if s.suffix == "cart_link_create"]
    if cart:
        values["cart_tool"] = cart[0]
        parts.append(texts.get("instructions.cart", _CART))
    personal = [name(s) for s in specs if s.scopes]
    if personal:
        values["personal_tools"] = ", ".join(personal)
        parts.append(texts.get("instructions.sign_in", _SIGN_IN))
    return "\n\n".join(_fill(p, values) for p in parts)
