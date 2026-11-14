"""Тесты реального парсера auto.ru и PlaywrightBrowser (подзадача 3.7).

Сеть не используется: Playwright подменяется ``MagicMock``. Тесты не требуют
Qt и установленного Chromium.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from common.config import Config
from server.parsers import autoru as autoru_module
from server.parsers.autoru import AutoRuParser
from server.parsers.base import (
    Ad,
    BlockedError,
    CaptchaDetectedError,
    ParserError,
)
from server.parsers.browser import (
    STEALTH_SCRIPT,
    PlaywrightBrowser,
)

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
    "transmissions": ["АКПП"],
    "fuels": ["Бензин"],
}


def offer(offer_id: str, price: int, year: int, mileage: int = 50_000) -> dict:
    """JSON-объявление в структуре auto.ru (по публичным примерам)."""
    return {
        "id": offer_id,
        "price_info": {"RUR": price, "currency": "RUR"},
        "documents": {"year": year},
        "state": {"mileage": mileage},
        "vehicle_info": {
            "mark_info": {"name": "BMW", "code": "BMW"},
            "model_info": {"name": "X5", "code": "X5"},
            "tech_param": {
                "transmission": "AUTOMATIC",
                "engine_type": "GASOLINE",
                "displacement": 2998,
            },
        },
    }


def next_data_html(*offers: dict) -> str:
    data = {"props": {"pageProps": {"listing": {"offers": list(offers)}}}}
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data, ensure_ascii=False)}</script></body></html>"
    )


DOM_HTML = """
<html><body>
<div class="ListingItem"><a class="ListingItemTitle__link" href="/cars/used/sale/bmw/x5/1-aa/">BMW X5</a>
<div class="ListingItem__price">3 250 000 ₽</div><div class="ListingItem__year">2019</div>
<div class="ListingItem__kmAge">45 000 км</div>
<div class="ListingItemTechSummaryDesktop__cell">3.0 л / 249 л.с. / Дизель</div>
<div class="ListingItemTechSummaryDesktop__cell">Автомат</div></div>
<div class="ListingItem"><a class="ListingItemTitle__link" href="/cars/used/sale/bmw/x5/2-bb/">BMW X5</a>
<div class="ListingItem__price">2 900 000 ₽</div><div class="ListingItem__year">2018</div></div>
</body></html>
"""


@pytest.fixture()
def parser(tmp_path) -> AutoRuParser:
    return AutoRuParser(Config(tmp_path / "settings.json"))


def make_browser_mock(
    html: str = "<html></html>",
    *,
    page_url: str = "https://auto.ru/cars/bmw/x5/all/",
    status: int = 200,
    response_none: bool = False,
):
    """Подменяет ``PlaywrightBrowser`` в autoru; возвращает (patcher, класс, страница)."""
    tab = MagicMock()
    tab.url = page_url
    tab.content.return_value = html
    response = None if response_none else MagicMock(status=status, url=page_url)
    tab.goto.return_value = response
    browser = MagicMock()
    browser.new_page.return_value = tab
    cls = MagicMock()
    cls.return_value.__enter__.return_value = browser
    cls.return_value.__exit__.return_value = False
    return patch.object(autoru_module, "PlaywrightBrowser", cls), cls, tab


# ----------------------------------------------------------------------
# URL
# ----------------------------------------------------------------------
def test_build_url_contains_all_filters(parser):
    url = parser._build_url(SPEC, {}, 1)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/cars/bmw/x5/all/"
    assert query["year_from"] == ["2018"] and query["year_to"] == ["2021"]
    assert query["transmission"] == ["AUTOMATIC"]
    assert query["fuel_type"] == ["PETROL"]
    assert query["page"] == ["1"]


def test_build_url_page_and_multiple_values(parser):
    spec = dict(SPEC, transmissions=["АКПП", "Робот"], fuels=["Дизель"],
                manufacturer="Land Rover", model="Range Rover")
    url = parser._build_url(spec, {}, 3)
    assert "/cars/land_rover/range_rover/all/" in url
    query = parse_qs(urlparse(url).query)
    assert query["transmission"] == ["AUTOMATIC", "AMT"]
    assert query["page"] == ["3"]


def test_build_url_skips_unknown_filters(parser):
    spec = dict(SPEC, transmissions=["Неизвестная"], fuels=[])
    query = parse_qs(urlparse(parser._build_url(spec, {}, 1)).query)
    assert "transmission" not in query and "fuel_type" not in query


# ----------------------------------------------------------------------
# fetch_page
# ----------------------------------------------------------------------
def test_fetch_page_parses_next_data(parser):
    html = next_data_html(offer("100-abc", 3_500_000, 2019), offer("101-def", 2_800_000, 2018, 90_000))
    patcher, _, tab = make_browser_mock(html)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert len(ads) == 2 and all(isinstance(a, Ad) for a in ads)
    first = ads[0]
    assert first.url == "https://auto.ru/cars/used/sale/bmw/x5/100-abc/"
    assert (first.price, first.year, first.mileage) == (3_500_000, 2019, 50_000)
    assert (first.manufacturer, first.model) == ("BMW", "X5")
    assert (first.transmission, first.fuel, first.engine_volume) == ("АКПП", "Бензин", 3.0)
    assert first.raw_json["id"] == "100-abc"
    tab.goto.assert_called_once()
    assert tab.goto.call_args.kwargs["wait_until"] == "domcontentloaded"


def test_fetch_page_dedups_and_tolerates_missing_fields(parser):
    sparse = {"id": "200-x", "price_info": {}}
    html = next_data_html(offer("100-abc", 1_000_000, 2020), offer("100-abc", 1_000_000, 2020), sparse)
    patcher, _, _ = make_browser_mock(html)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert len(ads) == 2
    bare = [a for a in ads if "200-x" in a.url][0]
    assert bare.price is None and bare.year is None and bare.mileage is None
    assert bare.engine_volume is None


def test_fetch_page_dom_fallback(parser):
    patcher, _, _ = make_browser_mock(DOM_HTML)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert [a.url for a in ads] == [
        "https://auto.ru/cars/used/sale/bmw/x5/1-aa/",
        "https://auto.ru/cars/used/sale/bmw/x5/2-bb/",
    ]
    assert (ads[0].price, ads[0].year, ads[0].mileage) == (3_250_000, 2019, 45_000)
    assert (ads[0].engine_volume, ads[0].fuel, ads[0].transmission) == (3.0, "Дизель", "АКПП")
    assert ads[1].mileage is None and ads[1].fuel is None


def test_fetch_page_captcha_by_url(parser):
    patcher, _, _ = make_browser_mock(
        "<html></html>", page_url="https://auto.ru/showcaptcha?cc=1"
    )
    with patcher, pytest.raises(CaptchaDetectedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "auto.ru" and "showcaptcha" in exc.value.url


def test_fetch_page_captcha_by_html(parser):
    html = '<html><script src="https://smartcaptcha.yandexcloud.net/x.js"></script></html>'
    patcher, _, _ = make_browser_mock(html)
    with patcher, pytest.raises(CaptchaDetectedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "auto.ru"


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_page_blocked(parser, status):
    patcher, _, _ = make_browser_mock("<html>Доступ ограничен</html>", status=status)
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "auto.ru" and str(status) in exc.value.detail


def test_fetch_page_empty_results(parser):
    # На пустой выдаче сайт может показывать рекомендации — они не результат.
    html = "<html><h1>Ничего не найдено</h1>" + DOM_HTML + "</html>"
    patcher, _, _ = make_browser_mock(html)
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_fetch_page_no_response_raises_parser_error(parser):
    patcher, _, _ = make_browser_mock(response_none=True)
    with patcher, pytest.raises(ParserError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_stop_before_start_makes_no_browser(parser):
    stop = threading.Event()
    stop.set()
    patcher, cls, _ = make_browser_mock()
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []
    cls.assert_not_called()
    cls.return_value.__enter__.assert_not_called()


def test_stop_after_goto_returns_empty(parser):
    stop = threading.Event()
    patcher, _, tab = make_browser_mock(next_data_html(offer("1-a", 1, 2020)))
    tab.goto.side_effect = lambda *a, **k: (stop.set(), MagicMock(status=200, url=tab.url))[1]
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []


def test_parser_reads_browser_settings_from_config(tmp_path):
    config = Config(tmp_path / "s.json")
    config.set("server", "playwright_headless", False)
    config.set("server", "ignore_https_errors", False)
    parser = AutoRuParser(config)
    patcher, cls, _ = make_browser_mock(next_data_html())
    with patcher:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    cls.assert_called_once_with(headless=False, ignore_https_errors=False)


def test_test_fast_mode_and_simulation_do_not_touch_browser(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        ads = parser.fetch_page(SPEC, {"test_fast_mode": True}, 1, threading.Event())
        assert ads and ads[0].raw_json["source"] == "stub"
        with pytest.raises(CaptchaDetectedError):
            parser.fetch_page(SPEC, {"simulate_captcha": "auto.ru"}, 1, threading.Event())
        with pytest.raises(BlockedError):
            parser.fetch_page(SPEC, {"simulate_block": "auto.ru"}, 1, threading.Event())
    cls.assert_not_called()


def test_collect_real_mode_stops_on_empty_page_and_respects_limits(parser):
    pages = iter([
        [Ad(url=f"https://auto.ru/{i}") for i in range(3)],
        [Ad(url=f"https://auto.ru/{i}") for i in range(10, 13)],
        [],
    ])
    seen: list[int] = []
    with patch.object(parser, "fetch_page", side_effect=lambda *a: next(pages)), \
         patch.object(autoru_module, "interruptible_sleep", return_value=True):
        ads = parser.collect(
            SPEC, {"max_pages_per_site": 5, "max_ads_per_site": 4},
            threading.Event(), progress_cb=lambda p, n: seen.append(n),
        )
    assert len(ads) == 4 and seen == [3, 4]


def test_close_is_noop(parser):
    assert parser.close() is None


# ----------------------------------------------------------------------
# PlaywrightBrowser
# ----------------------------------------------------------------------
@pytest.fixture()
def pw():
    """Подменяет ``sync_playwright``; возвращает (playwright, browser, context)."""
    playwright, browser, context = MagicMock(), MagicMock(), MagicMock()
    playwright.chromium.launch.return_value = browser
    browser.new_context.return_value = context
    factory = MagicMock()
    factory.return_value.start.return_value = playwright
    with patch("server.parsers.browser.sync_playwright", factory):
        yield playwright, browser, context


def test_browser_new_context_arguments(pw):
    playwright, browser, _ = pw
    with PlaywrightBrowser(headless=False, ignore_https_errors=True):
        pass
    assert playwright.chromium.launch.call_args.kwargs["headless"] is False
    kwargs = browser.new_context.call_args.kwargs
    assert kwargs["ignore_https_errors"] is True
    assert kwargs["locale"] == "ru-RU"
    assert kwargs["timezone_id"] == "Europe/Moscow"
    assert kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert "Chrome/124" in kwargs["user_agent"] and "Windows NT 10.0" in kwargs["user_agent"]
    assert kwargs["extra_http_headers"]["sec-ch-ua-platform"] == '"Windows"'


def test_browser_ignore_https_errors_false_is_passed(pw):
    _, browser, _ = pw
    with PlaywrightBrowser(ignore_https_errors=False):
        pass
    assert browser.new_context.call_args.kwargs["ignore_https_errors"] is False


def test_browser_route_aborts_heavy_resources(pw):
    _, _, context = pw
    with PlaywrightBrowser():
        pattern, handler = context.route.call_args.args
    assert pattern == "**/*"
    for kind in ("image", "media", "font", "stylesheet"):
        route = MagicMock()
        route.request.resource_type = kind
        handler(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
    for kind in ("document", "script", "xhr", "fetch"):
        route = MagicMock()
        route.request.resource_type = kind
        handler(route)
        route.continue_.assert_called_once()
        route.abort.assert_not_called()


def test_browser_stealth_script_registered(pw):
    _, _, context = pw
    with PlaywrightBrowser():
        pass
    context.add_init_script.assert_called_once_with(STEALTH_SCRIPT)
    for needle in ("navigator, 'webdriver'", "plugins", "languages", "window.chrome"):
        assert needle in STEALTH_SCRIPT


def test_browser_new_page_and_cleanup(pw):
    playwright, browser, context = pw
    with PlaywrightBrowser() as b:
        assert b.new_page() is context.new_page.return_value
    context.close.assert_called_once()
    browser.close.assert_called_once()
    playwright.stop.assert_called_once()


def test_browser_new_page_outside_with_raises():
    with pytest.raises(RuntimeError):
        PlaywrightBrowser().new_page()


def test_browser_cleanup_on_launch_failure(pw):
    playwright, browser, _ = pw
    browser.new_context.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        PlaywrightBrowser().__enter__()
    browser.close.assert_called_once()
    playwright.stop.assert_called_once()


def test_browser_from_config(tmp_path, pw):
    config = Config(tmp_path / "s.json")
    config.set("server", "playwright_headless", False)
    config.set("server", "ignore_https_errors", False)
    playwright, browser, _ = pw
    with PlaywrightBrowser.from_config(config):
        pass
    assert playwright.chromium.launch.call_args.kwargs["headless"] is False
    assert browser.new_context.call_args.kwargs["ignore_https_errors"] is False


def test_default_settings_have_server_section(tmp_path):
    config = Config(tmp_path / "s.json").load()
    assert config.get("server", "ignore_https_errors") is True
    assert config.get("server", "playwright_headless") is True
