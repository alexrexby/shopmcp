"""Engine detection (plan-engines.md §2.7): synthetic evidence per engine, a positive and
its nearest negative; the network part only against a local server."""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from shopmcp import detect as d
from shopmcp.detect import Evidence, Fetch, Weight, detect, probe, probe_urls
from shopmcp.engines import Engine

SITE = "https://shop.example"
PS_COOKIE = "PrestaShop-0123456789abcdef0123456789abcdef=x; path=/"


def page(body: str = "<html></html>", status: int = 200, **headers: str) -> Fetch:
    """A response; header keyword `set_cookie` may hold several cookies split by `|`."""
    pairs = []
    for name, value in headers.items():
        for v in value.split("|"):
            pairs.append((name.replace("_", "-").lower(), v))
    return Fetch(url=SITE + "/", status=status, headers=tuple(pairs), body=body)


def json_page(body: str, status: int = 200, **headers: str) -> Fetch:
    return page(body, status, content_type="application/json; charset=utf-8", **headers)


NOT_FOUND = page("<html>404</html>", 404, content_type="text/html")


def run(home: Fetch, **probes: Fetch) -> d.Detection:
    return detect(Evidence(site=SITE, home=home, probes={Engine(k): v for k, v in probes.items()}))


# --- one positive and its nearest negative per engine ---------------------------------------


def test_bitrix_by_header():
    got = run(page("<script src='/bitrix/js/main/core.js'>", x_powered_cms="Bitrix Site Manager"))
    assert got.engine is Engine.BITRIX and not got.headless and not got.warnings


def test_bitrix_with_a_nextjs_catalog_is_headless():
    # The home page stays classic; the catalog page linked from it is rendered by Next.js.
    home = page('<a href="/catalog/new/">', x_powered_cms="Bitrix Site Manager (DEMO)")
    got = run(home, bitrix=page('<script src="/_next/static/chunks/main.js">'))
    assert got.engine is Engine.BITRIX and got.headless
    assert any("Next.js" in w for w in got.warnings)


def test_bitrix_without_header_is_a_weak_guess():
    got = run(page("<script>BX.message({})</script>"))
    assert got.engine is Engine.BITRIX
    assert any("weak signs only" in w for w in got.warnings)


def test_insales_by_cart_items_json():
    home = page(
        "<img src='//cdn.insales-shop.ru/files/1.png'>", set_cookie="first_current_location=1"
    )
    got = run(home, insales=json_page('{"items_count":0,"order_lines":[],"total_price":0}'))
    assert got.engine is Engine.INSALES


def test_insales_theme_with_woocommerce_classes_stays_insales():
    # Seen live: an InSales theme ported from WordPress keeps `woocommerce-js` classes.
    home = page('<body class="woocommerce-js"><img src="//cdn.insales-shop.ru/a.png">')
    got = run(
        home,
        insales=json_page('{"order_lines":[]}'),
        woocommerce=NOT_FOUND,
        magento=NOT_FOUND,
    )
    assert got.engine is Engine.INSALES and not got.candidates


def test_cart_items_json_of_another_app_is_not_insales():
    got = run(page(), insales=json_page('{"items":[]}'))
    assert got.engine is None and got.reason


def test_opencart_by_api_route():
    home = page("<link href='catalog/view/theme/x/style.css'>", set_cookie="OCSESSID=abc")
    got = run(home, opencart=json_page('{"error":{"warning":"no permission"}}'))
    assert got.engine is Engine.OPENCART
    assert {s.weight for s in got.signals if s.engine is Engine.OPENCART} == set(Weight)


def test_opencart_json_after_php_notices():
    body = (
        "<b>Notice</b>: Undefined index: api_token in <b>/x/catalog/controller/startup/session.php"
    )
    body += '</b>{"error":{"warning":"Warning: You do not have permission to access the API!"}}'
    got = run(page(set_cookie="OCSESSID=1"), opencart=page(body, content_type="text/html"))
    assert got.engine is Engine.OPENCART and not got.warnings


def test_opencart_route_answered_by_a_catch_all_page_is_not_opencart():
    got = run(page(), opencart=page("<html>home</html>"))
    assert got.engine is None


def test_tilda_store():
    home = page(
        '<div class="t706"><script src="https://static.tildacdn.com/js.js">', x_tilda_server="7"
    )
    got = run(home)
    assert got.engine is Engine.TILDA and not got.warnings


def test_tilda_without_store_warns():
    got = run(page("<script src='https://static.tildacdn.com/js.js'>", x_tilda_server="7"))
    assert got.engine is Engine.TILDA
    assert any("without a Store" in w for w in got.warnings)


def test_shop_script_by_api_token_required():
    home = page("<link href='/wa-data/public/shop/themes/x/a.css'>")
    got = run(home, shop_script=json_page('{"error":"token_required"}', 400))
    assert got.engine is Engine.SHOP_SCRIPT


def test_api_php_of_another_engine_is_not_shop_script():
    got = run(page(), shop_script=json_page('{"error":"not_found"}', 404))
    assert got.engine is None


def test_ecwid_store_id_from_the_script():
    got = run(page('<script src="https://app.ecwid.com/script.js?1003&data_platform=x">'))
    assert got.engine is Engine.ECWID and got.ecwid_store_id == "1003"


def test_ecwid_loaded_by_a_script_is_not_denied():
    got = run(page("<div id='builder-root'></div>"))
    assert got.engine is None and got.ecwid_store_id is None
    assert any("Ecwid" in w for w in got.warnings)  # unseen is not absent


def test_shopify_by_header():
    got = run(page("<script src='https://cdn.shopify.com/s/a.js'>", powered_by="Shopify"))
    assert got.engine is Engine.SHOPIFY


def test_shopify_buy_button_on_a_woocommerce_site_is_woocommerce():
    home = page("<script src='https://cdn.shopify.com/buy-button.js'>")
    got = run(home, woocommerce=json_page("[]", x_wp_total="12"))
    assert got.engine is Engine.WOOCOMMERCE


def test_woocommerce_by_store_api():
    got = run(page(), woocommerce=json_page('[{"id":1}]', x_wp_total="40"))
    assert got.engine is Engine.WOOCOMMERCE


def test_wordpress_without_woocommerce():
    home = page(link='<https://shop.example/wp-json/>; rel="https://api.w.org/"')
    got = run(home, woocommerce=json_page('{"code":"rest_no_route"}', 404), magento=NOT_FOUND)
    assert got.engine is None
    assert any("WordPress without" in w for w in got.warnings)


def test_woocommerce_probe_follows_wordpress_in_a_subdirectory():
    home = page(link='<https://shop.example/blog/wp-json/>; rel="https://api.w.org/"')
    urls = probe_urls(home, SITE)
    assert urls[Engine.WOOCOMMERCE] == SITE + "/blog/wp-json/wc/store/v1/products?per_page=1"
    foreign = page(link='<https://evil.example/wp-json/>; rel="https://api.w.org/"')
    assert probe_urls(foreign, SITE)[Engine.WOOCOMMERCE].startswith(SITE + "/wp-json/")


def test_magento_by_graphql_store_config():
    got = run(page(), magento=json_page('{"data":{"storeConfig":{"store_code":"default"}}}'))
    assert got.engine is Engine.MAGENTO


def test_another_graphql_server_is_not_magento():
    got = run(page(), magento=json_page('{"errors":[{"message":"Cannot query field"}]}'))
    assert got.engine is None


def test_prestashop_17_by_cookie():
    got = run(page("<script>var prestashop = {};</script>", set_cookie=PS_COOKIE))
    assert got.engine is Engine.PRESTASHOP and not got.warnings


def test_prestashop_16_warns():
    got = run(page("<link href='/themes/default-bootstrap/a.css'>", set_cookie=PS_COOKIE))
    assert got.engine is Engine.PRESTASHOP
    assert any("1.6" in w for w in got.warnings)


def test_prestashop_like_cookie_without_md5_is_not_decisive():
    got = run(page(set_cookie="PrestaShop-session=x"))
    assert got.engine is None


# --- the judgement rules ---------------------------------------------------------------------


def test_two_decisive_signs_give_candidates_without_a_choice():
    home = page(x_powered_cms="Bitrix Site Manager")
    got = run(home, woocommerce=json_page("[]", x_wp_total="1"))
    assert got.engine is None
    assert set(got.candidates) == {Engine.BITRIX, Engine.WOOCOMMERCE}
    assert "several engines" in (got.reason or "")


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_antibot_status_is_unknown_not_absent(status):
    got = run(page("<html>blocked</html>", status, x_powered_cms="Bitrix Site Manager"))
    assert got.engine is None and not got.candidates and not got.signals
    assert "antibot" in (got.reason or "") and "--engine" in (got.reason or "")


def test_challenge_page_with_200_is_unknown():
    got = run(page("<html><title>Just a moment...</title></html>"))
    assert got.engine is None and "antibot" in (got.reason or "")


def test_recaptcha_on_a_normal_page_is_not_an_antibot():
    body = "<script src='https://www.google.com/recaptcha/api.js'></script>"
    got = run(page(body, powered_by="Shopify"))
    assert got.engine is Engine.SHOPIFY


def test_unreachable_site():
    got = run(Fetch(url=SITE, error="URLError: timed out"))
    assert got.engine is None and "unreachable" in (got.reason or "")


def test_redirect_to_another_host_warns():
    home = Fetch(url="https://www.shop.example/", status=200, headers=(("powered-by", "Shopify"),))
    got = run(home)
    assert got.engine is Engine.SHOPIFY
    assert any("www.shop.example" in w for w in got.warnings)


# --- probe(): what it asks for -------------------------------------------------------------


def test_probe_stops_after_a_closed_home_page():
    asked = []
    ev = probe(SITE, lambda url: asked.append(url) or page("", 403))
    assert asked == [SITE + "/"] and ev.probes == {}


def test_probe_asks_cheap_probes_and_hinted_ones_only():
    home = page(
        "<a href='/catalog/x/'>/bitrix/js/ cdn.insales-shop.ru catalog/view/theme/ /wa-apps/shop/",
        set_cookie="OCSESSID=1",
    )
    asked = []
    ev = probe(SITE, lambda url: asked.append(url) or (home if len(asked) == 1 else NOT_FOUND))
    assert len(asked) <= d.MAX_REQUESTS
    assert set(ev.probes) == {
        Engine.WOOCOMMERCE,
        Engine.MAGENTO,
        Engine.INSALES,
        Engine.OPENCART,
        Engine.SHOP_SCRIPT,
        Engine.BITRIX,
    }
    plain = []
    probe(SITE, lambda url: plain.append(url) or (page() if len(plain) == 1 else NOT_FOUND))
    assert len(plain) == 3  # home + Store API + GraphQL


# --- fetch(): GET, no cookies, capped body, errors as evidence --------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str | None, str | None]]] = []

    def do_GET(self) -> None:
        self.seen.append((self.command, self.headers.get("Cookie"), self.headers.get("User-Agent")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "sid=1")
            self.end_headers()
            return
        status = 404 if self.path == "/missing" else 200
        body = b"x" * (d.MAX_BODY + 1000) if status == 200 else b"gone"
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "a=1")
        self.send_header("Set-Cookie", "b=2")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.seen = []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_fetch_is_a_capped_get_without_cookies(local_site):
    r = d.fetch(local_site + "/redirect")
    assert r.status == 200 and len(r.body) == d.MAX_BODY
    assert r.cookies() == ["a", "b"]
    assert [m for m, _, _ in _Handler.seen] == ["GET", "GET"]
    assert all(cookie is None for _, cookie, _ in _Handler.seen)  # the redirect's sid not sent
    assert _Handler.seen[0][2] == d.USER_AGENT


def test_fetch_returns_http_errors_and_network_errors(local_site):
    assert (d.fetch(local_site + "/missing").status, d.fetch(local_site + "/missing").body) == (
        404,
        "gone",
    )
    failed = d.fetch("http://127.0.0.1:9/")
    assert failed.status is None and failed.error


# --- live: GET only, sites from the environment (never written into the repo) -------------------


def _live_sites() -> list[tuple[str, str]]:
    raw = os.environ.get("SHOPMCP_DETECT_SITES", "")
    return [tuple(p.split("=", 1)) for p in raw.split(",") if "=" in p]  # type: ignore[misc]


@pytest.mark.online
@pytest.mark.parametrize(
    "expected,site",
    _live_sites()
    or [pytest.param("", "", marks=pytest.mark.skip("SHOPMCP_DETECT_SITES is not set"))],
)
def test_live_detection(expected, site):
    got = detect(probe(site))
    assert got.engine == Engine(expected), (got.reason, got.signals, got.warnings)
