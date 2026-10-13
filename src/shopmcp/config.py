"""Store config: `store.toml` -> `${ENV}` substitution -> pydantic validation.

Secrets live only in the environment and reach the config as `${VAR}`; a missing
variable fails the start instead of silently becoming an empty string.
"""

import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

from shopmcp.i18n import Locale

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TEXT_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class ConfigError(ValueError):
    pass


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdapterConfig(BaseModel):
    """`kind` selects the adapter; every other key belongs to that adapter and is read by it."""

    model_config = ConfigDict(extra="allow")
    kind: str


class CartConfig(Section):
    strategy: Literal["share_link", "url_template", "link_list"] = "link_list"
    url_template: str | None = None  # "https://shop.ru/cart/add?{items}"
    item_template: str | None = None  # "id[]={id}&qty[]={qty}"
    item_separator: str = "&"

    @model_validator(mode="after")
    def _template_complete(self) -> "CartConfig":
        if self.strategy == "url_template":
            if not self.url_template or "{items}" not in self.url_template:
                raise ValueError("cart.url_template must contain {items}")
            if not self.item_template or "{id}" not in self.item_template:
                raise ValueError("cart.item_template must contain {id}")
        return self


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
    cart: CartConfig = CartConfig()
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


def load(path: str | Path) -> StoreConfig:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return StoreConfig.model_validate(_substitute(raw, path.name))
    except ValidationError as e:
        # No input values in the message: after ${ENV} substitution they may be secrets.
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"{path}: {problems}") from None
