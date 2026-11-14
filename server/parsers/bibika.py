"""Реальный парсер выдачи bibika.ru на Playwright (подзадача 3.10).

Планировщик ``TaskManager`` вызывает :meth:`BibikaParser.fetch_page` — одна
страница выдачи за вызов; паузы между запросами выдерживает он сам.

При ``use_stub_data`` (и ``simulate_captcha`` / ``simulate_block``) парсер ведёт
себя как заглушка 3.3: автотесты не ходят в сеть. ``test_fast_mode`` только
сокращает паузы и на выбор «заглушка / сеть» не влияет.

Исследование структуры bibika.ru
--------------------------------
**НЕ ПРОВЕРЕНО В СЕТИ, реализовано по гипотезе из ТЗ** (из среды разработки
bibika.ru недоступен). Селекторы — предположения; если выдача не найдётся,
парсер сохраняет HTML в ``data/debug/bibika_page_N_<время>.html`` и бросает
``BlockedError(no_listings_selector)`` — по дампу селекторы будут уточнены.
Обход защиты не делается (ни прокси, ни OCR, ни подмены User-Agent).

* **URL:** ``https://bibika.ru/{марка}/{модель}/?year_from=..&year_to=..&page=N``;
  ``page`` для первой страницы не добавляется. Общие для площадок поля
  ``spec.region`` / ``region_slug`` здесь не используются.
* **Выдача:** серверный HTML. Карточка — ``div[data-id], div.bb-item`` (гипотеза);
  ссылка — первая ``a[href*="/cars/"]`` / ``a[href*="/avto/"]`` внутри карточки;
  цена — элемент ``[class*="price"]``, запасной вариант — число перед ``₽`` в тексте.
  Принимаются только ссылки на хост ``bibika.ru`` с ``/cars/`` или ``/avto/`` и
  номером (4+ цифры) в пути; рубрики и навигация отбрасываются.
* **Поля** (год, пробег, объём, коробка, топливо) берутся регулярками из текста
  карточки; марка и модель — из ``spec``; весь текст — в ``raw_json["text"]``.
* **Пост-фильтр по годам:** сайт может игнорировать ``year_from``/``year_to``, поэтому
  карточки с годом вне ``[year_from, year_to]`` отбрасываются в Python (год
  неизвестен — карточка проходит).
* **Блокировка:** ``captcha`` в URL или HTML-фраза «Подтвердите, что вы не робот»
  -> :class:`CaptchaDetectedError`; ``blocked`` / ``firewall`` в URL, фраза
  «Доступ ограничен» или HTTP 403/429 -> :class:`BlockedError`.
* **Рендеринг:** как у остальных площадок — после первой карточки ждём не менее 10,
  затем короткая пауза (на серверном HTML это лишь страховка).
"""

from __future__ import annotations

import json
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

logger = logging.getLogger("aprice.server.parsers.bibika")

BASE_URL = "https://bibika.ru"
#: Путь выдачи по марке и модели (не подтверждён, см. docstring модуля).
URL_TEMPLATE = BASE_URL + "/{brand}/{model}/"
QUERY_PARAMS = {"year_from": "year_from", "year_to": "year_to", "page": "page"}

GOTO_TIMEOUT_MS = 30_000
LISTINGS_TIMEOUT_MS = 10_000
GOTO_TIMEOUT_REASON = "goto_timeout"
NO_LISTINGS_REASON = "no_listings_selector"
#: Каталог дампов HTML (в ``.gitignore``).
DEBUG_DIR: Path = constants.DATA_DIR / "debug"

#: Карточка объявления (гипотеза: серверный HTML bibika.ru).
CARD_SELECTOR = 'div[data-id], div.bb-item'
LISTINGS_SELECTOR = CARD_SELECTOR
LINK_SELECTOR = 'a[href*="/cars/"], a[href*="/avto/"]'
PRICE_SELECTOR = '[class*="price"]'
#: Ссылка на объявление: хост bibika.ru, ``/cars/`` или ``/avto/`` и номер (4+ цифры).
BIBIKA_HOST_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)*bibika\.ru(?:[:/?#]|$)", re.IGNORECASE)
LISTING_PATH_RE = re.compile(r"/(?:cars|avto)/[^?#]*\d{4,}")
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
#: JS для ``evaluate_all`` по карточкам: ``href`` ссылки, текст цены и текст карточки.
CARDS_SCRIPT = (
    "els => { " + _JS_TEXT +
    f" return els.map(e => {{"
    f" const l = e.querySelector({json.dumps(LINK_SELECTOR)});"
    f" const pe = e.querySelector({json.dumps(PRICE_SELECTOR)});"
    " return {href: (l && l.href) || null, price_text: pe ? tx(pe) : null, text: tx(e)}; }); }"
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


class BibikaParser(BaseSiteParser):
    """Парсер выдачи bibika.ru на Playwright (см. исследование в docstring модуля)."""

    site_name = "bibika.ru"

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
        """Собирает одну страницу выдачи bibika.ru.

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

        url = self._build_url(spec, page)
        logger.info("[bibika.ru] страница %d: %s", page, url)
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
                raise ParserError(f"bibika.ru: no response for {url}")

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
                logger.info("[bibika.ru] финальный URL: %s, html_len=%d", current_url, len(html))
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
            logger.info("[bibika.ru] финальный URL: %s, html_len=%d", current_url, len(html))
            ads = self._parse_listings_from_page(tab, spec)  # пока вкладка открыта

        if not ads:
            self._report_empty(html, current_url, page)
        elif len(ads) < INCOMPLETE_THRESHOLD:
            logger.warning(
                "[bibika.ru] собрано только %d объявлений (url=%s) — сохраняю HTML",
                len(ads), current_url,
            )
            self._save_debug_html(html, f"bibika_page_{page}_incomplete.html")
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
    def _build_url(self, spec: dict, page: int) -> str:
        """Строит URL выдачи ``https://bibika.ru/{марка}/{модель}/?year_from=..&year_to=..&page=N``.

        Args:
            spec: спецификация (``manufacturer``, ``model``, ``year_from``,
                ``year_to``; необязательные ``manufacturer_slug`` / ``model_slug``
                переопределяют slug).
            page: номер страницы, с 1; для первой страницы ``page`` не добавляется.

        Returns:
            str: готовый URL (без запроса, если нет ни годов, ни страницы > 1).
        """
        brand = spec.get("manufacturer_slug") or _slug(str(spec.get("manufacturer", "")))
        model = spec.get("model_slug") or _slug(str(spec.get("model", "")))
        url = URL_TEMPLATE.format(brand=brand, model=model)
        params = self._query_params(spec, page)
        return url + ("?" + urlencode(params) if params else "")

    @staticmethod
    def _query_params(spec: dict, page: int) -> list[tuple[str, Any]]:
        """Параметры годов и номера страницы (``page`` — только со второй)."""
        params: list[tuple[str, Any]] = []
        if spec.get("year_from"):
            params.append((QUERY_PARAMS["year_from"], spec["year_from"]))
        if spec.get("year_to"):
            params.append((QUERY_PARAMS["year_to"], spec["year_to"]))
        if page > 1:
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
            "[bibika.ru] парсинг вернул 0 объявлений: url=%s, html_len=%d, has_card=%s",
            url, len(html), 'data-id=' in html or 'bb-item' in html,
        )
        self._save_debug_html(html, f"bibika_page_{page}_{int(time.time())}.html")

    @staticmethod
    def _save_debug_html(html: str, filename: str) -> None:
        """Сохраняет HTML в :data:`DEBUG_DIR`; сбой записи только логируется."""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / filename
            path.write_text(html, encoding="utf-8")
            logger.warning("[bibika.ru] HTML сохранён: %s", path)
        except OSError:
            logger.warning("[bibika.ru] не удалось сохранить HTML для анализа", exc_info=True)

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
            logger.info("[bibika.ru] карточек меньше %d или страница не дорисовалась — парсим как есть",
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
                logger.debug("[bibika.ru] не объявление, пропущено: %s", href)
                continue
            ad = self._card_to_ad(
                url, card.get("price_text") or "", card.get("text") or "", spec
            )
            if ad is None:
                skipped["no_fields"] += 1
            elif not self._year_in_range(ad.year, spec):
                skipped["out_of_range"] += 1
                logger.debug("[bibika.ru] год %s вне диапазона, пропущено: %s", ad.year, url)
            elif ad.url in unique:
                skipped["duplicate"] += 1
            else:
                unique[ad.url] = ad
        logger.info(
            "[bibika.ru] карточек=%d, принято=%d, пропущено: %s",
            len(cards), len(unique), skipped,
        )
        if skipped["out_of_range"] and not unique:
            logger.warning(
                "[bibika.ru] все %d карточек вне диапазона лет %s–%s: площадка, "
                "похоже, игнорирует фильтр minyear/maxyear",
                skipped["out_of_range"], spec.get("year_from"), spec.get("year_to"),
            )
        return list(unique.values())

    def _card_to_ad(
        self, url: str, price_text: str, text: str, spec: dict
    ) -> Optional[Ad]:
        """Собирает :class:`Ad` из ссылки, текста цены и текста карточки.

        Год и пробег берутся **только из текста** карточки, но не из URL.

        Returns:
            Optional[Ad]: объявление или ``None``, если нет цены, года и пробега.
        """
        facts = text
        price = self._extract_price(price_text)
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
            raw_json={"source": "bibika.ru", "text": text},
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
        """``True`` для ссылки на объявление: хост bibika.ru и путь ``..._<номер>``."""
        return bool(BIBIKA_HOST_RE.match(url) and LISTING_PATH_RE.search(url))

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
