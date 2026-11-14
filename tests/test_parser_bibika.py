"""Тесты реального парсера bibika.ru (подзадача 3.10).

Сеть не используется: Playwright подменяется ``MagicMock``, HTML и карточки
сочинены вручную. Часть тестов гоняет JS-сценарий в настоящем Chromium на
``set_content`` (офлайн; нет Chromium — пропуск). Qt не требуется.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from common.config import Config
from server.parsers import bibika as bibika_module
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    ParserError,
)
from server.parsers.bibika import BibikaParser

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
}
CARD_1 = "BMW X5, 2020\n7 690 000 ₽\n3.0 л, дизель, автомат, 103 583 км\nМосква"
CARD_2 = "BMW X5, 2018\n3 850 000 руб.\n3.0 л, бензин, механика, 95 000 км"
CARDS = [
    {"href": "/cars/bmw-x5-2020-1234567", "price_text": "7 690 000 ₽", "text": CARD_1},
    {"href": "https://bibika.ru/avto/bmw/x5/2234567", "price_text": None, "text": CARD_2},
]
LISTING_HTML = '<html><div data-id="1">…</div></html>'
LISTING_URL = "https://bibika.ru/bmw/x5/?year_from=2018"


def twenty_cards(n: int = 20) -> list[dict]:
    return [
        {
            "href": f"/cars/bmw-x5-{1000000 + i}",
            "price_text": f"{3_000_000 + i * 10_000:,} ₽".replace(",", " "),
            "text": f"BMW X5, {2018 + i % 4}\n{3_000_000 + i * 10_000:,} ₽".replace(",", " ")
            + f"\n3.0 л, дизель, автомат, {50_000 + i} км",
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def debug_dir(tmp_path, monkeypatch):
    """Дампы HTML — во временный каталог, не в data/debug."""
    path = tmp_path / "debug"
    monkeypatch.setattr(bibika_module, "DEBUG_DIR", path)
    return path


@pytest.fixture()
def parser(tmp_path) -> BibikaParser:
    return BibikaParser(Config(tmp_path / "settings.json"))


def make_browser_mock(
    html: str = LISTING_HTML,
    *,
    page_url: str = LISTING_URL,
    status: int = 200,
    response_none: bool = False,
    cards=None,
):
    """Подменяет ``PlaywrightBrowser`` в bibika; возвращает (patcher, класс, страница)."""
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
    return patch.object(bibika_module, "PlaywrightBrowser", cls), cls, tab


def locator_for(cards):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = cards
    return tab


# ----------------------------------------------------------------------
# URL
# ----------------------------------------------------------------------
def test_build_url_first_page_has_years_but_no_page(parser):
    parts = urlparse(parser._build_url(SPEC, 1))
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "bibika.ru", "/bmw/x5/")
    assert parse_qs(parts.query) == {"year_from": ["2018"], "year_to": ["2021"]}


def test_build_url_second_page_adds_page(parser):
    assert parse_qs(urlparse(parser._build_url(SPEC, 2)).query)["page"] == ["2"]


def test_build_url_without_years_and_first_page_has_no_query(parser):
    spec = {k: v for k, v in SPEC.items() if k not in ("year_from", "year_to")}
    assert parser._build_url(spec, 1) == "https://bibika.ru/bmw/x5/"
    assert urlparse(parser._build_url(spec, 3)).query == "page=3"


def test_build_url_slug_spaces_and_override(parser):
    url = parser._build_url(dict(SPEC, manufacturer="Land Rover", model="Range Rover Sport"), 1)
    assert urlparse(url).path == "/land_rover/range_rover_sport/"
    url = parser._build_url(dict(SPEC, manufacturer_slug="bmw_", model_slug="x5_m"), 1)
    assert urlparse(url).path == "/bmw_/x5_m/"


def test_build_url_ignores_region_fields(parser):
    url = parser._build_url(dict(SPEC, region="Москва", region_slug="moskva", region_id=77), 1)
    assert urlparse(url).path == "/bmw/x5/"


# ----------------------------------------------------------------------
# fetch_page
# ----------------------------------------------------------------------
def test_fetch_page_success_two_cards(parser):
    patcher, _, tab = make_browser_mock(cards=CARDS)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert len(ads) == 2 and all(isinstance(a, Ad) for a in ads)
    first, second = ads
    assert first.url == "https://bibika.ru/cars/bmw-x5-2020-1234567"
    assert (first.price, first.year, first.mileage) == (7_690_000, 2020, 103_583)
    assert (first.manufacturer, first.model) == ("BMW", "X5")
    assert (first.transmission, first.fuel, first.engine_volume) == ("АКПП", "Дизель", 3.0)
    assert first.raw_json == {"source": "bibika.ru", "text": CARD_1}
    assert (second.price, second.year, second.mileage) == (3_850_000, 2018, 95_000)
    assert (second.transmission, second.fuel) == ("МКПП", "Бензин")
    kwargs = tab.goto.call_args.kwargs
    assert kwargs["wait_until"] == "commit" and kwargs["timeout"] == 30_000
    tab.wait_for_selector.assert_called_once_with(
        'div[data-id], div.bb-item', state="attached", timeout=10_000,
    )


@pytest.mark.parametrize("url", [
    "https://bibika.ru/captcha?from=x",
    "https://bibika.ru/showcaptcha",
])
def test_fetch_page_captcha_by_url(parser, url):
    patcher, _, tab = make_browser_mock(page_url=url)
    with patcher, pytest.raises(CaptchaDetectedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "bibika.ru" and "captcha" in exc.value.url
    tab.wait_for_selector.assert_not_called()


@pytest.mark.parametrize("url", ["https://bibika.ru/blocked", "https://bibika.ru/firewall?id=1"])
def test_fetch_page_blocked_by_url(parser, url):
    patcher, _, tab = make_browser_mock(page_url=url)
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "bibika.ru"
    tab.wait_for_selector.assert_not_called()


def test_fetch_page_timeout_then_captcha_url(parser):
    patcher, _, tab = make_browser_mock(page_url="https://bibika.ru/captcha?r=1")
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_timeout_no_captcha(parser):
    patcher, _, tab = make_browser_mock()
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "goto_timeout"


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_page_blocked_status(parser, status):
    patcher, _, _ = make_browser_mock("<html>пусто</html>", status=status)
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == f"http_{status}"


def test_http_403_with_access_restricted_html_is_blocked(parser):
    patcher, _, _ = make_browser_mock("<html>Доступ ограничен</html>", status=403)
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "access_restricted"


def test_http_403_with_robot_phrase_is_captcha(parser):
    patcher, _, _ = make_browser_mock("<h1>Подтвердите, что вы не робот</h1>", status=403)
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_access_restricted_html_after_selector_timeout_is_blocked(parser, debug_dir):
    patcher, _, tab = make_browser_mock("<html><h1>Доступ ограничен</h1></html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "access_restricted"
    assert not debug_dir.exists()


def test_robot_phrase_after_selector_timeout_is_captcha(parser):
    patcher, _, tab = make_browser_mock("<h1>Подтвердите, что вы не робот</h1>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_empty_results(parser):
    patcher, _, tab = make_browser_mock("<html><h1>Ничего не найдено</h1></html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_fetch_page_no_selector_is_blocked_and_html_is_dumped(parser, debug_dir):
    html = "<html><body>другая вёрстка</body></html>"
    patcher, _, tab = make_browser_mock(html)
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 2, threading.Event())
    assert exc.value.detail == "no_listings_selector"
    dumps = list(debug_dir.glob("bibika_page_2_*.html"))
    assert len(dumps) == 1 and dumps[0].read_text(encoding="utf-8") == html


def test_captcha_word_in_normal_page_html_is_not_captcha(parser):
    html = LISTING_HTML + '<script src="https://www.google.com/recaptcha/api.js"></script>'
    patcher, _, _ = make_browser_mock(html, cards=CARDS)
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 2


def test_fetch_page_no_response_raises_parser_error(parser):
    patcher, _, _ = make_browser_mock(response_none=True)
    with patcher, pytest.raises(ParserError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_non_timeout_goto_error_propagates(parser):
    patcher, _, tab = make_browser_mock()
    tab.goto.side_effect = RuntimeError("net::ERR_CERT_AUTHORITY_INVALID")
    with patcher, pytest.raises(RuntimeError, match="ERR_CERT"):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_stop_before_start_makes_no_browser(parser):
    stop = threading.Event()
    stop.set()
    patcher, cls, _ = make_browser_mock()
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []
    cls.assert_not_called()


def test_stop_after_goto_returns_empty_without_parsing(parser):
    stop = threading.Event()
    patcher, _, tab = make_browser_mock(cards=CARDS)
    tab.goto.side_effect = lambda *a, **k: (stop.set(), MagicMock(status=200, url=tab.url))[1]
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []
    tab.locator.assert_not_called()


def test_stop_after_selector_wait_returns_empty_without_parsing(parser):
    stop = threading.Event()
    patcher, _, tab = make_browser_mock(cards=CARDS)
    tab.wait_for_selector.side_effect = lambda *a, **k: stop.set()
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, stop) == []
    tab.locator.assert_not_called()


def test_parser_reads_browser_settings_from_config(tmp_path):
    config = Config(tmp_path / "s.json")
    config.set("server", "playwright_headless", False)
    config.set("server", "ignore_https_errors", False)
    patcher, cls, _ = make_browser_mock(cards=CARDS)
    with patcher:
        BibikaParser(config).fetch_page(SPEC, {}, 1, threading.Event())
    cls.assert_called_once_with(headless=False, ignore_https_errors=False)


# ----------------------------------------------------------------------
# use_stub_data и test_fast_mode — два независимых флага
# ----------------------------------------------------------------------
def test_use_stub_data_returns_stub_without_browser(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        ads = parser.fetch_page(SPEC, {"use_stub_data": True}, 1, threading.Event())
    assert ads and all(a.raw_json["source"] == "stub" for a in ads)
    cls.assert_not_called()


def test_test_fast_mode_alone_hits_real_parsing(parser):
    patcher, cls, tab = make_browser_mock(cards=CARDS)
    with patcher:
        ads = parser.fetch_page(SPEC, {"test_fast_mode": True}, 1, threading.Event())
    assert [a.raw_json["source"] for a in ads] == ["bibika.ru", "bibika.ru"]
    cls.assert_called_once()
    tab.goto.assert_called_once()


def test_test_fast_mode_only_shortens_delays(parser):
    assert parser._page_delays({"test_fast_mode": True}) == BaseSiteParser.FAST_PAGE_DELAY_SEC
    assert parser._page_delays({}) == BaseSiteParser.PAGE_DELAY_SEC
    assert BaseSiteParser._use_stub({"test_fast_mode": True}) is False


def test_simulation_flags_work_without_browser(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        with pytest.raises(CaptchaDetectedError):
            parser.fetch_page(SPEC, {"simulate_captcha": "bibika.ru"}, 1, threading.Event())
        with pytest.raises(BlockedError):
            parser.fetch_page(SPEC, {"simulate_block": "bibika.ru"}, 1, threading.Event())
    cls.assert_not_called()


# ----------------------------------------------------------------------
# Ожидание полной загрузки
# ----------------------------------------------------------------------
def test_waits_for_ten_cards_after_first_selector(parser):
    patcher, _, tab = make_browser_mock(cards=twenty_cards())
    with patcher:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    js = "document.querySelectorAll('div[data-id], div.bb-item').length >= 10"
    tab.wait_for_function.assert_called_once_with(js, timeout=5_000)
    tab.wait_for_timeout.assert_called_once_with(1_500)
    names = [c[0] for c in tab.method_calls
             if c[0] in ("wait_for_selector", "wait_for_function", "wait_for_timeout", "content")]
    assert names.index("wait_for_selector") < names.index("wait_for_function") \
        < names.index("wait_for_timeout") < names.index("content")


def test_wait_for_function_timeout_is_not_an_error(parser):
    patcher, _, tab = make_browser_mock(cards=twenty_cards(8))
    tab.wait_for_function.side_effect = PlaywrightTimeoutError("t")
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 8
    tab.wait_for_timeout.assert_called_once_with(1_500)


def test_no_full_list_wait_when_listings_selector_times_out(parser):
    patcher, _, tab = make_browser_mock("<html>пусто</html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("t")
    with patcher, pytest.raises(BlockedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    tab.wait_for_function.assert_not_called()


# ----------------------------------------------------------------------
# Диагностика
# ----------------------------------------------------------------------
def test_zero_ads_dumps_html_and_warns(parser, debug_dir, caplog):
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=[])
    with patcher, caplog.at_level("INFO", logger="aprice.server.parsers.bibika"):
        assert parser.fetch_page(SPEC, {}, 3, threading.Event()) == []
    dumps = list(debug_dir.glob("bibika_page_3_*.html"))
    assert len(dumps) == 1 and dumps[0].read_text(encoding="utf-8") == LISTING_HTML
    assert "0 объявлений" in caplog.text


def test_fewer_than_five_ads_saves_incomplete_html(parser, debug_dir):
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=twenty_cards(4))
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 7, threading.Event())) == 4
    assert (debug_dir / "bibika_page_7_incomplete.html").read_text(encoding="utf-8") == LISTING_HTML


def test_exactly_five_ads_is_not_incomplete(parser, debug_dir):
    patcher, _, _ = make_browser_mock(cards=twenty_cards(5))
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 5
    assert not debug_dir.exists()


def test_dump_failure_does_not_break_fetch(parser, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(bibika_module, "DEBUG_DIR", blocker / "sub")  # mkdir -> OSError
    patcher, _, _ = make_browser_mock(cards=[])
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


# ----------------------------------------------------------------------
# Разбор карточек
# ----------------------------------------------------------------------
def test_parse_uses_only_card_selector(parser):
    tab = locator_for(CARDS)
    parser._parse_listings_from_page(tab, SPEC)
    assert [c.args[0] for c in tab.locator.call_args_list] == ['div[data-id], div.bb-item']


def test_ad_cards_without_url_are_dropped(parser):
    cards = CARDS + [
        {"href": None, "price_text": "100 000 ₽", "text": "Реклама\n100 000 ₽"},
        {"href": "", "price_text": None, "text": "Баннер"},
    ]
    assert len(parser._parse_listings_from_page(locator_for(cards), SPEC)) == 2


@pytest.mark.parametrize("href", [
    "https://bibika.ru/bmw/x5/",                         # рубрика
    "https://bibika.ru/cars/bmw/",                       # раздел без номера
    "https://bibika.ru/about",                           # навигация
    "https://evil.example/cars/bmw-x5-1234567",          # чужой хост
    "https://bibika.ru.evil.example/cars/bmw-x5-1234567",  # подмена хоста
    "https://notbibika.ru/cars/bmw-x5-1234567",          # похожее имя
    "javascript:void(0)",
])
def test_non_listing_urls_are_dropped(parser, href):
    card = {"href": href, "price_text": "5 000 000 ₽", "text": "BMW X5, 2019\n5 000 000 ₽"}
    assert parser._parse_listings_from_page(locator_for([card]), SPEC) == []


def test_relative_and_absolute_urls_are_accepted(parser):
    ads = parser._parse_listings_from_page(locator_for(CARDS), SPEC)
    assert [a.url.startswith("https://bibika.ru/") for a in ads] == [True, True]


def test_duplicates_are_merged_by_url(parser):
    cards = CARDS + [dict(CARDS[0])]
    assert len(parser._parse_listings_from_page(locator_for(cards), SPEC)) == 2


def test_price_element_wins_and_text_is_fallback(parser):
    card = dict(CARDS[0], price_text="1 234 567 ₽")
    assert parser._parse_listings_from_page(locator_for([card]), SPEC)[0].price == 1_234_567
    for bad in (None, "", "договорная"):
        card = dict(CARDS[0], price_text=bad)
        assert parser._parse_listings_from_page(locator_for([card]), SPEC)[0].price == 7_690_000


def test_card_without_price_year_mileage_is_dropped(parser):
    card = {"href": "/cars/b-1234567", "price_text": None, "text": "Нет данных"}
    assert parser._parse_listings_from_page(locator_for([card]), SPEC) == []


def test_card_with_only_price_is_kept(parser):
    card = {"href": "/cars/b-1234567", "price_text": "5 100 000 ₽", "text": "Лот"}
    ad = parser._parse_listings_from_page(locator_for([card]), SPEC)[0]
    assert ad.price == 5_100_000 and ad.year is None and ad.mileage is None


def test_every_ad_has_nonempty_text(parser):
    ads = parser._parse_listings_from_page(locator_for(twenty_cards()), SPEC)
    assert len(ads) == 20 and all(a.raw_json["text"].strip() for a in ads)


# ----------------------------------------------------------------------
# Пост-фильтр по годам
# ----------------------------------------------------------------------
def card_2026():
    return {"href": "/cars/bmw-x5-2026-9234567", "price_text": "20 900 000 ₽",
            "text": "BMW X5, 2026\n20 900 000 ₽\n3.0 л, бензин, автомат"}


def test_year_outside_range_is_dropped(parser):
    ads = parser._parse_listings_from_page(locator_for(CARDS + [card_2026()]), SPEC)
    assert [a.year for a in ads] == [2020, 2018]  # 2026 при year_to=2021 отброшен


@pytest.mark.parametrize("year, kept", [(2017, False), (2018, True), (2021, True), (2022, False)])
def test_year_range_boundaries_are_inclusive(parser, year, kept):
    card = dict(CARDS[0], text=f"BMW X5, {year}\n7 690 000 ₽")
    assert bool(parser._parse_listings_from_page(locator_for([card]), SPEC)) is kept


def test_card_without_year_passes_year_filter(parser):
    card = dict(CARDS[0], text="BMW X5\n7 690 000 ₽\n103 583 км")
    ads = parser._parse_listings_from_page(locator_for([card]), SPEC)
    assert len(ads) == 1 and ads[0].year is None


@pytest.mark.parametrize("spec_years", [
    {}, {"year_from": None, "year_to": None}, {"year_from": 0, "year_to": 0},
    {"year_from": "abc", "year_to": ""},
])
def test_missing_or_bad_bounds_do_not_filter(parser, spec_years):
    spec = {**{k: v for k, v in SPEC.items() if not k.startswith("year_")}, **spec_years}
    assert len(parser._parse_listings_from_page(locator_for(CARDS + [card_2026()]), spec)) == 3


def test_only_one_bound_filters_that_side(parser):
    spec = dict(SPEC, year_from=2019, year_to=None)
    years = [a.year for a in parser._parse_listings_from_page(
        locator_for(CARDS + [card_2026()]), spec)]
    assert years == [2020, 2026]


def test_all_out_of_range_warns_and_dumps_html(parser, debug_dir, caplog):
    cards = [dict(card_2026(), href=f"/cars/bmw-x5-{9000000 + i}") for i in range(6)]
    patcher, _, _ = make_browser_mock(cards=cards)
    with patcher, caplog.at_level("WARNING", logger="aprice.server.parsers.bibika"):
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []
    assert "игнорирует фильтр" in caplog.text
    assert list(debug_dir.glob("bibika_page_1_*.html"))


def test_year_comes_from_text_not_from_url(parser):
    card = {"href": "/cars/bmw-x5-2026-1234567", "price_text": "7 690 000 ₽",
            "text": "BMW X5, 2020\n7 690 000 ₽"}
    assert parser._parse_listings_from_page(locator_for([card]), SPEC)[0].year == 2020


def test_year_in_url_only_is_not_used(parser):
    card = {"href": "/cars/2019/bmw-x5-1234567", "price_text": "7 690 000 ₽",
            "text": "BMW X5\n7 690 000 ₽"}
    assert parser._parse_listings_from_page(locator_for([card]), SPEC)[0].year is None


# ----------------------------------------------------------------------
# Регулярки
# ----------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("7 690 000 ₽", 7_690_000),
    ("7 690 000 ₽", 7_690_000),   # неразрывный пробел
    ("3 850 000 руб.", 3_850_000),
    ("2020\n7 690 000 ₽", 7_690_000),
    ("без цены", None),
])
def test_regex_price(text, expected):
    assert BibikaParser._extract_price(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("103 583 км", 103_583),
    ("95 000 км", 95_000),
    ("2018\n120 500 км", 120_500),
    ("500 км", 500),
    ("Без пробега", None),
])
def test_regex_mileage(text, expected):
    assert BibikaParser._extract_mileage(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("BMW X5, 2020", 2020), ("1999 год", 1999), ("7 690 000 ₽", None), ("номер 12020", None),
])
def test_regex_year(text, expected):
    assert BibikaParser._extract_year(text) == expected


def test_regex_volume_and_labels():
    assert BibikaParser._extract_volume("3.0 л (249 л.с.)") == 3.0
    assert BibikaParser._extract_volume("2,5 л") == 2.5
    assert BibikaParser._extract_volume("249 л.с.") is None
    find = BibikaParser._find_label
    assert find("Механика", bibika_module.TRANSMISSION_LABELS) == "МКПП"
    assert find("Вариатор", bibika_module.TRANSMISSION_LABELS) == "Вариатор"
    assert find("гибрид", bibika_module.FUEL_LABELS) == "Гибрид"
    assert find("ничего", bibika_module.FUEL_LABELS) is None


# ----------------------------------------------------------------------
# collect / close
# ----------------------------------------------------------------------
def test_collect_real_mode_stops_on_empty_page_and_respects_limits(parser):
    pages = iter([
        [Ad(url=f"https://bibika.ru/{i}") for i in range(3)],
        [Ad(url=f"https://bibika.ru/{i}") for i in range(10, 13)],
        [],
    ])
    seen: list[int] = []
    with patch.object(parser, "fetch_page", side_effect=lambda *a: next(pages)), \
         patch.object(bibika_module, "interruptible_sleep", return_value=True):
        ads = parser.collect(
            SPEC, {"max_pages_per_site": 5, "max_ads_per_site": 4},
            threading.Event(), progress_cb=lambda p, n: seen.append(n),
        )
    assert len(ads) == 4 and seen == [3, 4]


def test_collect_with_use_stub_data_uses_stub(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        ads = parser.collect(
            SPEC, {"use_stub_data": True, "test_fast_mode": True, "max_pages_per_site": 1},
            threading.Event(),
        )
    assert ads and ads[0].raw_json["source"] == "stub"
    cls.assert_not_called()


def test_close_is_noop(parser):
    assert parser.close() is None


# ----------------------------------------------------------------------
# JS-сценарий в настоящем Chromium (офлайн: set_content, без сети)
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def chromium_page():
    from playwright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - браузер может быть не установлен
        pytest.skip(f"Chromium недоступен: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    pw.stop()


def _card_html(i: int, *, link: bool = True) -> str:
    anchor = (
        f'<a href="/cars/bmw-x5-{1000000 + i}">BMW X5, 2019</a>' if link else "<span>Реклама</span>"
    )
    return (
        f'<div data-id="{i}">{anchor}<div class="item-price">{3_000_000 + i:,} ₽</div>'
        f"<div>3.0 л, дизель, {50_000 + i} км</div></div>"
    )


def test_cards_script_in_real_browser(chromium_page):
    html = "".join(_card_html(i) for i in range(20)) + _card_html(99, link=False)
    chromium_page.set_content(html)
    cards = chromium_page.locator(bibika_module.CARD_SELECTOR).evaluate_all(
        bibika_module.CARDS_SCRIPT
    )
    assert len(cards) == 21
    assert cards[0]["href"].endswith("/cars/bmw-x5-1000000")
    assert cards[0]["price_text"] == "3,000,000 ₽" and "км" in cards[0]["text"]
    assert cards[-1]["href"] is None
    ads = BibikaParser._parse_listings_from_page(
        BibikaParser.__new__(BibikaParser), chromium_page, dict(SPEC, year_from=2018, year_to=2021)
    )
    # about:blank: относительный href не разрешается, _absolute достраивает его
    assert len(ads) == 20 and all(a.raw_json["text"].strip() for a in ads)


def test_bb_item_class_is_also_a_card(chromium_page):
    chromium_page.set_content(
        '<div class="bb-item"><a href="/avto/bmw/x5/7654321">BMW X5, 2019</a>'
        '<span class="price">4 000 000 ₽</span></div>'
    )
    ads = BibikaParser._parse_listings_from_page(
        BibikaParser.__new__(BibikaParser), chromium_page, SPEC
    )
    assert [a.price for a in ads] == [4_000_000]


DELAYED_HTML = """
<div id="list">""" + _card_html(0) + """</div>
<script>
setTimeout(() => {
  for (let i = 1; i < 20; i++) {
    const d = document.createElement('div');
    d.setAttribute('data-id', String(i));
    d.innerHTML = '<a href="/cars/bmw-x5-' + (1000000 + i) + '">BMW X5, 2019</a>' +
                  '<div class="price">3 000 000 ₽</div>';
    document.getElementById('list').appendChild(d);
  }
}, 700);
</script>
"""


def test_delayed_render_collects_all_twenty_in_real_browser(chromium_page, parser):
    chromium_page.set_content(DELAYED_HTML)
    assert chromium_page.locator(bibika_module.CARD_SELECTOR).count() == 1
    parser._wait_full_list(chromium_page)
    assert len(parser._parse_listings_from_page(chromium_page, SPEC)) == 20


def test_without_wait_delayed_page_gives_one_card_in_real_browser(chromium_page, parser):
    chromium_page.set_content(DELAYED_HTML)
    assert len(parser._parse_listings_from_page(chromium_page, SPEC)) == 1
    chromium_page.wait_for_timeout(1_000)  # дорисовка закончена, таймер не утекает
