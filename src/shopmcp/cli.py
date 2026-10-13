"""Command line: serve | index | check | scaffold.

Messages here are in English: a developer reads them, not the model. Server, adapter
and conformance modules are imported lazily, so a broken or removed one fails only the
command that needs it.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import tomllib
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path
from string import Template

from pydantic import ValidationError

from shopmcp.config import ConfigError, StoreConfig, load
from shopmcp.i18n import LOCALES

# Placeholder the conformance search must fail on until the store owner edits it.
SAMPLE_QUERY_PLACEHOLDER = "CHANGE-ME"
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
    app = build_app(cfg, data_dir=_data_dir())
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, proxy_headers=False)
    return 0


def _index(args: argparse.Namespace) -> int:
    from shopmcp.adapters import registry
    from shopmcp.adapters.base import AdapterContext
    from shopmcp.server.app import HTTP_TIMEOUT

    cfg = load(args.config)
    ctx = AdapterContext(data_dir=_data_dir(), http_timeout=HTTP_TIMEOUT, store_id=cfg.id)
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
    print("error: conformance checks are not implemented yet (stage 2)", file=sys.stderr)
    return 2


def _toml_str(value: str) -> str:
    # JSON string escapes (\" \\ \n \uXXXX) are valid in a TOML basic string.
    return json.dumps(value, ensure_ascii=False)


def render_store_toml(
    store_id: str, *, feed: str, site: str, prefix: str | None = None, locale: str = "ru"
) -> str:
    """Store config text from the packaged template; validated before it is returned."""
    if prefix is None:
        prefix = re.sub(r"[^a-z0-9]", "", store_id)[:16]
    template = Template((files("shopmcp") / "templates" / "store.toml.tmpl").read_text("utf-8"))
    text = template.substitute(
        id=_toml_str(store_id),
        prefix=_toml_str(prefix),
        brand=_toml_str(store_id),
        site=_toml_str(site),
        locale=_toml_str(locale),
        locale_name=locale,
        feed=_toml_str(feed),
        sample_query=_toml_str(SAMPLE_QUERY_PLACEHOLDER),
    )
    try:
        # No ${ENV} substitution: the feed may legitimately reference a variable not set here.
        StoreConfig.model_validate(tomllib.loads(text))
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"scaffold: {problems}") from None
    return text


def _scaffold(args: argparse.Namespace) -> int:
    # Rendering validates the id against its pattern before it becomes part of a path.
    text = render_store_toml(
        args.store_id, feed=args.feed, site=args.site, prefix=args.prefix, locale=args.locale
    )
    path = Path("stores") / args.store_id / "store.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        print(f"error: {path} already exists, not overwritten", file=sys.stderr)
        return 1
    print(f"created {path}; edit the lines marked EDIT")
    return 0


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

    p = sub.add_parser("check", help="run the conformance checks")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("config", nargs="?", help="path to store.toml (served locally)")
    target.add_argument("--url", help="URL of a running MCP endpoint")
    p.add_argument("--online", action="store_true", help="also run checks that hit the network")
    p.set_defaults(func=_check)

    p = sub.add_parser("scaffold", help="create stores/<store_id>/store.toml")
    p.add_argument("store_id")
    p.add_argument("--feed", required=True, help="YML feed: path or https URL")
    p.add_argument("--site", required=True, help="store website URL")
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
