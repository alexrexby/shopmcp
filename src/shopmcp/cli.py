"""Command line: serve | index | check | scaffold.

Messages here are in English: a developer reads them, not the model. Server, adapter
and conformance modules are imported lazily, so a broken or removed one fails only the
command that needs it.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import textwrap
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from shopmcp.config import MIN_SECRET_BYTES, ConfigError, StoreConfig, load
from shopmcp.engines import CART_SECRET, PRESETS, Engine, Preset
from shopmcp.i18n import LOCALES, L10n

if TYPE_CHECKING:
    from shopmcp.detect import Detection

# Placeholder the conformance search must fail on until the store owner edits it.
SAMPLE_QUERY_PLACEHOLDER = "CHANGE-ME"
FEED_PLACEHOLDER = "CHANGE-ME"  # no --feed: the feed URL is the owner's to fill in
CART_PATH_DEFAULT = "cart"  # PrestaShop's cart path in an English storefront
# Where adapters keep their index; `serve` and `index` must agree on it. /data in the image.
DATA_DIR_ENV = "SHOPMCP_DATA_DIR"


def _data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV) or "data")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from shopmcp.server.app import build_app

    cfg = load(args.config)
    # proxy_headers=False: the client address is rewritten only by our rate limiter,
    # and only for `server.trusted_proxies`; uvicorn would trust its own list instead.
    app = build_app(cfg, data_dir=_data_dir(), config_dir=Path(args.config).resolve().parent)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, proxy_headers=False)
    return 0


def _index(args: argparse.Namespace) -> int:
    from shopmcp.adapters import registry
    from shopmcp.adapters.base import AdapterContext
    from shopmcp.server.app import HTTP_TIMEOUT

    cfg = load(args.config)
    ctx = AdapterContext(
        data_dir=_data_dir(),
        http_timeout=HTTP_TIMEOUT,
        store_id=cfg.id,
        config_dir=Path(args.config).resolve().parent,
    )
    adapter = registry.create(cfg.adapter, ctx)

    async def build() -> dict:
        await adapter.start()
        try:
            return await adapter.health()
        finally:
            await adapter.close()

    print(json.dumps(asyncio.run(build()), ensure_ascii=False, default=str))
    return 0


def _check(args: argparse.Namespace) -> int:
    from shopmcp.conformance import runner

    # The report is the output: SDK session INFO lines for every request would drown it.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    if not args.config and not args.url:
        print("error: check needs a store.toml, --url, or both", file=sys.stderr)
        return 2
    cfg = load(args.config) if args.config else None
    if args.url:
        # With a config the live endpoint is held to it; without one, expectations are inferred.
        results = runner.check_url(args.url, cfg)
    else:
        assert cfg is not None
        remote = sorted(
            k for k, v in (cfg.adapter.model_extra or {}).items() if str(v).startswith("https://")
        )
        if remote and not args.online:
            print(
                f"error: the store reads adapter.{', adapter.'.join(remote)} over the network;"
                " rerun with --online",
                file=sys.stderr,
            )
            return 2
        timeout = args.start_timeout
        if timeout is None:
            timeout = runner.ONLINE_START_TIMEOUT if args.online else runner.START_TIMEOUT
        results = runner.check_config(cfg, Path(args.config).resolve().parent, timeout)
    print(runner.report(results))
    return 1 if any(r.status == "fail" for r in results) else 0


def _toml_str(value: str) -> str:
    # JSON string escapes (\" \\ \n \uXXXX) are valid in a TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_str(str(value))


def _comment(text: L10n | str, prefix: str = "") -> list[str]:
    """`# en` then `# ru` lines, wrapped; a plain string is one language-neutral comment."""
    parts = [text.en, text.ru] if isinstance(text, L10n) else [text]
    lines = []
    for part in parts:
        lines += textwrap.wrap(prefix + part, 88, initial_indent="# ", subsequent_indent="# ")
    return lines


def _line(key: str, value: Any, comment: str | None = None, *, commented: bool = False) -> str:
    text = f"{'# ' if commented else ''}{key} = {_toml_value(value)}"
    return f"{text:<35} # {comment}" if comment else text


def _fill(value: Any, slots: Mapping[str, str]) -> Any:
    """Replace the preset's scaffold-time placeholders (`{origin}`, `{host}`, `{base}`, `{feed}`,
    `{cart_path}`)."""
    if isinstance(value, str):
        for name, repl in slots.items():
            value = value.replace("{" + name + "}", repl)
        return value
    if isinstance(value, list | tuple):
        return [_fill(v, slots) for v in value]
    return value


# What the owner fills in by hand, per preset `edit` key.
_EDIT = {
    "adapter.feed": L10n(
        en="feed URL from the store admin (https) or a path from this file",
        ru="URL фида из админки магазина (https) или путь от этого файла",
    ),
    "cart.secret": L10n(
        en=f"set SHOPMCP_CART_SECRET (>= {MIN_SECRET_BYTES} bytes) in the environment",
        ru=f"задайте SHOPMCP_CART_SECRET (>= {MIN_SECRET_BYTES} байт) в окружении",
    ),
    "cart.line_template": L10n(
        en=f"the cart path of the storefront language ({CART_PATH_DEFAULT}, panier...)",
        ru=f"путь корзины на языке витрины ({CART_PATH_DEFAULT}, panier...)",
    ),
    "adapter.cart_ref": L10n(
        en="how a feed id maps to the cart key: check the g:id/offer id of your feed "
        "against [adapter.cart_ref] below",
        ru="как id фида соответствует ключу корзины: сверьте g:id/offer id своего фида "
        "с [adapter.cart_ref] ниже",
    ),
}
# Inline notes on the [cart] lines a preset marks for editing.
_CART_EDIT = {
    "secret": "EDIT: SHOPMCP_CART_SECRET >= 32 bytes, environment only / только в окружении",
    "line_template": "EDIT: cart path / путь корзины",
}
_BETA = L10n(
    en="BETA: form_post is checked in Chromium only (check V1), not in Safari. Host this "
    "server on the store's site (server.public_url on a subdomain of the store): from "
    "another site the store keeps the customer's cart only if its cart cookie is "
    "SameSite=None on https, otherwise every POST starts a new cart; InSales replaces "
    "the cart from another site always. `shopmcp check` C11 fails or warns.",
    ru="БЕТА: form_post проверен только в Chromium (проверка V1), в Safari нет. Размещайте "
    "сервер на сайте магазина (server.public_url на поддомене магазина): с другого сайта "
    "магазин сохраняет корзину покупателя, только если его кука корзины SameSite=None на "
    "https, иначе каждый POST заводит новую корзину; InSales с другого сайта заменяет "
    "корзину всегда. `shopmcp check` C11 даёт провал или предупреждение.",
)
_HTTP_STORE = L10n(
    en="The store is on http: its cookie cannot be SameSite=None, so form_post works only "
    "from a subdomain of the store.",
    ru="Магазин на http: его кука не бывает SameSite=None, поэтому form_post работает "
    "только с поддомена магазина.",
)


def _form_post_notes(site: str) -> list[L10n]:
    http = urllib.parse.urlsplit(site).scheme.lower() == "http"
    return [_BETA, _HTTP_STORE] if http else [_BETA]


_NO_ADAPTER = L10n(
    en="No catalog adapter for this engine. The cart tool needs a catalog: a Google "
    "Merchant feed (from an app) goes here; Atom feeds are not supported.",
    ru="Адаптера каталога для этого движка нет. Инструменту корзины нужен каталог: сюда "
    "— фид Google Merchant (из приложения); Atom-фиды не поддерживаются.",
)
_HEADLESS = L10n(
    en="line_template removed: a headless storefront ignores add-to-cart links.",
    ru="line_template убран: безголовая витрина не понимает ссылки-действия.",
)


def _edit_keys(preset: Preset | None, feed: str | None) -> list[str]:
    keys = list(preset.edit) if preset else []
    reads_feed = preset is None or preset.adapter is None or "feed" in preset.adapter
    if reads_feed and feed is None and "adapter.feed" not in keys:
        keys.insert(0, "adapter.feed")
    if feed is not None and "adapter.feed" in keys:
        keys.remove("adapter.feed")  # given on the command line
    return keys


def _adapter_lines(preset: Preset | None, feed: str | None, slots: Mapping[str, str]) -> str:
    feed_note = "https URL or path from this file / https-URL или путь от этого файла"
    edit = "EDIT: " if feed is None else ""
    if preset is None:
        return "\n".join(
            [
                _line("kind", "yml"),
                _line("feed", feed or FEED_PLACEHOLDER, edit + feed_note),
                _line("refresh_minutes", 60),
                _line(
                    "stale_minutes",
                    180,
                    "older index: /healthz 503 / индекс старше",
                    commented=True,
                ),
            ]
        )
    lines = []
    adapter = preset.adapter
    if adapter is None:
        lines += _comment(_NO_ADAPTER)
        adapter = {"kind": "feed", "feed": "{feed}"}
    for key, value in adapter.items():
        note = None
        if key == "feed":
            note = ("EDIT: " if "adapter.feed" in _edit_keys(preset, feed) else "") + feed_note
        lines.append(_line(key, _fill(value, slots), note))
    if preset.cart_ref is not None:
        lines += ["", *_comment(_EDIT["adapter.cart_ref"], "EDIT: "), "[adapter.cart_ref]"]
        lines += [_line(k, v) for k, v in preset.cart_ref.items()]
    elif "adapter.cart_ref" in preset.edit:
        lines += [
            "",
            *_comment(_EDIT["adapter.cart_ref"], "EDIT: "),
            "# [adapter.cart_ref]",
            '# pattern = "^(?P<product_id>[0-9]+)-(?P<variant_id>[0-9]+)$"',
            '# options = { Size = "size" }',
        ]
    return "\n".join(lines)


def _cart_lines(preset: Preset | None, slots: Mapping[str, str], *, headless: bool) -> str:
    if preset is None:
        return "\n".join(
            [
                _line("strategy", "link_list"),
                '# url_template = "https://shop.example/cart/add?{items}"',
                '# item_template = "id[]={id}&qty[]={qty}"',
                '# item_separator = "&"',
            ]
        )
    cart = dict(preset.cart)
    lines = []
    if headless and "line_template" in cart:
        del cart["line_template"]
        lines += _comment(_HEADLESS)
    if cart["strategy"] == "form_post":
        lines += [c for note in _form_post_notes(slots["origin"]) for c in _comment(note)]
    for key, value in cart.items():
        note = _CART_EDIT.get(key, "EDIT") if f"cart.{key}" in preset.edit else None
        lines.append(_line(key, _fill(value, slots), note))
    if preset.alt_cart and preset.alt_cart_when:
        when = preset.alt_cart_when
        lines += [
            "",
            *_comment(L10n(en=f"Instead, when: {when.en}", ru=f"Вместо, если: {when.ru}")),
        ]
        if preset.alt_cart["strategy"] == "form_post":
            lines += [c for note in _form_post_notes(slots["origin"]) for c in _comment(note)]
        lines += [_line(k, _fill(v, slots), commented=True) for k, v in preset.alt_cart.items()]
    if up := preset.cart_upgrade:
        up = L10n(en=f"Upgrade: {_fill(up.en, slots)}", ru=f"Апгрейд: {_fill(up.ru, slots)}")
        lines += ["", *_comment(up)]
    return "\n".join(lines)


def _engine_lines(preset: Preset | None) -> str:
    if preset is None:
        return ""
    lines = [f"# Engine / Движок: {preset.name} ({preset.engine.value})"]
    lines += _comment(preset.notes)
    return "\n".join(lines) + "\n"


def _host(site: str) -> str:
    parts = urllib.parse.urlsplit(site)
    host = (parts.hostname or "").lower()
    return f"{host}:{parts.port}" if parts.port else host


def _origin(site: str) -> str:
    """Scheme, host and port of `site`: a store on plain http keeps http."""
    return f"{urllib.parse.urlsplit(site).scheme.lower()}://{_host(site)}"


def _base(site: str) -> str:
    """The origin and the path of `site` without the trailing slash: WordPress in `/shop/`."""
    return _origin(site) + urllib.parse.urlsplit(site).path.rstrip("/")


def render_store_toml(
    store_id: str,
    *,
    site: str,
    feed: str | None = None,
    prefix: str | None = None,
    locale: str = "ru",
    engine: Engine | None = None,
    headless: bool = False,
) -> str:
    """Store config text from the packaged template; validated before it is returned.

    With `engine`, `[adapter]` and `[cart]` come from its preset in `engines.py`;
    without, a YML feed and product page links, which fit any store."""
    if prefix is None:
        prefix = re.sub(r"[^a-z0-9]", "", store_id)[:16]
    preset = PRESETS[engine] if engine else None
    slots = {
        "origin": _origin(site),
        "host": _host(site),
        "base": _base(site),
        "cart_path": CART_PATH_DEFAULT,
        "feed": feed or FEED_PLACEHOLDER,
    }
    template = Template((files("shopmcp") / "templates" / "store.toml.tmpl").read_text("utf-8"))
    text = template.substitute(
        id=_toml_str(store_id),
        prefix=_toml_str(prefix),
        brand=_toml_str(store_id),
        site=_toml_str(site),
        locale=_toml_str(locale),
        locale_name=locale,
        engine=_engine_lines(preset),
        adapter=_adapter_lines(preset, feed, slots),
        cart=_cart_lines(preset, slots, headless=headless),
        sample_query=_toml_str(SAMPLE_QUERY_PLACEHOLDER),
    )
    raw = tomllib.loads(text)
    # No ${ENV} substitution: the feed may reference a variable not set here. The cart
    # secret is one by construction; a stand-in of valid length lets the rest be checked.
    if raw.get("cart", {}).get("secret") == CART_SECRET:
        raw["cart"]["secret"] = "0" * MIN_SECRET_BYTES
    try:
        StoreConfig.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"scaffold: {problems}") from None
    return text


def _summary(
    engine: Engine | None, detection: "Detection | None", feed: str | None, site: str
) -> list[str]:
    """What scaffold found and what the owner still has to fill in."""
    out = []
    if detection is not None:
        if engine is None:
            out.append(f"engine: not determined: {detection.reason}")
            if detection.candidates:
                out.append("  candidates: " + ", ".join(e.value for e in detection.candidates))
        else:
            out.append(f"engine: {PRESETS[engine].name} (detected)")
        for s in detection.signals:
            out.append(f"  sign: {s.engine.value}: {s.what} ({s.weight.value})")
        out += [f"  warning: {w}" for w in detection.warnings]
        if detection.ecwid_store_id:
            out.append(f"  Ecwid store id: {detection.ecwid_store_id}")
    elif engine is not None:
        out.append(f"engine: {PRESETS[engine].name} (--engine)")
    preset = PRESETS[engine] if engine else None
    if preset is None:
        out.append("config: YML feed + product page links; rerun with --engine for a preset")
    else:
        out.append(f"cart: {preset.cart['strategy']}; {preset.notes.en}")
        if "form_post" in (preset.cart["strategy"], (preset.alt_cart or {}).get("strategy")):
            out += [f"  {note.en}" for note in _form_post_notes(site)]
        if feed and preset.adapter is not None and "feed" not in preset.adapter:
            out.append(f"  --feed unused: adapter {preset.adapter['kind']!r} reads no feed")
    keys = _edit_keys(preset, feed)
    out.append("fill in by hand (lines marked EDIT): brand, instructions, server.public_url,")
    out.append("  conformance.sample_query" + "".join(f", {k}" for k in keys))
    for k in keys:
        if k in _EDIT:
            out.append(f"  {k}: {_EDIT[k].en}")
    return out


def _scaffold(args: argparse.Namespace) -> int:
    # Rendering validates the id and the site before any request and before the id
    # becomes part of a path.
    render_store_toml(args.store_id, feed=args.feed, site=args.site, prefix=args.prefix)
    path = Path("stores") / args.store_id / "store.toml"
    if path.exists():  # before the probe: up to 8 requests for nothing; `x` below still guards
        print(f"error: {path} already exists, not overwritten", file=sys.stderr)
        return 1
    engine = Engine(args.engine) if args.engine else None
    detection = None
    if engine is None:
        from shopmcp import detect

        detection = detect.detect(detect.probe(args.site))
        engine = detection.engine
    text = render_store_toml(
        args.store_id,
        feed=args.feed,
        site=args.site,
        prefix=args.prefix,
        locale=args.locale,
        engine=engine,
        headless=bool(detection and detection.headless),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        print(f"error: {path} already exists, not overwritten", file=sys.stderr)
        return 1
    print("\n".join(_summary(engine, detection, args.feed, args.site)))
    print(f"created {path}; edit the lines marked EDIT")
    return 0


def _seconds(value: str) -> float:
    seconds = float(value)  # ValueError: argparse says "invalid _seconds value"
    if not seconds > 0:  # NaN too
        raise argparse.ArgumentTypeError(f"must be a number of seconds above 0, got {value}")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shopmcp", description="MCP server for an online store")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "serve", help=f"run the MCP server over HTTP (index in ${DATA_DIR_ENV}, default ./data)"
    )
    p.add_argument("config", help="path to store.toml")
    p.set_defaults(func=_serve)

    p = sub.add_parser("index", help=f"build the search index once into ${DATA_DIR_ENV} and exit")
    p.add_argument("config", help="path to store.toml")
    p.set_defaults(func=_index)

    p = sub.add_parser(
        "check", help="run the conformance checks (store.toml: served locally; --url: live)"
    )
    p.add_argument("config", nargs="?", help="path to store.toml; with --url, the expectations")
    p.add_argument("--url", help="URL of a running MCP endpoint, e.g. https://shop.ru/mcp")
    p.add_argument(
        "--online", action="store_true", help="let a local run fetch the store's https sources"
    )
    p.add_argument(
        "--start-timeout",
        type=_seconds,
        metavar="SECONDS",
        help="how long a local run waits for the first index (default 60, with --online 1860)",
    )
    p.set_defaults(func=_check)

    p = sub.add_parser(
        "scaffold",
        help="create stores/<store_id>/store.toml; the engine is detected from the site (GET only)",
    )
    p.add_argument("store_id")
    p.add_argument("--site", required=True, help="store website URL")
    p.add_argument(
        "--engine",
        choices=[e.value for e in Engine],
        help="store engine: skips detection, takes its cart and catalog preset",
    )
    p.add_argument("--feed", help="catalog feed (YML or Google Merchant): path or https URL")
    p.add_argument("--prefix", help="tool name prefix (default: store id without dashes)")
    p.add_argument("--locale", choices=LOCALES, default="ru")
    p.set_defaults(func=_scaffold)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
