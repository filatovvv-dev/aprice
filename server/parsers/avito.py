"""Реальный парсер выдачи avito.ru на Playwright (подзадача 3.9).

Планировщик ``TaskManager`` вызывает :meth:`AvitoParser.fetch_page` — одна
страница выдачи за вызов; паузы между запросами выдерживает он сам.

При ``use_stub_data`` (и ``simulate_captcha`` / ``simulate_block``) парсер ведёт
себя как заглушка 3.3: автотесты не ходят в сеть. ``test_fast_mode`` только
сокращает паузы и на выбор «заглушка / сеть» не влияет.

Исследование структуры avito.ru
-------------------------------
**НЕ ПРОВЕРЕНО В СЕТИ, реализовано по гипотезе из ТЗ** (из среды разработки
avito.ru недоступен). Площадка — самая защищённая из пяти (Servicepipe,
фингерпринтинг, лимиты по IP): ожидаемый исход смоука — либо объявления, либо
честная ``captcha`` / ``blocked``. Обход защиты **не делается** (ни прокси, ни
OCR, ни подмены User-Agent).

* **URL:** ``https://www.avito.ru/{регион}/avtomobili/{марка}/{модель}`` +
  ``?minyear=..&maxyear=..&p={страница}``. Регион — ``spec.region_slug``, без него
  ``rossiya`` (вся Россия). Параметры года ``minyear``/``maxyear`` взяты из ТЗ и
  не подтверждены. Запасной вариант — текстовый поиск
  ``/{регион}/avtomobili?q={марка}+{модель}&...`` (:meth:`AvitoParser._build_search_url`,
  включается ``options["avito_text_search"]``). Общее для площадок поле
  ``spec.region`` здесь не используется.
* **Выдача:** карточка — ``[data-marker="item"]``; ссылка —
  ``a[data-marker="item-title"]`` (относительная, дополняется ``https://www.avito.ru``);
  цена — ``[data-marker="item-price"]`` (атрибут ``content`` или
  ``meta[itemprop="price"]``), запасной вариант — число перед ``₽`` в тексте.
  Принимаются только ссылки на хост ``avito.ru`` с путём, оканчивающимся на
  ``_<6+ цифр>`` (номер объявления); остальное — реклама/навигация.
* **Поля** (год, пробег, объём, коробка, топливо) берутся регулярками из текста
  карточки; марка и модель — из ``spec``; весь текст — в ``raw_json["text"]``.
* **Блокировка:** ``captcha`` в URL или HTML-фраза «Подтвердите, что вы не робот»
  -> :class:`CaptchaDetectedError`; ``blocked`` / ``firewall`` в URL, фраза
  «Доступ ограничен» или HTTP 403/429 -> :class:`BlockedError`.
* **Рендеринг:** выдача — React; после первой карточки ждём не менее 10
  (как в drom.ru), затем короткая пауза.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from threading import Event
from typing import Any, Optional
from urllib.parse import quote, urlencode

from common import constants
from common.config import Config
from common.utils import interruptible_sleep
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    PageCallback,
    ParserError,
    ProgressCallback,
)
from server.parsers.browser import PlaywrightBrowser

try:  # исключение таймаута Playwright; без пакета ловить нечего
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - зависит от окружения
    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Запасной тип, если Playwright не установлен."""

logger = logging.getLogger("aprice.server.parsers.avito")

BASE_URL = "https://www.avito.ru"
DEFAULT_REGION = "rossiya"
#: Путь выдачи по марке и модели (не подтверждён, см. docstring модуля).
URL_TEMPLATE = BASE_URL + "/{region}/avtomobili/{brand}/{model}"
#: Запасной путь: текстовый поиск.
SEARCH_URL_TEMPLATE = BASE_URL + "/{region}/avtomobili"
QUERY_PARAMS = {"year_from": "minyear", "year_to": "maxyear", "page": "p"}

GOTO_TIMEOUT_MS = 30_000
LISTINGS_TIMEOUT_MS = 10_000
GOTO_TIMEOUT_REASON = "goto_timeout"
NO_LISTINGS_REASON = "no_listings_selector"
#: Каталог дампов HTML (в ``.gitignore``).
DEBUG_DIR: Path = constants.DATA_DIR / "debug"

CARD_SELECTOR = '[data-marker="item"]'
LISTINGS_SELECTOR = CARD_SELECTOR
#: Ссылка на объявление: хост avito.ru, путь оканчивается номером ``_1234567890``.
AVITO_HOST_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)*avito\.ru(?:[:/?#]|$)", re.IGNORECASE)
LISTING_PATH_RE = re.compile(r"_\d{6,}(?:[?#/]|$)")
#: Меньше стольких объявлений — аномалия: HTML сохраняется для разбора.
INCOMPLETE_THRESHOLD = 5
#: Ждём, пока React дорисует карточки (как в drom.ru).
FULL_LIST_MIN_CARDS = 10
FULL_LIST_TIMEOUT_MS = 5_000
FULL_LIST_SETTLE_MS = 1_500
FULL_LIST_JS = (
    f"document.querySelectorAll('{CARD_SELECTOR}').length >= {FULL_LIST_MIN_CARDS}"
)

#: Маркеры в URL: ``captcha`` — капча, остальные — блокировка.
CAPTCHA_URL_MARKERS = ("captcha",)
BLOCKED_URL_MARKERS = ("blocked", "firewall")
#: Фразы на странице (в нижнем регистре).
CAPTCHA_HTML_MARKERS = ("подтвердите, что вы не робот", "captcha")
BLOCKED_HTML_MARKERS = ("доступ ограничен",)
EMPTY_MARKERS = ("ничего не найдено", "не найдено")
BLOCKED_STATUSES = (403, 429)

#: JS-функция ``tx``: видимый текст; при пустом ``innerText`` — обход текстовых узлов.
_JS_TEXT = (
    "const tx = el => { if (el.innerText) return el.innerText;"
    " const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT); const p = [];"
    " while (w.nextNode()) { const t = w.currentNode.nodeValue.trim(); if (t) p.push(t); }"
    " return p.join('\\n'); };"
)
#: JS для ``evaluate_all`` по карточкам: ``href`` заголовка, цена из атрибута и текст.
CARDS_SCRIPT = (
    "els => { " + _JS_TEXT +
    " return els.map(e => {"
    " const l = e.querySelector('a[data-marker=\"item-title\"]');"
    " const pe = e.querySelector('[data-marker=\"item-price\"]');"
    " const pm = e.querySelector('meta[itemprop=\"price\"]');"
    " const price = (pm && pm.getAttribute('content'))"
    " || (pe && pe.getAttribute('content')) || null;"
    " const params = Array.from(e.querySelectorAll('[data-marker=\"item-specific-param\"]'))"
    ".map(tx).filter(Boolean).join(', ');"
    " return {href: (l && l.href) || null, price: price, text: tx(e), params: params}; }); }"
)

#: Число с группами тысяч по 3 цифры: соседнее число (год) не «слипается» с суммой.
_NUMBER = r"(?<!\d)(\d{1,3}(?:[\s  ]\d{3})+|\d+)"
_PRICE_RE = re.compile(_NUMBER + r"\s*(?:₽|руб)", re.IGNORECASE)
_MILEAGE_RE = re.compile(_NUMBER + r"\s*км", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
_VOLUME_RE = re.compile(r"(\d[.,]\d)\s*л(?![а-яё])", re.IGNORECASE)

TRANSMISSION_LABELS = {
    "акпп": "АКПП", "автомат": "АКПП",
    "мкпп": "МКПП", "механик": "МКПП", "механич": "МКПП",
    "вариатор": "Вариатор", "робот": "Робот",
}
FUEL_LABELS = {
    "бензин": "Бензин", "дизель": "Дизель", "гибрид": "Гибрид", "электро": "Электро",
}


def _slug(value: str) -> str:
    """Slug для пути URL: нижний регистр, пробелы -> ``_``, экранирование."""
    return quote(value.strip().lower().replace(" ", "_"), safe="_-")


class AvitoParser(BaseSiteParser):
    """Парсер выдачи avito.ru на Playwright (см. исследование в docstring модуля)."""

    site_name = "avito.ru"

    def __init__(self, config: Config) -> None:
        """Читает настройки браузера из секции ``server`` (BR-07).

        Args:
            config: конфигурация приложения.
        """
        super().__init__(config)
        self._headless = bool(config.get("server", "playwright_headless", True))
        self._ignore_ssl = bool(config.get("server", "ignore_https_errors", True))

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def fetch_page(
        self,
        spec: dict,
        options: dict,
        page: int,
        stop_event: Event,
    ) -> list[Ad]:
        """Собирает одну страницу выдачи avito.ru.

        Args:
            spec: спецификация задания.
            options: опции задания.
            page: номер страницы, с 1.
            stop_event: событие остановки (проверяется до и после загрузки).

        Returns:
            list[Ad]: объявления страницы; ``[]`` — пустая выдача или остановка.

        Raises:
            CaptchaDetectedError: ``captcha`` в URL либо просьба подтвердить, что
                вы не робот (без обхода).
            BlockedError: ``blocked``/``firewall`` в URL, «Доступ ограничен»,
                HTTP 403/429 (``detail="http_<код>"``), таймаут навигации
                (``detail="goto_timeout"``) или нет карточек за 10 с
                (``detail="no_listings_selector"``).
            ParserError: нет ответа на запрос.
        """
        self._check_simulation(options)
        if self._use_stub(options):
            return super().fetch_page(spec, options, page, stop_event)
        if stop_event.is_set():
            return []

        url = self._build_url(spec, page, text_search=bool(options.get("avito_text_search")))
        logger.info("[avito.ru] страница %d: %s", page, url)
        with PlaywrightBrowser(
            headless=self._headless, ignore_https_errors=self._ignore_ssl
        ) as browser:
            tab = browser.new_page()
            response = None
            goto_error: Optional[PlaywrightTimeoutError] = None
            try:
                response = tab.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="commit")
            except PlaywrightTimeoutError as exc:
                goto_error = exc

            current_url = tab.url or ""
            response_url = (getattr(response, "url", "") or "") if response else ""
            for candidate in (current_url, response_url):
                self._raise_if_blocked_url(candidate)
            if goto_error is not None:
                raise BlockedError(self.site_name, GOTO_TIMEOUT_REASON)
            if stop_event.is_set():
                return []
            if response is None:
                raise ParserError(f"avito.ru: no response for {url}")

            if response.status in BLOCKED_STATUSES:
                # Страница отказа может быть капчей — сначала смотрим HTML.
                self._raise_if_blocked_html(tab.content(), current_url or url)
                raise BlockedError(self.site_name, f"http_{response.status}")

            try:
                tab.wait_for_selector(
                    LISTINGS_SELECTOR, state="attached", timeout=LISTINGS_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                html = tab.content()
                logger.info("[avito.ru] финальный URL: %s, html_len=%d", current_url, len(html))
                self._raise_if_blocked_html(html, current_url or url)
                if any(m in html.lower() for m in EMPTY_MARKERS):
                    self._report_empty(html, current_url, page)
                    return []
                # Дамп ДО исключения: по HTML подбираются селекторы.
                self._report_empty(html, current_url, page)
                raise BlockedError(self.site_name, NO_LISTINGS_REASON)
            if stop_event.is_set():
                return []
            self._wait_full_list(tab)
            html = tab.content()
            logger.info("[avito.ru] финальный URL: %s, html_len=%d", current_url, len(html))
            ads = self._parse_listings_from_page(tab, spec)  # пока вкладка открыта

        if not ads:
            self._report_empty(html, current_url, page)
        elif len(ads) < INCOMPLETE_THRESHOLD:
            logger.warning(
                "[avito.ru] собрано только %d объявлений (url=%s) — сохраняю HTML",
                len(ads), current_url,
            )
            self._save_debug_html(html, f"avito_page_{page}_incomplete.html")
        return ads

    def collect(
        self,
        spec: dict,
        options: dict,
        stop_event: Event,
        progress_cb: Optional[ProgressCallback] = None,
        on_page: Optional[PageCallback] = None,
    ) -> list[Ad]:
        """Собирает все страницы подряд (обратная совместимость с 3.3).

        Планировщик использует :meth:`fetch_page`; этот метод нужен
        автономным вызовам. При ``use_stub_data`` работает как заглушка.
        """
        self._check_simulation(options)
        if self._use_stub(options):
            return self._collect_stub(spec, options, stop_event, progress_cb, on_page)

        max_pages = int(options.get("max_pages_per_site") or 10)
        max_ads = int(options.get("max_ads_per_site") or 500)
        delay_min, delay_max = self._page_delays(options)
        ads: list[Ad] = []
        page = 0
        while page < max_pages and len(ads) < max_ads and not stop_event.is_set():
            page += 1
            batch = self.fetch_page(spec, options, page, stop_event)[: max_ads - len(ads)]
            if not batch:
                break  # пустая выдача или остановка
            ads.extend(batch)
            if on_page is not None:
                on_page(list(batch))
            if progress_cb is not None:
                progress_cb(page, len(ads))
            if page >= max_pages or len(ads) >= max_ads:
                break
            if not interruptible_sleep(
                stop_event,
                random.uniform(delay_min, delay_max),
                check_interval=self.STOP_CHECK_INTERVAL_SEC,
            ):
                break
        return ads

    def close(self) -> None:
        """No-op: браузер создаётся и закрывается в каждом ``fetch_page``."""

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------
    def _build_url(self, spec: dict, page: int, text_search: bool = False) -> str:
        """Строит URL выдачи (без фильтров коробки/топлива/объёма).

        Args:
            spec: спецификация (``region_slug`` — регион avito, без него
                ``rossiya``; ``manufacturer``, ``model``, ``year_from``,
                ``year_to``; необязательные ``manufacturer_slug`` / ``model_slug``
                переопределяют slug).
            page: номер страницы, с 1; передаётся как ``p``.
            text_search: ``True`` — запасной URL текстового поиска.

        Returns:
            str: готовый URL.
        """
        if text_search:
            return self._build_search_url(spec, page)
        region = _slug(str(spec.get("region_slug") or DEFAULT_REGION))
        brand = spec.get("manufacturer_slug") or _slug(str(spec.get("manufacturer", "")))
        model = spec.get("model_slug") or _slug(str(spec.get("model", "")))
        url = URL_TEMPLATE.format(region=region, brand=brand, model=model)
        return url + "?" + urlencode(self._query_params(spec, page))

    def _build_search_url(self, spec: dict, page: int) -> str:
        """Запасной URL: ``/{регион}/avtomobili?q={марка}+{модель}&...&p={N}``."""
        region = _slug(str(spec.get("region_slug") or DEFAULT_REGION))
        query = f"{spec.get('manufacturer', '')} {spec.get('model', '')}".strip()
        params = [("q", query), *self._query_params(spec, page)]
        return SEARCH_URL_TEMPLATE.format(region=region) + "?" + urlencode(params)

    @staticmethod
    def _query_params(spec: dict, page: int) -> list[tuple[str, Any]]:
        """Параметры года и номера страницы (``p``)."""
        params: list[tuple[str, Any]] = []
        if spec.get("year_from"):
            params.append((QUERY_PARAMS["year_from"], spec["year_from"]))
        if spec.get("year_to"):
            params.append((QUERY_PARAMS["year_to"], spec["year_to"]))
        params.append((QUERY_PARAMS["page"], page))
        return params

    # ------------------------------------------------------------------
    # Блокировки и диагностика
    # ------------------------------------------------------------------
    def _raise_if_blocked_url(self, url: str) -> None:
        """По маркерам в URL: ``captcha`` -> капча; ``blocked``/``firewall`` -> блокировка."""
        lowered = url.lower()
        if any(m in lowered for m in CAPTCHA_URL_MARKERS):
            raise CaptchaDetectedError(self.site_name, url)
        if any(m in lowered for m in BLOCKED_URL_MARKERS):
            raise BlockedError(self.site_name, "blocked_url")

    def _raise_if_blocked_html(self, html: str, url: str) -> None:
        """По тексту страницы: просьба подтвердить, что вы не робот, или «Доступ ограничен».

        Args:
            html: HTML страницы.
            url: URL для сообщения об ошибке капчи.

        Raises:
            CaptchaDetectedError: найдена просьба подтвердить, что вы не робот.
            BlockedError: найдена фраза «Доступ ограничен».
        """
        lowered = html.lower()
        if any(m in lowered for m in CAPTCHA_HTML_MARKERS):
            raise CaptchaDetectedError(self.site_name, url)
        if any(m in lowered for m in BLOCKED_HTML_MARKERS):
            raise BlockedError(self.site_name, "access_restricted")

    def _report_empty(self, html: str, url: str, page: int) -> None:
        """Предупреждение в лог и дамп HTML в :data:`DEBUG_DIR` при 0 объявлений.

        Args:
            html: HTML страницы, давшей 0 объявлений.
            url: финальный URL страницы.
            page: номер страницы выдачи.
        """
        logger.warning(
            "[avito.ru] парсинг вернул 0 объявлений: url=%s, html_len=%d, has_item=%s",
            url, len(html), 'data-marker="item"' in html,
        )
        self._save_debug_html(html, f"avito_page_{page}_{int(time.time())}.html")

    @staticmethod
    def _save_debug_html(html: str, filename: str) -> None:
        """Сохраняет HTML в :data:`DEBUG_DIR`; сбой записи только логируется."""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / filename
            path.write_text(html, encoding="utf-8")
            logger.warning("[avito.ru] HTML сохранён: %s", path)
        except OSError:
            logger.warning("[avito.ru] не удалось сохранить HTML для анализа", exc_info=True)

    # ------------------------------------------------------------------
    # Разбор выдачи
    # ------------------------------------------------------------------
    @staticmethod
    def _wait_full_list(tab: Any) -> None:
        """Ждёт, пока React дорисует карточки (не меньше 10; таймаут — не ошибка).

        Затем короткая пауза: карточки могут дорисовываться порциями.
        """
        try:
            tab.wait_for_function(FULL_LIST_JS, timeout=FULL_LIST_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.info("[avito.ru] карточек меньше %d или React не успел — парсим как есть",
                        FULL_LIST_MIN_CARDS)
        tab.wait_for_timeout(FULL_LIST_SETTLE_MS)

    def _parse_listings_from_page(self, tab: Any, spec: dict) -> list[Ad]:
        """Извлекает все объявления страницы через ``Locator.evaluate_all``.

        Принимаются только карточки со ссылкой на объявление
        (:meth:`_is_listing_url`); без ссылки (реклама) и без цены, года и
        пробега — отбрасываются. Дубли по ``url`` склеиваются.

        Args:
            tab: открытая страница Playwright.
            spec: спецификация (источник марки и модели).

        Returns:
            list[Ad]: объявления без дублей по ``url``; ``[]`` — карточек нет.
        """
        cards = tab.locator(CARD_SELECTOR).evaluate_all(CARDS_SCRIPT) or []
        unique: dict[str, Ad] = {}
        skipped = {"no_href": 0, "not_listing": 0, "no_fields": 0, "out_of_range": 0, "duplicate": 0}
        for card in cards:
            href = card.get("href")
            if not href:
                skipped["no_href"] += 1
                continue
            url = self._absolute(href)
            if not self._is_listing_url(url):
                skipped["not_listing"] += 1
                logger.debug("[avito.ru] не объявление, пропущено: %s", href)
                continue
            ad = self._card_to_ad(
                url, card.get("price"), card.get("text") or "", spec, card.get("params") or ""
            )
            if ad is None:
                skipped["no_fields"] += 1
            elif not self._year_in_range(ad.year, spec):
                skipped["out_of_range"] += 1
                logger.debug("[avito.ru] год %s вне диапазона, пропущено: %s", ad.year, url)
            elif ad.url in unique:
                skipped["duplicate"] += 1
            else:
                unique[ad.url] = ad
        logger.info(
            "[avito.ru] карточек=%d, принято=%d, пропущено: %s",
            len(cards), len(unique), skipped,
        )
        if skipped["out_of_range"] and not unique:
            logger.warning(
                "[avito.ru] все %d карточек вне диапазона лет %s–%s: площадка, "
                "похоже, игнорирует фильтр minyear/maxyear",
                skipped["out_of_range"], spec.get("year_from"), spec.get("year_to"),
            )
        return list(unique.values())

    def _card_to_ad(
        self, url: str, price_attr: Optional[str], text: str, spec: dict, params: str = ""
    ) -> Optional[Ad]:
        """Собирает :class:`Ad` из ссылки, атрибута цены и текста карточки.

        Год и пробег берутся **только из текста** карточки и строки параметров
        (``item-specific-param``), но не из URL: номер года в slug ссылки
        (``bmw_x5_2026_123``) не считается.

        Returns:
            Optional[Ad]: объявление или ``None``, если нет цены, года и пробега.
        """
        facts = f"{text}\n{params}" if params else text
        price = self._price_from_attr(price_attr)
        if price is None:
            price = self._extract_price(text)
        year = self._extract_year(facts)
        mileage = self._extract_mileage(facts)
        if price is None and year is None and mileage is None:
            return None
        return Ad(
            url=url,
            price=price,
            year=year,
            manufacturer=spec.get("manufacturer"),
            model=spec.get("model"),
            transmission=self._find_label(facts, TRANSMISSION_LABELS),
            fuel=self._find_label(facts, FUEL_LABELS),
            engine_volume=self._extract_volume(facts),
            mileage=mileage,
            raw_json={"source": "avito.ru", "text": text},
        )

    @staticmethod
    def _year_in_range(year: Optional[int], spec: dict) -> bool:
        """Страховка от игнорирования ``minyear``/``maxyear``: год в ``[year_from, year_to]``.

        Год неизвестен (``None``) — карточка проходит; границы не заданы или не числа —
        не ограничивают.
        """
        if year is None:
            return True
        for key, ok in (("year_from", lambda b: year >= b), ("year_to", lambda b: year <= b)):
            try:
                bound = int(spec.get(key))
            except (TypeError, ValueError):
                continue
            if bound and not ok(bound):
                return False
        return True

    @staticmethod
    def _absolute(href: str) -> str:
        """Абсолютный URL: относительный ``href`` дополняется :data:`BASE_URL`."""
        return BASE_URL + href if href.startswith("/") else href

    @staticmethod
    def _is_listing_url(url: str) -> bool:
        """``True`` для ссылки на объявление: хост avito.ru и путь ``..._<номер>``."""
        return bool(AVITO_HOST_RE.match(url) and LISTING_PATH_RE.search(url))

    @staticmethod
    def _price_from_attr(value: Optional[str]) -> Optional[int]:
        """Цена из атрибута ``content`` (``"7690000"``); не число -> ``None``."""
        if value is None:
            return None
        digits = str(value).strip()
        return int(digits) if digits.isdigit() else None

    @staticmethod
    def _extract_price(text: str) -> Optional[int]:
        """Цена: число перед ``₽`` или ``руб`` (``"7 690 000 ₽"`` -> ``7690000``)."""
        match = _PRICE_RE.search(text)
        return int(re.sub(r"\D", "", match.group(1))) if match else None

    @staticmethod
    def _extract_mileage(text: str) -> Optional[int]:
        """Пробег: число перед ``км`` (``"103 583 км"`` -> ``103583``)."""
        match = _MILEAGE_RE.search(text)
        return int(re.sub(r"\D", "", match.group(1))) if match else None

    @staticmethod
    def _extract_year(text: str) -> Optional[int]:
        """Год выпуска: первое отдельно стоящее ``19xx``/``20xx``."""
        match = _YEAR_RE.search(text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_volume(text: str) -> Optional[float]:
        """Объём двигателя из текста вида ``3.0 л``."""
        match = _VOLUME_RE.search(text)
        return float(match.group(1).replace(",", ".")) if match else None

    @staticmethod
    def _find_label(text: str, labels: dict[str, str]) -> Optional[str]:
        """Первое найденное в тексте слово из словаря ``признак -> название``."""
        lowered = text.lower()
        for needle, label in labels.items():
            if needle in lowered:
                return label
        return None
