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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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


CARD_1 = (
    "BMW X5 III (F15) Рестайлинг\n7 700 000 ₽\n3.0 л / 249 л.с. / Дизель\n"
    "Автоматическая\n2019\n81 000 км\nМосква"
)
CARD_2 = "BMW X5 xDrive30d\n6 450 000 ₽\n3.0 л / 249 л.с. / Бензин\nАКПП\n2018\n120 500 км"


def card(href: str | None, text: str) -> dict:
    """Карточка в виде, который отдаёт ``Locator.evaluate_all`` (href + текст)."""
    return {"href": href, "text": text}


CARDS = [
    card("https://auto.ru/cars/used/sale/bmw/x5/1-aa/", CARD_1),
    card("https://auto.ru/cars/used/sale/bmw/x5/2-bb/", CARD_2),
]
LISTING_HTML = "<html><div class='ListingCars__items'><div>…</div></div></html>"


@pytest.fixture(autouse=True)
def debug_dir(tmp_path, monkeypatch):
    """Дампы HTML пустых страниц — во временный каталог, не в data/debug."""
    path = tmp_path / "debug"
    monkeypatch.setattr(autoru_module, "DEBUG_DIR", path)
    return path


@pytest.fixture()
def parser(tmp_path) -> AutoRuParser:
    return AutoRuParser(Config(tmp_path / "settings.json"))


def make_browser_mock(
    html: str = "<html></html>",
    *,
    page_url: str = "https://auto.ru/cars/bmw/x5/all/",
    status: int = 200,
    response_none: bool = False,
    cards: list | None = None,
):
    """Подменяет ``PlaywrightBrowser`` в autoru; возвращает (patcher, класс, страница)."""
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = cards if cards is not None else []
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
    assert parsed.path == "/moskva/cars/bmw/x5/all/"
    assert query["year_from"] == ["2018"] and query["year_to"] == ["2021"]
    assert query["transmission"] == ["AUTOMATIC"]
    assert query["fuel_type"] == ["PETROL"]
    assert query["page"] == ["1"]


def test_build_url_exact_region_url(parser):
    spec = {"manufacturer": "BMW", "model": "X5", "year_from": 2018, "year_to": 2021}
    assert parser._build_url(spec, {}, 1) == (
        "https://auto.ru/moskva/cars/bmw/x5/all/?year_from=2018&year_to=2021&page=1"
    )


def test_build_url_uses_spec_region(parser):
    url = parser._build_url(dict(SPEC, region="petrozavodsk"), {}, 2)
    assert urlparse(url).path == "/petrozavodsk/cars/bmw/x5/all/"


def test_build_url_empty_region_falls_back_to_default(parser):
    assert urlparse(parser._build_url(dict(SPEC, region=""), {}, 1)).path.startswith(
        "/moskva/cars/"
    )


def test_build_url_page_and_multiple_values(parser):
    spec = dict(SPEC, transmissions=["АКПП", "Робот"], fuels=["Дизель"],
                manufacturer="Land Rover", model="Range Rover")
    url = parser._build_url(spec, {}, 3)
    assert "/moskva/cars/land_rover/range_rover/all/" in url
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
def test_fetch_page_parses_dom_cards(parser):
    patcher, _, tab = make_browser_mock(LISTING_HTML, cards=CARDS)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert len(ads) == 2 and all(isinstance(a, Ad) for a in ads)
    first = ads[0]
    assert first.url == "https://auto.ru/cars/used/sale/bmw/x5/1-aa/"
    assert (first.price, first.year, first.mileage) == (7_700_000, 2019, 81_000)
    assert (first.manufacturer, first.model) == ("BMW", "X5")
    assert (first.transmission, first.fuel, first.engine_volume) == ("АКПП", "Дизель", 3.0)
    assert first.raw_json["text"] == CARD_1
    assert (ads[1].price, ads[1].year, ads[1].mileage) == (6_450_000, 2018, 120_500)
    assert (ads[1].transmission, ads[1].fuel) == ("АКПП", "Бензин")
    tab.locator.assert_called_once_with("div.ListingCars__items > div")
    assert tab.goto.call_args.kwargs["wait_until"] == "commit"
    assert tab.goto.call_args.kwargs["timeout"] == 30_000


def test_wait_for_listings_selector(parser):
    patcher, _, tab = make_browser_mock(LISTING_HTML, cards=CARDS)
    with patcher:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    tab.wait_for_selector.assert_called_once_with(
        "div.ListingCars__items > div", state="attached", timeout=10_000
    )


def test_parse_listings_empty(parser):
    """Карточек нет -> [] без ошибки (с предупреждением и дампом)."""
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=[])
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_parse_listings_dedups_and_skips_non_listing_cards(parser):
    cards = [
        CARDS[0],
        CARDS[0],  # дубль по url
        card(None, "Реклама банка 7 700 000 ₽"),  # нет ссылки
        card("https://auto.ru/banner/", "Скидки и акции"),  # нет цены/года/пробега
    ]
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=cards)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert [a.url for a in ads] == [CARDS[0]["href"]]


def test_card_fields_missing_are_none_and_relative_href(parser):
    ad = parser._card_to_ad("/cars/used/sale/bmw/x5/3-cc/", "BMW X5\n5 100 000 ₽", SPEC)
    assert ad.url == "https://auto.ru/cars/used/sale/bmw/x5/3-cc/"
    assert ad.price == 5_100_000
    assert ad.year is None and ad.mileage is None and ad.engine_volume is None
    assert ad.transmission is None and ad.fuel is None


@pytest.mark.parametrize("text, expected", [
    ("7 700 000 ₽", 7_700_000),
    ("от 7\u00a0700\u00a0000\u00a0₽", 7_700_000),
    ("2019\n7 700 000 ₽", 7_700_000),  # год перед ценой не «слипается»
    ("2 450 000₽", 2_450_000),
    ("без цены", None),
])
def test_regex_price(text, expected):
    assert AutoRuParser._extract_price(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("81 000 км", 81_000),
    ("2019 · 81 000 км", 81_000),
    ("120\u00a0500 км", 120_500),
    ("2018\n120 500 км", 120_500),  # год на предыдущей строке не «слипается»
    ("2018 120 500 км", 120_500),  # группы тысяч не начинаются посреди числа
    ("500 км", 500),
    ("нет данных", None),
])
def test_regex_mileage(text, expected):
    assert AutoRuParser._extract_mileage(text) == expected


def test_regex_year_and_volume_and_labels():
    assert AutoRuParser._extract_year("BMW X5, 2019, 81 000 км") == 2019
    assert AutoRuParser._extract_year("Toyota Camry 1998, 250 000 км") == 1998
    assert AutoRuParser._extract_year("7 700 000 ₽") is None
    assert AutoRuParser._extract_volume("2.0 л / 249 л.с.") == 2.0
    assert AutoRuParser._extract_volume("3,5 л") == 3.5
    assert AutoRuParser._extract_volume("249 л.с.") is None
    find = AutoRuParser._find_label
    assert find("Механика", autoru_module.TRANSMISSION_DOM_LABELS) == "МКПП"
    assert find("Вариатор", autoru_module.TRANSMISSION_DOM_LABELS) == "Вариатор"
    assert find("Электро", autoru_module.FUEL_DOM_LABELS) == "Электро"


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


def test_fetch_page_captcha_via_commit_url(parser):
    """FX-06: goto вернулся, но page.url уже на капче — селектор не ждём."""
    patcher, _, tab = make_browser_mock(
        page_url="https://auto.ru/showcaptcha?cc=1&retpath=x"
    )
    with patcher, pytest.raises(CaptchaDetectedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert "showcaptcha" in exc.value.url
    tab.wait_for_selector.assert_not_called()


def test_fetch_page_smartcaptcha_in_url(parser):
    patcher, _, _ = make_browser_mock(page_url="https://smartcaptcha.yandexcloud.net/x")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_timeout_then_captcha_url(parser):
    """FX-02: таймаут навигации, но URL уже капча -> CAPTCHA, не Blocked."""
    patcher, _, tab = make_browser_mock(page_url="https://auto.ru/showcaptcha?cc=1")
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_timeout_no_captcha(parser):
    """FX-03: таймаут и обычный URL -> BlockedError(goto_timeout)."""
    patcher, _, tab = make_browser_mock(
        page_url="https://auto.ru/cars/bmw/x5/all/?year_from=2018&page=1"
    )
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "goto_timeout" and exc.value.site == "auto.ru"


def test_fetch_page_selector_timeout_blocked(parser):
    """FX-04: выдачи нет, капчи и «ничего не найдено» нет -> no_listings_selector."""
    patcher, _, tab = make_browser_mock("<html><body>заглушка</body></html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "no_listings_selector"
    kwargs = tab.wait_for_selector.call_args.kwargs
    assert kwargs["timeout"] == 10_000 and kwargs["state"] == "attached"
    assert tab.wait_for_selector.call_args.args[0] == "div.ListingCars__items > div"


def test_fetch_page_selector_timeout_but_captcha_html(parser):
    patcher, _, tab = make_browser_mock('<html><div id="smartcaptcha"></div></html>')
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_selector_timeout_empty_results(parser):
    patcher, _, tab = make_browser_mock("<html>Объявления не найдены</html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_fetch_page_blocked_status_with_captcha_html_is_captcha(parser):
    """403 со страницей SmartCaptcha — это CAPTCHA (её можно пройти вручную)."""
    patcher, _, _ = make_browser_mock("<html>smartcaptcha</html>", status=403)
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_non_timeout_goto_error_propagates(parser):
    patcher, _, tab = make_browser_mock()
    tab.goto.side_effect = RuntimeError("net::ERR_CERT_AUTHORITY_INVALID")
    with patcher, pytest.raises(RuntimeError, match="ERR_CERT"):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_empty_results(parser):
    # На пустой выдаче сайт может показывать рекомендации — они не результат.
    html = "<html><h1>Ничего не найдено</h1>" + LISTING_HTML + "</html>"
    patcher, _, _ = make_browser_mock(html, cards=CARDS)
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_fetch_page_zero_ads_dumps_html_and_warns(parser, debug_dir, caplog):
    """Парсинг дал 0 объявлений -> предупреждение и дамп HTML для анализа."""
    html = "<html><body><script>window.x = {}</script>пусто</body></html>"
    patcher, _, _ = make_browser_mock(html)
    with patcher, caplog.at_level("INFO", logger="aprice.server.parsers.autoru"):
        assert parser.fetch_page(SPEC, {}, 2, threading.Event()) == []
    dumps = list(debug_dir.glob("autoru_page_2_*.html"))
    assert len(dumps) == 1 and dumps[0].read_text(encoding="utf-8") == html
    text = caplog.text
    assert "0 объявлений" in text and "html_len=%d" not in text
    assert f"html_len={len(html)}" in text and "финальный URL" in text


def test_fetch_page_with_ads_writes_no_dump(parser, debug_dir):
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=CARDS[:1])
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 1
    assert not debug_dir.exists()


def test_dump_failure_does_not_break_fetch(parser, debug_dir):
    debug_dir.parent.joinpath("debug").write_text("файл вместо каталога")
    patcher, _, _ = make_browser_mock("<html>пусто</html>")
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
    patcher, _, tab = make_browser_mock(LISTING_HTML, cards=CARDS)
    tab.goto.side_effect = lambda *a, **k: (stop.set(), MagicMock(status=200, url=tab.url))[1]
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []


def test_parser_reads_browser_settings_from_config(tmp_path):
    config = Config(tmp_path / "s.json")
    config.set("server", "playwright_headless", False)
    config.set("server", "ignore_https_errors", False)
    parser = AutoRuParser(config)
    patcher, cls, _ = make_browser_mock(LISTING_HTML, cards=CARDS)
    with patcher:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    cls.assert_called_once_with(headless=False, ignore_https_errors=False)


def test_test_fast_mode_and_simulation_do_not_touch_browser(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        ads = parser.fetch_page(SPEC, {"test_fast_mode": True, "use_stub_data": True}, 1, threading.Event())
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


# ----------------------------------------------------------------------
# 3.11: пост-фильтр по годам (страховка, если фильтр URL не сработал)
# ----------------------------------------------------------------------
def _year_card(year: int | None, n: int = 1) -> dict:
    body = f"BMW X5\n7 700 000 ₽\n3.0 л / Дизель\n{year}\n81 000 км" if year else (
        "BMW X5\n7 700 000 ₽\n81 000 км")
    return card(f"https://auto.ru/cars/used/sale/bmw/x5/{n}-yy/", body)


def test_year_outside_range_is_dropped(parser):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = [
        _year_card(2026, 1), _year_card(2019, 2), _year_card(2017, 3),
    ]
    ads = parser._parse_listings_from_page(tab, SPEC)  # 2018–2021
    assert [a.year for a in ads] == [2019]


def test_card_without_year_passes_year_filter(parser):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = [_year_card(None, 1)]
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert len(ads) == 1 and ads[0].year is None


@pytest.mark.parametrize("year, kept", [(2017, False), (2018, True), (2021, True), (2022, False)])
def test_year_filter_boundaries_are_inclusive(parser, year, kept):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = [_year_card(year)]
    assert bool(parser._parse_listings_from_page(tab, SPEC)) is kept


def test_year_filter_without_bounds_keeps_everything(parser):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = [_year_card(2026)]
    spec = {k: v for k, v in SPEC.items() if not k.startswith("year_")}
    assert len(parser._parse_listings_from_page(tab, spec)) == 1


def test_all_cards_out_of_range_warns(parser, caplog):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = [_year_card(2026, 1), _year_card(2025, 2)]
    with caplog.at_level("WARNING", logger="aprice.server.parsers"):
        assert parser._parse_listings_from_page(tab, SPEC) == []
    assert "игнорирует фильтр по годам" in caplog.text
