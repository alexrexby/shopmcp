"""A recorded WooCommerce Store API behind `urllib.request.urlopen`, for shop.example only.

The JSON files are whole listings in the shape of live responses (trimmed, fictional);
`WooShop` pages them as WordPress does: `per_page`/`page`, `X-WP-Total`,
`X-WP-TotalPages`, 200 `[]` past the end. `faults[(listing, page)]` replaces one answer
with a failure. Any other host goes to the real `urlopen` (conformance talks to a local
server through it).
"""

import http.client
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, Self

HERE = Path(__file__).resolve().parent
SITE = "https://shop.example"
HOST = "shop.example"
API = "/wp-json/wc/store/v1"


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((HERE / f"{name}.json").read_text("utf-8"))


class Response:
    def __init__(
        self,
        body: bytes,
        *,
        ctype: str = "application/json; charset=UTF-8",
        headers: dict[str, str] | None = None,
        cut_at: int | None = None,
    ) -> None:
        self.headers = Message()
        self.headers["Content-Type"] = ctype
        for k, v in (headers or {}).items():
            self.headers[k] = v
        self.status = 200
        self._body = io.BytesIO(body)
        self._cut_at = cut_at

    def read(self, n: int = -1) -> bytes:
        if self._cut_at is not None and self._body.tell() >= self._cut_at:
            raise http.client.IncompleteRead(b"")
        if self._cut_at is not None:
            n = min(n if n >= 0 else self._cut_at, self._cut_at - self._body.tell())
        return self._body.read(n)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(b"{}"))


def _server_error(url: str) -> Response:
    raise http_error(url, 500)


# Ready-made faults: each takes the request URL, returns a Response or raises.
FAULTS: dict[str, Callable[[str], Response]] = {
    "500": _server_error,
    "html": lambda url: Response(
        b"<html><body>Checking your browser</body></html>", ctype="text/html"
    ),
    "cut": lambda url: Response(b'[{"id": 1, "name": "x"' + b" " * 200_000, cut_at=70_000),
    "not_json": lambda url: Response(b'[{"id": 1,'),
    "object": lambda url: Response(b'{"code": "rest_no_route"}'),
}


class WooShop:
    def __init__(self, *, total_pages_header: bool = True) -> None:
        self.listings = {
            "products": load("products"),
            "variations": load("variations"),
            "categories": load("categories"),
        }
        self.total_pages_header = total_pages_header
        self.faults: dict[tuple[str, int], Callable[[str], Response]] = {}
        self.requests: list[urllib.request.Request] = []
        self.real = urllib.request.urlopen

    def install(self, monkeypatch: Any) -> Self:
        monkeypatch.setattr(urllib.request, "urlopen", self.urlopen)
        return self

    def pages(self, listing: str) -> list[int]:
        """Pages of `listing` requested, in order."""
        out = []
        for req in self.requests:
            name, page, _ = self._route(req.full_url)
            if name == listing:
                out.append(page)
        return out

    def urlopen(self, req: Any, *args: Any, **kwargs: Any) -> Any:
        url = req.full_url if isinstance(req, urllib.request.Request) else req
        if urllib.parse.urlsplit(url).hostname != HOST:
            return self.real(req, *args, **kwargs)
        self.requests.append(req)
        name, page, per_page = self._route(url)
        if (name, page) in self.faults:
            return self.faults[(name, page)](url)
        if name is None:
            raise http_error(url, 404)
        items = self.listings[name]
        total = len(items)
        pages = max(1, -(-total // per_page))
        headers = {"X-WP-Total": str(total)}
        if self.total_pages_header:
            headers["X-WP-TotalPages"] = str(pages)
        chunk = items[(page - 1) * per_page : page * per_page]
        return Response(json.dumps(chunk, ensure_ascii=False).encode(), headers=headers)

    @staticmethod
    def _route(url: str) -> tuple[str | None, int, int]:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        page, per_page = int(query.get("page", 1)), int(query.get("per_page", 10))
        if parts.path == f"{API}/products/categories":
            return "categories", page, per_page
        if parts.path == f"{API}/products":
            return (
                ("variations" if query.get("type") == "variation" else "products"),
                page,
                per_page,
            )
        return None, page, per_page
