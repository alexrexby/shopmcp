"""The form_post trampoline: `GET /cart/h/{token}` -> a page that POSTs the cart to the store.

`single`: one form with every line, submitted on load. `chain`: the forms go as JSON in a
`<script type="application/json">`; one fixed script submits the next form on every visit
and counts in `sessionStorage["mcp:" + jti]`; the store redirects back to the Referer (the
full URL of this page: `Referrer-Policy: unsafe-url`), and after the last form the page goes
to `finish_url`. Without JavaScript the same forms are buttons.

Ids in the token are any printable text (cart/refs.py), so the page is safe only by
escaping: every attribute and text through `html.escape(quote=True)`, the JSON with `<`,
`>`, `&` as `\\u003c...` and non-ASCII as `\\uXXXX`, and the script builds the forms with
`createElement` and `.name`/`.value`, never with markup. `action` and `finish_url` come from
the config only (checked to stay on the site), never from the token. The scripts and the
style are constants: CSP allows them by hash, nothing else runs on the page.
"""

import base64
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from shopmcp.cart import form_post
from shopmcp.cart.token import TokenError, verify
from shopmcp.config import FormPostCart, StoreConfig
from shopmcp.i18n import L10n, Texts, define

log = logging.getLogger(__name__)

STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:34em;margin:3em auto;padding:0 1em}"
    "button{font:inherit;padding:.5em 1em;margin:.25em 0}"
)
# HTMLFormElement.prototype.submit: a field named `submit` would shadow form.submit.
SINGLE_SCRIPT = 'HTMLFormElement.prototype.submit.call(document.getElementById("mcp-form"));'
CHAIN_SCRIPT = """(function () {
  var data = JSON.parse(document.getElementById("mcp-chain").textContent);
  var key = "mcp:" + data.jti;
  var next = Number(sessionStorage.getItem(key) || 0);
  if (next >= data.forms.length) {
    location.replace(data.finish);
    return;
  }
  sessionStorage.setItem(key, String(next + 1));
  var form = document.createElement("form");
  form.method = "post";
  form.action = data.action;
  data.forms[next].forEach(function (pair) {
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = pair[0];
    input.value = pair[1];
    form.appendChild(input);
  });
  document.body.appendChild(form);
  HTMLFormElement.prototype.submit.call(form);
})();"""

_TITLE = define(
    "trampoline.title",
    ru="Переход в корзину {brand}",
    en="Going to the {brand} cart",
)
_GO = define(
    "trampoline.go",
    ru="Перейти в корзину {brand}",
    en="Go to the {brand} cart",
)
_STEP = define(
    "trampoline.chain.step",
    ru="Добавить позицию {n} из {total}",
    en="Add line {n} of {total}",
)
_CHAIN_HINT = define(
    "trampoline.chain.hint",
    ru="Если переход не начался сам, нажмите кнопки по очереди, затем откройте корзину.",
    en="If nothing happens, press the buttons in turn, then open the cart.",
)
_INVALID = define(
    "trampoline.invalid",
    ru="Ссылка на корзину устарела или повреждена. Попросите ассистента собрать корзину заново.",
    en="The cart link has expired or is damaged. Ask the assistant to build the cart again.",
)
_TO_SITE = define(
    "trampoline.to_site",
    ru="Перейти на сайт {brand}",
    en="Go to the {brand} website",
)


def _sha256(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode()).digest()).decode() + "'"


def script_json(data: object) -> str:
    """JSON safe inside `<script>`: no `</script>`, `<!--` or `&`, ASCII only (U+2028/2029
    included), so neither the HTML parser nor an old JS engine can end the string early."""
    text = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _inputs(pairs: Sequence[tuple[str, str]]) -> str:
    return "".join(
        f'<input type="hidden" name="{escape(n, quote=True)}" value="{escape(v, quote=True)}">'
        for n, v in pairs
    )


def _page(lang: str, title: str, body: str, script: str | None) -> str:
    tail = f"<script>{script}</script>" if script else ""
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}{tail}</body></html>"
    )


def _headers(csp: str, referrer: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": csp,
        "Referrer-Policy": referrer,
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex",
        "X-Content-Type-Options": "nosniff",
    }


def _csp(script: str | None, form_action: str) -> str:
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src {_sha256(script)}" if script else "script-src 'none'",
            f"style-src {_sha256(STYLE)}",
            f"form-action {form_action}",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ]
    )


def route(cfg: StoreConfig, texts: Texts) -> Callable[[Request], Awaitable[Response]]:
    """The handler of `form_post.PATH + "{token}"`; `cfg.cart` must be a FormPostCart."""
    cart = cfg.cart
    if not isinstance(cart, FormPostCart):
        raise TypeError("the trampoline serves only cart.strategy = 'form_post'")
    keys = form_post.secrets(cart)
    action = str(cart.action)
    brand = cfg.brand

    def text(key: str, default: L10n, **values: object) -> str:
        line = texts.get(key, default)
        for name, value in {"brand": brand, **values}.items():
            line = line.replace("{" + name + "}", str(value))
        return line

    title = text("trampoline.title", _TITLE)

    def invalid() -> Response:
        link = text("trampoline.to_site", _TO_SITE)
        body = (
            f"<p>{escape(text('trampoline.invalid', _INVALID))}</p>"
            f'<p><a href="{escape(str(cfg.site), quote=True)}">{escape(link)}</a></p>'
        )
        csp = _csp(None, "'none'")
        return HTMLResponse(
            _page(cfg.locale, title, body, None),
            status_code=400,
            headers=_headers(csp, "no-referrer"),
        )

    def single(fields: Sequence[tuple[str, str]]) -> Response:
        body = (
            f'<form id="mcp-form" method="post" action="{escape(action, quote=True)}">'
            f"{_inputs(fields)}"
            f'<button type="submit">{escape(text("trampoline.go", _GO))}</button></form>'
        )
        csp = _csp(SINGLE_SCRIPT, form_post.origin(action))
        # No Referer: the store learns nothing about the link, and the token stays here.
        return HTMLResponse(
            _page(cfg.locale, title, body, SINGLE_SCRIPT),
            headers=_headers(csp, "no-referrer"),
        )

    def chain(jti: str, forms: Sequence[Sequence[tuple[str, str]]]) -> Response:
        finish = str(cart.finish_url)
        buttons = "".join(
            f'<form method="post" action="{escape(action, quote=True)}">{_inputs(f)}'
            f'<button type="submit">{escape(text("trampoline.chain.step", _STEP, n=i, total=len(forms)))}'
            "</button></form>"
            for i, f in enumerate(forms, 1)
        )
        data = {"jti": jti, "action": action, "finish": finish, "forms": forms}
        body = (
            f"<p>{escape(text('trampoline.chain.hint', _CHAIN_HINT))}</p>{buttons}"
            f'<p><a href="{escape(finish, quote=True)}">{escape(text("trampoline.go", _GO))}</a></p>'
            f'<script type="application/json" id="mcp-chain">{script_json(data)}</script>'
        )
        # 'self': the store answers the POST with a redirect back here, and form-action
        # also covers the redirects of a submitted form (Chromium enforces it).
        csp = _csp(CHAIN_SCRIPT, f"{form_post.origin(action)} 'self'")
        # The full URL as Referer: that is where the store sends the browser back to.
        return HTMLResponse(
            _page(cfg.locale, title, body, CHAIN_SCRIPT),
            headers=_headers(csp, "unsafe-url"),
        )

    async def trampoline(request: Request) -> Response:
        try:
            payload = verify(request.path_params["token"], store=cfg.id, secrets=keys)
        except TokenError as e:
            log.info("trampoline: %s", e)  # str(e) never holds the token
            return invalid()
        forms = form_post.forms(cart, payload.lines)
        if forms is None:  # the config changed since the link was made
            return invalid()
        if cart.mode == "single":
            return single(forms[0])
        return chain(payload.jti, forms)

    return trampoline
