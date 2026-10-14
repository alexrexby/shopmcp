"""Store config: `store.toml` -> `${ENV}` substitution -> pydantic validation.

Secrets live only in the environment and reach the config as `${VAR}`; a missing
variable fails the start instead of silently becoming an empty string.
"""

import os
import re
import tomllib
import urllib.parse
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    HttpUrl,
    SecretStr,
    Tag,
    ValidationError,
    model_validator,
)

from shopmcp.i18n import Locale

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SECRET_KEYS = ("secret",)  # in [cart]: must be exactly one ${ENV} reference
_TEXT_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class ConfigError(ValueError):
    pass


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdapterConfig(BaseModel):
    """`kind` selects the adapter; every other key belongs to that adapter and is read by it."""

    model_config = ConfigDict(extra="allow")
    kind: str


# A cart line needs one of these to name its product (`{variant_id}` alone may be empty).
_ID_PLACEHOLDERS = ("{id}", "{product_id}")
MIN_SECRET_BYTES = 32


def _has_id(template: str) -> bool:
    return any(p in template for p in _ID_PLACEHOLDERS)


def _strong_secret(v: SecretStr) -> SecretStr:
    if len(v.get_secret_value().encode()) < MIN_SECRET_BYTES:
        raise ValueError(f"must be at least {MIN_SECRET_BYTES} bytes")
    return v


# Signs cart tokens. Only from `${ENV}`: `load()` refuses a literal in the file.
CartSecret = Annotated[SecretStr, AfterValidator(_strong_secret)]
TokenTtl = Annotated[int, Field(ge=5, le=10080)]  # minutes a signed cart link lives


class LinkListCart(Section):
    """Product page links; with `line_template` every line also gets an add-to-cart link."""

    strategy: Literal["link_list"] = "link_list"
    line_template: str | None = None  # "{url}?action=ADD2BASKET&id={id}&quantity={qty}"

    @model_validator(mode="after")
    def _template_complete(self) -> "LinkListCart":
        if self.line_template is not None and not _has_id(self.line_template):
            raise ValueError("cart.line_template must contain {id} or {product_id}")
        return self


class UrlTemplateCart(Section):
    strategy: Literal["url_template"]
    url_template: str  # "https://shop.ru/cart/add?{items}"
    item_template: str  # "id[]={id}&qty[]={qty}"
    item_separator: str = "&"

    @model_validator(mode="after")
    def _template_complete(self) -> "UrlTemplateCart":
        if "{items}" not in self.url_template:
            raise ValueError("cart.url_template must contain {items}")
        if not _has_id(self.item_template):
            raise ValueError("cart.item_template must contain {id} or {product_id}")
        return self


class JsonFragmentCart(Section):
    """The storefront script reads the cart from the URL fragment (Ecwid)."""

    strategy: Literal["json_fragment"]
    url_template: str  # "https://shop.ru/#!/~/cart/create={payload}"
    payload_encoding: Literal["uri", "base64"] = "uri"

    @model_validator(mode="after")
    def _template_complete(self) -> "JsonFragmentCart":
        if "{payload}" not in self.url_template:
            raise ValueError("cart.url_template must contain {payload}")
        return self


class FormPostCart(Section):
    """Our trampoline page POSTs the lines to the store: one form, or a chain of them."""

    strategy: Literal["form_post"]
    action: HttpUrl  # "https://shop.ru/cart_items"
    mode: Literal["single", "chain"]
    # Form field name -> value, both templates; a field rendered empty is not sent.
    fields: Annotated[list[tuple[str, str]], Field(min_length=1, max_length=10)]
    finish_url: HttpUrl | None = None  # where a chain ends: the cart page
    secret: CartSecret
    ttl_minutes: TokenTtl = 1440

    @model_validator(mode="after")
    def _form_complete(self) -> "FormPostCart":
        if not any(_has_id(name) or _has_id(value) for name, value in self.fields):
            raise ValueError("cart.fields must contain {id} or {product_id}")
        if self.mode == "chain" and self.finish_url is None:
            raise ValueError("cart.finish_url is required for mode = 'chain'")
        return self


class ReceiverCart(Section):
    """Our module on the store site takes a signed token and fills the cart."""

    strategy: Literal["receiver"]
    url_template: str  # "https://shop.ru/mcp/cart/?mcp_cart={token}"
    secret: CartSecret
    key_id: str = Field("1", pattern=r"^[A-Za-z0-9._-]{1,32}$")
    ttl_minutes: TokenTtl = 1440

    @model_validator(mode="after")
    def _template_complete(self) -> "ReceiverCart":
        if "{token}" not in self.url_template:
            raise ValueError("cart.url_template must contain {token}")
        return self


class ShareLinkCart(Section):
    strategy: Literal["share_link"]


def _cart_strategy(value: Any) -> str:
    # `[cart]` without `strategy` stays a link list, as before the union.
    if isinstance(value, dict):
        return str(value.get("strategy", "link_list"))
    return str(getattr(value, "strategy", ""))


CartConfig = Annotated[
    Annotated[LinkListCart, Tag("link_list")]
    | Annotated[UrlTemplateCart, Tag("url_template")]
    | Annotated[JsonFragmentCart, Tag("json_fragment")]
    | Annotated[FormPostCart, Tag("form_post")]
    | Annotated[ReceiverCart, Tag("receiver")]
    | Annotated[ShareLinkCart, Tag("share_link")],
    Discriminator(_cart_strategy),
]


def _cart_urls(cart: Any) -> list[tuple[str, str]]:
    """(field, URL) of every cart link the config can produce; each must stay on the site."""
    urls = []
    for name in ("url_template", "line_template", "action", "finish_url"):
        value = getattr(cart, name, None)
        # A line template from `{url}` starts at the product page, which the feed owns.
        if value is not None and not str(value).startswith("{url}"):
            urls.append((name, str(value)))
    return urls


def _on_site(url: str, site: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or not site:
        return False
    return host == site or host.endswith("." + site)


class LimitsConfig(Section):
    max_page_size: int = Field(20, ge=1, le=100)
    max_cart_lines: int = Field(30, ge=1, le=200)
    max_quantity: int = Field(99, ge=1, le=10000)  # per cart line
    max_response_bytes: int = Field(24000, ge=1024)
    rate_per_minute: int = Field(60, ge=1)


class ServerConfig(Section):
    path: str = Field("/mcp", pattern=r"^/[a-z0-9/_-]*$")
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    public_url: HttpUrl  # resource in RFC 9728 and the token audience
    allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*"]
    allowed_origins: list[str] = []
    trusted_proxies: list[str] = []


class AuthConfig(Section):
    enabled: bool = False
    issuer: str = ""
    jwks_uri: str = ""
    # tool suffix -> required scopes; overrides the tool's default. Never empty: a personal
    # tool without a scope would be public, and a public tool needs no entry at all.
    tool_scopes: dict[str, Annotated[list[str], Field(min_length=1)]] = {}

    @model_validator(mode="after")
    def _complete_when_enabled(self) -> "AuthConfig":
        if self.enabled and not (self.issuer and self.jwks_uri):
            raise ValueError("auth.enabled requires auth.issuer and auth.jwks_uri")
        return self


class ToolsConfig(Section):
    disabled: list[str] = []


class ConformanceConfig(Section):
    sample_query: str
    sample_barcode: str | None = None


class StoreConfig(Section):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    prefix: str = Field(pattern=r"^[a-z][a-z0-9]{1,15}$")
    brand: str = Field(min_length=1)
    site: HttpUrl
    locale: Locale = "ru"
    instructions: str = ""
    phone: str | None = None
    delivery: str | None = None
    payment: str | None = None
    regions: list[str] = []
    adapter: AdapterConfig
    cart: CartConfig = LinkListCart()
    limits: LimitsConfig = LimitsConfig()
    server: ServerConfig
    auth: AuthConfig = AuthConfig()
    tools: ToolsConfig = ToolsConfig()
    texts: dict[str, str] = {}  # text key -> override in the store language
    conformance: ConformanceConfig

    @model_validator(mode="after")
    def _text_keys(self) -> "StoreConfig":
        bad = [k for k in self.texts if not re.match(_TEXT_KEY, k)]
        if bad:
            raise ValueError(f"texts: malformed keys {bad}")
        return self

    @model_validator(mode="after")
    def _cart_on_site(self) -> "StoreConfig":
        # A cart link to another host would make the store an open redirect.
        site = (self.site.host or "").lower()
        bad = [name for name, url in _cart_urls(self.cart) if not _on_site(url, site)]
        if bad:
            raise ValueError(f"cart.{', cart.'.join(bad)}: host must be {site} or its subdomain")
        return self


def _substitute(value: Any, where: str) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"{where}: environment variable {name} is not set")
            return os.environ[name]

        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(v, f"{where}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def _secrets_from_env(raw: dict[str, Any], path: Path) -> None:
    """A secret written into the file ends up in git and in backups: refuse it before the
    substitution, while a literal still looks different from `${VAR}`."""
    cart = raw.get("cart")
    if not isinstance(cart, dict):
        return
    for key in _SECRET_KEYS:
        value = cart.get(key)
        if value is not None and not (isinstance(value, str) and _ENV_REF.fullmatch(value)):
            raise ConfigError(f"{path}: cart.{key} must be a ${{ENV}} reference, not a literal")


def load(path: str | Path) -> StoreConfig:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    _secrets_from_env(raw, path)
    try:
        return StoreConfig.model_validate(_substitute(raw, path.name))
    except ValidationError as e:
        # No input values in the message: after ${ENV} substitution they may be secrets.
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"{path}: {problems}") from None
