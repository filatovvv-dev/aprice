"""Парсер выдачи drom.ru (подзадача 3.8).

Реальная реализация на Playwright (:class:`~server.parsers.browser.PlaywrightBrowser`)
по схеме :class:`~server.parsers.autoru.AutoRuParser`: ``goto(wait_until="commit")``,
быстрая проверка URL на CAPTCHA, ожидание карточек, разбор текста карточек
регулярными выражениями. Планировщик вызывает :meth:`DromParser.fetch_page` —
одну страницу за вызов.

В ``test_fast_mode`` (и при ``simulate_captcha`` / ``simulate_block``) парсер
ведёт себя как заглушка 3.3: автотесты не ходят в сеть.

Исследование структуры drom.ru
------------------------------
**НЕ ПРОВЕРЕНО В СЕТИ, реализовано по гипотезе** (из среды разработки drom.ru
недоступен); подтвердит или опровергнет смоук-тест на Windows.

* **URL (проверено вручную Заказчиком):** ``https://auto.drom.ru/{марка}/{модель}/``
  — вся Россия; ``https://auto.drom.ru/region77/{марка}/{модель}/?minyear=..&maxyear=..``
  — регион по коду (77 — Москва, 78 — Санкт-Петербург); ``/all/`` и ``/moskva/``
  дают 404. Регион задают ``spec.region_id`` (код) или ``spec.region_slug``;
  не заданы — вся Россия. ``page=N`` добавляется со 2-й страницы; формат
  пагинации **не проверен** (:data:`QUERY_PARAMS`). Путь ``/catalog/`` из первой
  версии вёл в каталог моделей. Параметры коробки, топлива и объёма **не
  передаются** (фильтрация — на стороне Client).
* **Выдача:** маркеры ``data-ftid`` (стабильнее CSS-классов): карточка —
  ``[data-ftid="bulls-list_bull"]``, заголовок/ссылка —
  ``[data-ftid="bull_title"]`` (:data:`LISTINGS_SELECTOR`; ``a`` не требуется:
  ссылка берётся у самого элемента, его предка или потомка). Основной способ —
  найти заголовки ``bull_title`` и взять текст ближайшего предка-карточки;
  запасной — карточки ``bulls-list_bull`` и их первая ссылка. Селекторы
  **не проверены** на ``auto.drom.ru``: при 0 карточек смотреть HTML из
  ``data/debug/drom_page_*.html``.
* **Поля** берутся из текста карточки регулярками: цена — число перед ``₽`` или
  ``руб``, пробег — перед ``км``, год — ``19xx``/``20xx``, объём — ``N.N л``,
  коробка и топливо — по ключевым словам. Марка и модель — из ``spec``. Весь
  текст карточки хранится в ``raw_json["text"]``.
* **CAPTCHA:** ``captcha`` в URL (``https://www.drom.ru/captcha/?...``) или в
  HTML (проверяется только когда выдача не появилась и при 403/429, чтобы
  служебные скрипты нормальной страницы не давали ложных срабатываний). CAPTCHA
  не обходится.
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

logger = logging.getLogger("aprice.server.parsers.drom")

BASE_URL = "https://www.drom.ru"
#: Выдача объявлений живёт на поддомене ``auto.drom.ru`` (``/catalog/`` — каталог моделей).
AUTO_URL = "https://auto.drom.ru"
#: Путь выдачи (не подтверждён, см. docstring модуля).
URL_TEMPLATE = AUTO_URL + "/{region}{brand}/{model}/"
#: Сегмент пути для кода региона drom.ru: ``region77`` — Москва.
REGION_ID_SEGMENT = "region{region_id}/"
#: Имена параметров года и страницы в query (``minyear``/``maxyear`` подтверждены
#: ручной проверкой; ``page`` — нет).
QUERY_PARAMS = {"year_from": "minyear", "year_to": "maxyear", "page": "page"}

GOTO_TIMEOUT_MS = 30_000
LISTINGS_TIMEOUT_MS = 10_000
GOTO_TIMEOUT_REASON = "goto_timeout"
NO_LISTINGS_REASON = "no_listings_selector"
#: Каталог дампов HTML пустых страниц (в ``.gitignore``).
DEBUG_DIR: Path = constants.DATA_DIR / "debug"

#: Ссылка на объявление и карточка (атрибуты ``data-ftid``).
#: Без ``a``: на ``auto.drom.ru`` маркер может стоять и на обёртке заголовка.
TITLE_SELECTOR = '[data-ftid="bull_title"]'
CARD_SELECTOR = '[data-ftid="bulls-list_bull"]'
#: Признак готовности выдачи: карточка или ссылка на объявление.
LISTINGS_SELECTOR = f"{CARD_SELECTOR}, {TITLE_SELECTOR}"

CAPTCHA_MARKER = "captcha"
EMPTY_MARKERS = ("не найдено", "ничего не найдено")
BLOCKED_STATUSES = (403, 429)

#: JS для ``evaluate_all`` по ссылкам: ``href`` и текст ближайшего предка,
#: в котором уже есть цена (иначе — до 5 уровней вверх).
TITLES_SCRIPT = (
    "els => els.map(a => {"
    " let c = a.closest('[data-ftid=\"bulls-list_bull\"]');"
    " if (!c) { c = a;"
    "  for (let i = 0; i < 5 && c.parentElement && !/₽|руб/.test(c.innerText || ''); i++)"
    "   c = c.parentElement; }"
    " const l = a.matches('a') ? a : (a.closest('a') || a.querySelector('a'));"
    " return {href: (l && l.href) || null, text: c.innerText || ''}; })"
)
#: JS для ``evaluate_all`` по карточкам (запасной путь).
CARDS_SCRIPT = (
    "els => els.map(e => ({"
    "href: (e.querySelector('a[data-ftid=\"bull_title\"]') || e.querySelector('a') || {}).href"
    " || null, text: e.innerText || ''}))"
)

#: Число с группами тысяч по 3 цифры: соседнее число (год) не «слипается» с суммой.
_NUMBER = r"(?<!\d)(\d{1,3}(?:[    ]\d{3})+|\d+)"
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


class DromParser(BaseSiteParser):
    """Парсер выдачи drom.ru на Playwright (см. исследование в docstring модуля)."""

    site_name = "drom.ru"

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
        """Собирает одну страницу выдачи drom.ru.

        Args:
            spec: спецификация задания.
            options: опции задания.
            page: номер страницы, с 1.
            stop_event: событие остановки (проверяется до и после загрузки).

        Returns:
            list[Ad]: объявления страницы; ``[]`` — пустая выдача или остановка.

        Raises:
            CaptchaDetectedError: ``captcha`` в URL (в т.ч. после таймаута
                навигации) либо в HTML, когда выдача не появилась (без обхода).
            BlockedError: HTTP 403/429 (``detail="http_<код>"``), таймаут
                навигации (``detail="goto_timeout"``) или нет карточек выдачи
                за 10 с (``detail="no_listings_selector"``).
            ParserError: нет ответа на запрос.
        """
        self._check_simulation(options)
        if options.get("test_fast_mode"):
            return super().fetch_page(spec, options, page, stop_event)
        if stop_event.is_set():
            return []

        url = self._build_url(spec, page)
        logger.info("[drom.ru] страница %d: %s", page, url)
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
                if CAPTCHA_MARKER in candidate.lower():
                    raise CaptchaDetectedError(self.site_name, candidate)
            if goto_error is not None:
                raise BlockedError(self.site_name, GOTO_TIMEOUT_REASON)
            if stop_event.is_set():
                return []
            if response is None:
                raise ParserError(f"drom.ru: no response for {url}")

            if response.status in BLOCKED_STATUSES:
                # Страница отказа может быть капчей — сначала смотрим HTML.
                self._raise_if_captcha_html(tab.content(), current_url or url)
                raise BlockedError(self.site_name, f"http_{response.status}")

            try:
                tab.wait_for_selector(
                    LISTINGS_SELECTOR, state="attached", timeout=LISTINGS_TIMEOUT_MS
                )
            except PlaywrightTimeoutError:
                html = tab.content()
                logger.info("[drom.ru] финальный URL: %s, html_len=%d", current_url, len(html))
                self._raise_if_captcha_html(html, current_url or url)
                if any(m in html.lower() for m in EMPTY_MARKERS):
                    self._report_empty(html, current_url, page)
                    return []
                # Дамп ДО исключения: по HTML подбираются селекторы.
                self._report_empty(html, current_url, page)
                raise BlockedError(self.site_name, NO_LISTINGS_REASON)
            if stop_event.is_set():
                return []
            html = tab.content()
            logger.info("[drom.ru] финальный URL: %s, html_len=%d", current_url, len(html))
            ads = self._parse_listings_from_page(tab, spec)  # пока вкладка открыта

        if not ads:
            self._report_empty(html, current_url, page)
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
        автономным вызовам. В ``test_fast_mode`` работает как заглушка.
        """
        self._check_simulation(options)
        if options.get("test_fast_mode"):
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
        """Строит URL выдачи (без фильтров коробки/топлива/объёма).

        Args:
            spec: спецификация (``region_id`` — код региона drom.ru, либо
                ``region_slug``; без них — вся Россия; ``manufacturer``,
                ``model``, ``year_from``, ``year_to``; необязательные
                ``manufacturer_slug`` / ``model_slug`` переопределяют slug).
            page: номер страницы, с 1 (при 1 параметр ``page`` не добавляется).

        Returns:
            str: готовый URL.
        """
        region = self._region_segment(spec)
        brand = spec.get("manufacturer_slug") or _slug(str(spec.get("manufacturer", "")))
        model = spec.get("model_slug") or _slug(str(spec.get("model", "")))
        params: list[tuple[str, Any]] = []
        if spec.get("year_from"):
            params.append((QUERY_PARAMS["year_from"], spec["year_from"]))
        if spec.get("year_to"):
            params.append((QUERY_PARAMS["year_to"], spec["year_to"]))
        if page > 1:  # первая страница — без параметра, как в обычной ссылке
            params.append((QUERY_PARAMS["page"], page))
        url = URL_TEMPLATE.format(region=region, brand=brand, model=model)
        return url + "?" + urlencode(params) if params else url

    @staticmethod
    def _region_segment(spec: dict) -> str:
        """Сегмент пути региона: ``region{ID}/``, ``{slug}/`` или ``""`` (вся Россия).

        ``region_id`` приоритетнее ``region_slug``. Общее для площадок поле
        ``spec.region`` здесь **не** используется: slug вида ``moskva`` у drom.ru
        даёт 404.

        Args:
            spec: спецификация задания.
        """
        region_id = spec.get("region_id")
        if region_id:
            return REGION_ID_SEGMENT.format(region_id=int(region_id))
        region_slug = spec.get("region_slug")
        if region_slug:
            return _slug(str(region_slug)) + "/"
        return ""

    # ------------------------------------------------------------------
    # Диагностика
    # ------------------------------------------------------------------
    def _report_empty(self, html: str, url: str, page: int) -> None:
        """Предупреждение в лог и дамп HTML в :data:`DEBUG_DIR` при 0 объявлений.

        Сбой записи не должен ронять сбор.

        Args:
            html: HTML страницы, давшей 0 объявлений.
            url: финальный URL страницы.
            page: номер страницы выдачи.
        """
        logger.warning(
            "[drom.ru] парсинг вернул 0 объявлений: url=%s, html_len=%d, "
            "has_bulls_list=%s, has_bull_title=%s",
            url, len(html), "bulls-list_bull" in html, "bull_title" in html,
        )
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"drom_page_{page}_{int(time.time())}.html"
            path.write_text(html, encoding="utf-8")
            logger.warning("[drom.ru] HTML сохранён: %s", path)
        except OSError:
            logger.warning("[drom.ru] не удалось сохранить HTML для анализа", exc_info=True)

    def _raise_if_captcha_html(self, html: str, url: str) -> None:
        """Бросает :class:`CaptchaDetectedError`, если в HTML есть ``captcha``."""
        if CAPTCHA_MARKER in html.lower():
            raise CaptchaDetectedError(self.site_name, url)

    # ------------------------------------------------------------------
    # Разбор выдачи
    # ------------------------------------------------------------------
    def _parse_listings_from_page(self, tab: Any, spec: dict) -> list[Ad]:
        """Извлекает объявления из DOM через ``Locator.evaluate_all``.

        Сначала по ссылкам ``a[data-ftid="bull_title"]`` (текст — у ближайшего
        предка-карточки), при их отсутствии — по карточкам ``bulls-list_bull``.
        Карточки без ссылки и без цены, года и пробега (реклама) пропускаются.

        Args:
            tab: открытая страница Playwright.
            spec: спецификация (источник марки и модели).

        Returns:
            list[Ad]: объявления без дублей по ``url``; ``[]`` — карточек нет.
        """
        cards = tab.locator(TITLE_SELECTOR).evaluate_all(TITLES_SCRIPT) or []
        if not cards:
            cards = tab.locator(CARD_SELECTOR).evaluate_all(CARDS_SCRIPT) or []
        unique: dict[str, Ad] = {}
        for card in cards:
            ad = self._card_to_ad(card.get("href"), card.get("text") or "", spec)
            if ad is not None:
                unique.setdefault(ad.url, ad)
        return list(unique.values())

    def _card_to_ad(self, href: Optional[str], text: str, spec: dict) -> Optional[Ad]:
        """Собирает :class:`Ad` из ссылки и текста карточки.

        Args:
            href: ``href`` ссылки на объявление (``None`` — ссылки нет).
            text: видимый текст карточки.
            spec: спецификация (марка и модель).

        Returns:
            Optional[Ad]: объявление или ``None``, если карточка — реклама.
        """
        if not href:
            return None
        url = BASE_URL + href if href.startswith("/") else href
        price = self._extract_price(text)
        year = self._extract_year(text)
        mileage = self._extract_mileage(text)
        if price is None and year is None and mileage is None:
            return None
        return Ad(
            url=url,
            price=price,
            year=year,
            manufacturer=spec.get("manufacturer"),
            model=spec.get("model"),
            transmission=self._find_label(text, TRANSMISSION_LABELS),
            fuel=self._find_label(text, FUEL_LABELS),
            engine_volume=self._extract_volume(text),
            mileage=mileage,
            raw_json={"source": "drom.ru", "text": text},
        )

    @staticmethod
    def _extract_price(text: str) -> Optional[int]:
        """Цена: число перед ``₽`` или ``руб`` (``"4 190 000 ₽"`` -> ``4190000``)."""
        match = _PRICE_RE.search(text)
        return int(re.sub(r"\D", "", match.group(1))) if match else None

    @staticmethod
    def _extract_mileage(text: str) -> Optional[int]:
        """Пробег: число перед ``км`` (``"160 661 км"`` -> ``160661``)."""
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
