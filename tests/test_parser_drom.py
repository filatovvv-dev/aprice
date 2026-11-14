"""Тесты реального парсера drom.ru (подзадача 3.8).

Сеть не используется: Playwright подменяется ``MagicMock``, HTML и карточки
сочинены вручную. Тесты не требуют Qt и установленного Chromium.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from common.config import Config
from server.parsers import drom as drom_module
from server.parsers.base import (
    Ad,
    BlockedError,
    CaptchaDetectedError,
    ParserError,
)
from server.parsers.drom import DromParser

SPEC = {
    "mode": "range",
    "manufacturer": "BMW",
    "model": "X5",
    "year_from": 2018,
    "year_to": 2021,
}
CARD_1 = (
    "BMW X5, 2019\n4 190 000 ₽\n3.0 л (249 л.с.), дизель, автомат, 4WD, 160 661 км\nМосква"
)
CARD_2 = "BMW X5, 2018\n3 850 000 руб.\n3.0 л, бензин, механика, 95 000 км"
def twenty_cards(n: int = 20, source: str = "card") -> list[dict]:
    """``n`` разных карточек в формате ``evaluate_all`` (``/N.html``)."""
    return [
        {
            "source": source,
            "href": f"https://moscow.drom.ru/bmw/x5/{1000000 + i}.html",
            "text": f"BMW X5, {2018 + i % 4}\n{3_000_000 + i * 10_000:,} ₽".replace(",", " ")
            + f"\n3.0 л, дизель, автомат, {50_000 + i} км",
        }
        for i in range(n)
    ]


CARDS = [
    {"href": "https://moscow.drom.ru/bmw/x5/111.html", "text": CARD_1},
    {"href": "https://spb.drom.ru/bmw/x5/222.html", "text": CARD_2},
]
LISTING_HTML = '<html><div data-ftid="bulls-list_bull">…</div></html>'
LISTING_URL = "https://auto.drom.ru/region77/bmw/x5/?minyear=2018"


@pytest.fixture(autouse=True)
def debug_dir(tmp_path, monkeypatch):
    """Дампы HTML пустых страниц — во временный каталог, не в data/debug."""
    path = tmp_path / "debug"
    monkeypatch.setattr(drom_module, "DEBUG_DIR", path)
    return path


@pytest.fixture()
def parser(tmp_path) -> DromParser:
    return DromParser(Config(tmp_path / "settings.json"))


def make_browser_mock(
    html: str = LISTING_HTML,
    *,
    page_url: str = LISTING_URL,
    status: int = 200,
    response_none: bool = False,
    cards=None,
):
    """Подменяет ``PlaywrightBrowser`` в drom; возвращает (patcher, класс, страница)."""
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
    return patch.object(drom_module, "PlaywrightBrowser", cls), cls, tab


# ----------------------------------------------------------------------
# URL
# ----------------------------------------------------------------------
def test_build_url_without_region_is_all_russia(parser):
    assert parser._build_url(SPEC, 1) == (
        "https://auto.drom.ru/bmw/x5/?minyear=2018&maxyear=2021"
    )


def test_build_url_with_region_id(parser):
    assert parser._build_url(dict(SPEC, region_id=77), 1) == (
        "https://auto.drom.ru/region77/bmw/x5/?minyear=2018&maxyear=2021"
    )


def test_build_url_with_region_slug(parser):
    assert parser._build_url(dict(SPEC, region_slug="moscow"), 1) == (
        "https://auto.drom.ru/moscow/bmw/x5/?minyear=2018&maxyear=2021"
    )


def test_build_url_region_id_wins_over_slug(parser):
    url = parser._build_url(dict(SPEC, region_id=78, region_slug="moscow"), 1)
    assert urlparse(url).path == "/region78/bmw/x5/"


def test_build_url_ignores_shared_region_field(parser):
    """``spec.region`` (slug для auto.ru) drom.ru не понимает (``/moskva/`` -> 404)."""
    assert urlparse(parser._build_url(dict(SPEC, region="moskva"), 1)).path == "/bmw/x5/"


def test_build_url_pagination_is_path_segment(parser):
    """Страница N>1 — ``/pageN/`` в пути, а не ``?page=N``."""
    assert parser._build_url(dict(SPEC, region_id=77), 2) == (
        "https://auto.drom.ru/region77/bmw/x5/page2/?minyear=2018&maxyear=2021"
    )
    url = parser._build_url(SPEC, 3)
    assert urlparse(url).path == "/bmw/x5/page3/"
    assert parse_qs(urlparse(url).query) == {"minyear": ["2018"], "maxyear": ["2021"]}
    assert "page" not in parser._build_url(SPEC, 1)


def test_build_url_slug_override(parser):
    spec = dict(SPEC, manufacturer_slug="land_rover", model_slug="range-rover")
    assert urlparse(parser._build_url(spec, 1)).path == "/land_rover/range-rover/"


def test_build_url_has_no_unverified_filters(parser):
    spec = dict(SPEC, transmissions=["АКПП"], fuels=["Бензин"], engine_volume_from=2.0)
    assert set(parse_qs(urlparse(parser._build_url(spec, 1)).query)) == {"minyear", "maxyear"}


def test_build_url_without_years_has_no_query(parser):
    spec = {"manufacturer": "BMW", "model": "X5"}
    assert parser._build_url(spec, 1) == "https://auto.drom.ru/bmw/x5/"
    assert parser._build_url(spec, 2) == "https://auto.drom.ru/bmw/x5/page2/"


# ----------------------------------------------------------------------
# fetch_page
# ----------------------------------------------------------------------
def test_fetch_page_success(parser):
    patcher, _, tab = make_browser_mock(cards=CARDS)
    with patcher:
        ads = parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert len(ads) == 2 and all(isinstance(a, Ad) for a in ads)
    first, second = ads
    assert first.url == "https://moscow.drom.ru/bmw/x5/111.html"
    assert (first.price, first.year, first.mileage) == (4_190_000, 2019, 160_661)
    assert (first.manufacturer, first.model) == ("BMW", "X5")
    assert (first.transmission, first.fuel, first.engine_volume) == ("АКПП", "Дизель", 3.0)
    assert first.raw_json["text"] == CARD_1
    # цена с «руб.» и неразрывным пробелом в пробеге
    assert (second.price, second.year, second.mileage) == (3_850_000, 2018, 95_000)
    assert (second.transmission, second.fuel) == ("МКПП", "Бензин")
    kwargs = tab.goto.call_args.kwargs
    assert kwargs["wait_until"] == "commit" and kwargs["timeout"] == 30_000
    tab.wait_for_selector.assert_called_once_with(
        '[data-ftid="bulls-list_bull"], [data-ftid="bull_title"]',
        state="attached", timeout=10_000,
    )


def test_fetch_page_captcha_by_url(parser):
    patcher, _, tab = make_browser_mock(page_url="https://www.drom.ru/captcha/?r=1")
    with patcher, pytest.raises(CaptchaDetectedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.site == "drom.ru" and "captcha" in exc.value.url
    tab.wait_for_selector.assert_not_called()


def test_fetch_page_timeout_then_captcha_url(parser):
    patcher, _, tab = make_browser_mock(page_url="https://www.drom.ru/captcha/?r=1")
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_timeout_no_captcha(parser):
    patcher, _, tab = make_browser_mock()
    tab.goto.side_effect = PlaywrightTimeoutError("Timeout 30000ms exceeded")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == "goto_timeout" and exc.value.site == "drom.ru"


@pytest.mark.parametrize("status", [403, 429])
def test_fetch_page_blocked_status(parser, status):
    patcher, _, _ = make_browser_mock("<html>Доступ ограничен</html>", status=status)
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert exc.value.detail == f"http_{status}"


def test_fetch_page_blocked_status_with_captcha_html_is_captcha(parser):
    patcher, _, _ = make_browser_mock("<html>Введите captcha</html>", status=403)
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_fetch_page_empty_results(parser):
    patcher, _, tab = make_browser_mock("<html><h1>Ничего не найдено</h1></html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_fetch_page_no_selector_is_blocked_and_html_is_dumped(parser, debug_dir):
    """Таймаут ожидания выдачи: HTML сохраняется ДО исключения (для подбора селекторов)."""
    html = "<html><body>заглушка</body></html>"
    patcher, _, tab = make_browser_mock(html)
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(BlockedError) as exc:
        parser.fetch_page(SPEC, {}, 2, threading.Event())
    assert exc.value.detail == "no_listings_selector"
    dumps = list(debug_dir.glob("drom_page_2_*.html"))
    assert len(dumps) == 1 and dumps[0].read_text(encoding="utf-8") == html


def test_captcha_after_selector_timeout_is_not_dumped(parser, debug_dir):
    patcher, _, tab = make_browser_mock("<html>captcha</html>")
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())
    assert not debug_dir.exists()


def test_fetch_page_no_selector_but_captcha_html(parser):
    patcher, _, tab = make_browser_mock('<html><div class="CAPTCHA"></div></html>')
    tab.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout 10000ms")
    with patcher, pytest.raises(CaptchaDetectedError):
        parser.fetch_page(SPEC, {}, 1, threading.Event())


def test_captcha_word_in_normal_page_html_is_not_captcha(parser):
    """Выдача есть, а в скриптах страницы встречается «recaptcha» — это не CAPTCHA."""
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
    cls.return_value.__enter__.assert_not_called()


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
        DromParser(config).fetch_page(SPEC, {}, 1, threading.Event())
    cls.assert_called_once_with(headless=False, ignore_https_errors=False)


def test_test_fast_mode_and_simulation_do_not_touch_browser(parser):
    patcher, cls, _ = make_browser_mock()
    with patcher:
        ads = parser.fetch_page(SPEC, {"test_fast_mode": True}, 1, threading.Event())
        assert ads and ads[0].raw_json["source"] == "stub"
        with pytest.raises(CaptchaDetectedError):
            parser.fetch_page(SPEC, {"simulate_captcha": "drom.ru"}, 1, threading.Event())
        with pytest.raises(BlockedError):
            parser.fetch_page(SPEC, {"simulate_block": "drom.ru"}, 1, threading.Event())
    cls.assert_not_called()


# ----------------------------------------------------------------------
# Диагностика пустой выдачи
# ----------------------------------------------------------------------
def test_zero_ads_dumps_html_and_warns(parser, debug_dir, caplog):
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=[])
    with patcher, caplog.at_level("INFO", logger="aprice.server.parsers.drom"):
        assert parser.fetch_page(SPEC, {}, 3, threading.Event()) == []
    dumps = list(debug_dir.glob("drom_page_3_*.html"))
    assert len(dumps) == 1 and dumps[0].read_text(encoding="utf-8") == LISTING_HTML
    assert "0 объявлений" in caplog.text and f"html_len={len(LISTING_HTML)}" in caplog.text


def test_dump_failure_does_not_break_fetch(parser, debug_dir):
    debug_dir.write_text("файл вместо каталога")
    patcher, _, _ = make_browser_mock(cards=[])
    with patcher:
        assert parser.fetch_page(SPEC, {}, 1, threading.Event()) == []


def test_ads_found_writes_no_dump(parser, debug_dir):
    patcher, _, _ = make_browser_mock(cards=twenty_cards())
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 20
    assert not debug_dir.exists()


def test_fewer_than_five_ads_saves_incomplete_html(parser, debug_dir, caplog):
    """Меньше 5 объявлений — аномалия: HTML в ``drom_page_{N}_incomplete.html``."""
    patcher, _, _ = make_browser_mock(LISTING_HTML, cards=CARDS)
    with patcher, caplog.at_level("WARNING", logger="aprice.server.parsers.drom"):
        assert len(parser.fetch_page(SPEC, {}, 2, threading.Event())) == 2
    dump = debug_dir / "drom_page_2_incomplete.html"
    assert dump.read_text(encoding="utf-8") == LISTING_HTML
    assert "только 2" in caplog.text


def test_four_ads_is_incomplete(parser, debug_dir):
    patcher, _, _ = make_browser_mock(cards=twenty_cards(4))
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 4
    assert (debug_dir / "drom_page_1_incomplete.html").exists()


def test_exactly_five_ads_is_not_incomplete(parser, debug_dir):
    patcher, _, _ = make_browser_mock(cards=twenty_cards(5))
    with patcher:
        assert len(parser.fetch_page(SPEC, {}, 1, threading.Event())) == 5
    assert not debug_dir.exists()


# ----------------------------------------------------------------------
# _parse_listings_from_page
# ----------------------------------------------------------------------
def test_parse_listings_dom_two_cards(parser):
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = CARDS
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert [a.price for a in ads] == [4_190_000, 3_850_000]  # дубли двух источников склеены
    assert [c.args[0] for c in tab.locator.call_args_list] == [
        '[data-ftid="bulls-list_bull"]',
        '[data-ftid="bull_title"], a[href*=".html"]',
    ]


def locator_results(**by_selector):
    """Фейковый ``tab.locator``: у каждого селектора свой ответ ``evaluate_all``."""
    tab = MagicMock()
    tab.locator.side_effect = lambda sel: MagicMock(
        evaluate_all=MagicMock(return_value=by_selector.get(sel, []))
    )
    return tab


CONTAINERS = '[data-ftid="bulls-list_bull"]'
LINKS = '[data-ftid="bull_title"], a[href*=".html"]'


def test_parse_all_twenty_cards(parser):
    tab = locator_results(**{CONTAINERS: twenty_cards()})
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert len(ads) == 20 and len({a.url for a in ads}) == 20
    assert all(a.price and a.year and a.mileage for a in ads)


def test_parse_one_marked_link_does_not_hide_other_cards(parser):
    """Регрессия «1 из 20»: ``bull_title`` у одной карточки, остальные 19 — в контейнерах."""
    cards = twenty_cards()
    marked = dict(cards[0], source="link", marked=True)
    tab = locator_results(**{CONTAINERS: cards, LINKS: [marked]})
    assert len(parser._parse_listings_from_page(tab, SPEC)) == 20


def test_parse_links_only_without_containers(parser):
    links = [dict(c, source="link", marked=False) for c in twenty_cards()]
    tab = locator_results(**{LINKS: links})
    assert len(parser._parse_listings_from_page(tab, SPEC)) == 20


def test_parse_link_filter_drops_navigation_html_links(parser):
    links = [
        {"source": "link", "marked": False, "href": "https://www.drom.ru/about.html",
         "text": "О проекте 2019"},                       # навигация
        {"source": "link", "marked": False, "href": "https://moscow.drom.ru/bmw/x5/1234567.html",
         "text": "BMW X5\n4 000 000 ₽"},                  # объявление без маркера
        {"source": "link", "marked": True, "href": "https://x.drom.ru/promo",
         "text": "BMW X5\n5 000 000 ₽"},                  # маркер заголовка
    ]
    ads = parser._parse_listings_from_page(locator_results(**{LINKS: links}), SPEC)
    assert [a.price for a in ads] == [4_000_000, 5_000_000]


def test_card_with_url_and_price_is_kept_without_year_and_mileage(parser):
    tab = locator_results(**{CONTAINERS: [
        {"source": "card", "href": "https://x.drom.ru/1.html", "text": "BMW X5\n4 000 000 ₽"},
    ]})
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert len(ads) == 1 and ads[0].year is None and ads[0].mileage is None


def test_card_with_only_year_is_kept(parser):
    tab = locator_results(**{CONTAINERS: [
        {"source": "card", "href": "https://x.drom.ru/2.html", "text": "BMW X5, 2019"},
    ]})
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert len(ads) == 1 and ads[0].year == 2019 and ads[0].price is None


def test_container_wins_over_link_for_same_url(parser):
    """Одно объявление найдено и контейнером (полный текст), и ссылкой (короткий)."""
    url = "https://x.drom.ru/3000000.html"
    tab = locator_results(**{
        CONTAINERS: [{"source": "card", "href": url, "text": "BMW X5, 2019\n5 000 000 ₽"}],
        LINKS: [{"source": "link", "marked": True, "href": url, "text": "BMW X5, 2019"}],
    })
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert len(ads) == 1 and ads[0].price == 5_000_000


def test_parse_listings_skips_banners_and_dedups(parser):
    cards = [
        CARDS[0],
        CARDS[0],  # дубль
        {"href": None, "text": "Реклама 1 000 000 ₽"},  # нет ссылки
        {"href": "https://www.drom.ru/promo/", "text": "Выгодное предложение"},  # нет полей
        {"href": "/bmw/x5/333.html", "text": "BMW X5\n5 100 000 ₽"},  # относительный href
    ]
    tab = MagicMock()
    tab.locator.return_value.evaluate_all.return_value = cards
    ads = parser._parse_listings_from_page(tab, SPEC)
    assert [a.url for a in ads] == [
        "https://moscow.drom.ru/bmw/x5/111.html",
        "https://www.drom.ru/bmw/x5/333.html",
    ]
    last = ads[-1]
    assert last.price == 5_100_000
    assert last.year is None and last.mileage is None and last.engine_volume is None
    assert last.transmission is None and last.fuel is None


# ----------------------------------------------------------------------
# Регулярки
# ----------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("4 190 000 ₽", 4_190_000),
    ("4 190 000 ₽", 4_190_000),
    ("3 850 000 руб.", 3_850_000),
    ("2019\n4 190 000 ₽", 4_190_000),  # год перед ценой не «слипается»
    ("без цены", None),
])
def test_regex_price(text, expected):
    assert DromParser._extract_price(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("160 661 км", 160_661),
    ("95 000 км", 95_000),
    ("2018\n120 500 км", 120_500),
    ("500 км", 500),
    ("Без пробега", None),
])
def test_regex_mileage(text, expected):
    assert DromParser._extract_mileage(text) == expected


def test_regex_year_volume_labels():
    assert DromParser._extract_year("BMW X5, 2019") == 2019
    assert DromParser._extract_year("4 190 000 ₽") is None
    assert DromParser._extract_volume("3.0 л (249 л.с.)") == 3.0
    assert DromParser._extract_volume("249 л.с.") is None
    find = DromParser._find_label
    assert find("Механика", drom_module.TRANSMISSION_LABELS) == "МКПП"
    assert find("робот", drom_module.TRANSMISSION_LABELS) == "Робот"
    assert find("ЭЛЕКТРО", drom_module.FUEL_LABELS) == "Электро"


# ----------------------------------------------------------------------
# collect / close
# ----------------------------------------------------------------------
def test_collect_real_mode_stops_on_empty_page_and_respects_limits(parser):
    pages = iter([
        [Ad(url=f"https://drom.ru/{i}") for i in range(3)],
        [Ad(url=f"https://drom.ru/{i}") for i in range(10, 13)],
        [],
    ])
    seen: list[int] = []
    with patch.object(parser, "fetch_page", side_effect=lambda *a: next(pages)), \
         patch.object(drom_module, "interruptible_sleep", return_value=True):
        ads = parser.collect(
            SPEC, {"max_pages_per_site": 5, "max_ads_per_site": 4},
            threading.Event(), progress_cb=lambda p, n: seen.append(n),
        )
    assert len(ads) == 4 and seen == [3, 4]


def test_close_is_noop(parser):
    assert parser.close() is None


# ----------------------------------------------------------------------
# JS-сценарии в настоящем Chromium (офлайн: set_content, без сети)
# ----------------------------------------------------------------------
def _browser_html() -> str:
    """20 карточек: ``bull_title`` только у первой, у остальных — просто ссылка ``N.html``."""
    cards = []
    for i in range(20):
        title = (
            f'<a data-ftid="bull_title" href="/bmw/x5/{1000000 + i}.html">BMW X5, 2019</a>'
            if i == 0 else f'<a href="/bmw/x5/{1000000 + i}.html">BMW X5, 2019</a>'
        )
        cards.append(
            f'<div data-ftid="bulls-list_bull"><div>{title}</div>'
            f"<div>{3_000_000 + i} ₽</div><div>{80_000 + i} км</div></div>"
        )
    return (
        '<nav><a href="/about.html">О проекте</a></nav>'
        + "".join(cards)
        + '<div class="wrap"><div class="inner"><span data-ftid="bull_title">'
        '<a href="https://spb.drom.ru/bmw/x5/2000000.html">BMW X5, 2018</a></span>'
        "<div>3 850 000 ₽</div></div></div>"
    )


@pytest.fixture(scope="module")
def chromium_page():
    """Настоящая страница Chromium; нет браузера -> тесты пропускаются."""
    from playwright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - браузер может быть не установлен
        pytest.skip(f"Chromium недоступен: {exc}")
    page = browser.new_page()
    page.set_content(_browser_html())
    yield page
    browser.close()
    pw.stop()


def test_scripts_return_every_element_in_real_browser(chromium_page):
    """Оба JS-сценария отдают по записи на КАЖДЫЙ элемент, а не только на первый."""
    cards = chromium_page.locator(drom_module.CARD_SELECTOR).evaluate_all(
        drom_module.CARDS_SCRIPT
    )
    links = chromium_page.locator(drom_module.LINKS_SELECTOR).evaluate_all(
        drom_module.LINKS_SCRIPT
    )
    assert len(cards) == 20
    assert all(c["href"] and "₽" in c["text"] for c in cards)
    assert len(links) >= 21  # заголовок + 19 ссылок + 1 вне контейнера (+ навигация)
    cards_text = {c["href"]: c["text"] for c in cards}
    assert len(cards_text) == 20


def test_parser_collects_all_twenty_plus_one_in_real_browser(chromium_page, tmp_path):
    """Сквозная проверка на настоящем DOM: 20 карточек + заголовок вне контейнера."""
    parser = DromParser(Config(tmp_path / "s.json"))
    ads = parser._parse_listings_from_page(chromium_page, SPEC)
    urls = {a.url.rsplit("/", 1)[-1] for a in ads}
    assert len(ads) == 21
    assert "about.html" not in urls  # навигационная ссылка отсечена
    assert "2000000.html" in urls
    by_url = {a.url.rsplit("/", 1)[-1]: a for a in ads}
    assert by_url["1000007.html"].price == 3_000_007
    assert by_url["1000007.html"].mileage == 80_007
    assert by_url["2000000.html"].price == 3_850_000


def test_text_fallback_when_inner_text_is_empty_in_real_browser(chromium_page):
    """``content-visibility:hidden`` даёт пустой innerText — текст берётся из узлов."""
    chromium_page.evaluate(
        """() => { const d = document.createElement('div');
        d.setAttribute('data-ftid', 'bulls-list_bull'); d.id = 'hidden-card';
        d.style.cssText = 'content-visibility:hidden;contain-intrinsic-size:100px';
        d.innerHTML = '<a href="/bmw/x5/9999999.html">BMW X5</a><div>2 222 222 ₽</div>';
        document.body.appendChild(d); }"""
    )
    try:
        cards = chromium_page.locator("#hidden-card").evaluate_all(drom_module.CARDS_SCRIPT)
    finally:
        chromium_page.evaluate("document.getElementById('hidden-card').remove()")
    assert len(cards) == 1 and "2 222 222 ₽" in cards[0]["text"]
