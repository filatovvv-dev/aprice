"""Парсер выдачи auto.ru (подзадача 3.7).

Реальная реализация на Playwright (:class:`~server.parsers.browser.PlaywrightBrowser`).
Планировщик ``TaskManager`` вызывает :meth:`AutoRuParser.fetch_page` — **одну**
страницу выдачи за вызов; паузы между запросами выдерживает он сам.

В ``test_fast_mode`` (и при ``simulate_captcha`` / ``simulate_block``)
парсер ведёт себя как заглушка 3.3: так автотесты 3.4–3.6 по-прежнему не
ходят в сеть.

Исследование структуры auto.ru
------------------------------
**НЕ ПРОВЕРЕНО В СЕТИ.** Из среды разработки auto.ru недоступен (внешний
доступ ограничен allowlist-прокси; по задаче №1 «голый» HTTP к нему
редиректит на ``showcaptcha``), поэтому разбор реализован по структуре из
публичных примеров/документации и должен быть подтверждён ручным smoke-тестом
на Windows (раздел 8 промпта 3.7). Все допущения собраны в константах модуля,
чтобы правка после проверки была локальной.

* **URL:** ``https://auto.ru/{регион}/cars/{марка}/{модель}/all/?year_from=..&year_to=..
  &transmission=..&fuel_type=..&page=N``. Значения ``transmission``
  (``AUTOMATIC``/``MECHANICAL``/``CVT``/``AMT``) и ``fuel_type``
  (``PETROL``/``DIESEL``/``HYBRID``/``ELECTRO``) — по промпту 3.7; у самого
  auto.ru, возможно, иные имена/значения (например ``engine_group``,
  ``VARIATOR``, ``ROBOT``) — таблицы :data:`TRANSMISSION_PARAMS` и
  :data:`FUEL_PARAMS`, имя параметра — :data:`FUEL_PARAM_NAME`.
* **Данные (приоритет 1):** JSON ``<script id="__NEXT_DATA__">`` (Next.js);
  объявлением считается любой словарь с ``id`` и ``price_info`` или
  ``vehicle_info``; поля: ``price_info.RUR`` (цена), ``documents.year``,
  ``state.mileage``, ``vehicle_info.mark_info/model_info.name``,
  ``vehicle_info.tech_param.transmission/engine_type/displacement`` (см³).
  Поиск рекурсивный, поэтому точный путь к списку не важен.
* **Данные (приоритет 2, запасной):** DOM-блоки ``ListingItem`` — ссылка
  ``ListingItemTitle__link``, ``ListingItem__price``, ``ListingItem__year``,
  ``ListingItem__kmAge``, сводка ``ListingItemTechSummary``.
* Поле, которое не нашлось, равно ``None``; разбор не падает.

CAPTCHA не обходится: ``showcaptcha`` в URL/HTML или ``smartcaptcha`` в HTML
→ :class:`CaptchaDetectedError`; HTTP 403/429 → :class:`BlockedError`.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import random
import re
import time
from pathlib import Path
from threading import Event
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode

from common import constants
from common.config import Config
from common.utils import interruptible_sleep
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    ParserError,
    PageCallback,
    ProgressCallback,
)
from server.parsers.browser import PlaywrightBrowser

try:  # исключение таймаута Playwright; без пакета ловить нечего
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except ImportError:  # pragma: no cover - зависит от окружения
    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Запасной тип, если Playwright не установлен."""

logger = logging.getLogger("aprice.server.parsers.autoru")

BASE_URL = "https://auto.ru"
#: Регион по умолчанию: без региона в пути auto.ru отдаёт заглушку/пустую выдачу.
DEFAULT_REGION = "moskva"
#: Каталог дампов HTML пустых страниц (в ``.gitignore``).
DEBUG_DIR: Path = constants.DATA_DIR / "debug"
GOTO_TIMEOUT_MS = 30_000  # FX-05: не увеличивать
SELECTOR_TIMEOUT_MS = 5_000
#: Причины :class:`BlockedError` (в ``detail``) — различают сценарии отказа.
GOTO_TIMEOUT_REASON = "goto_timeout"
NO_LISTINGS_REASON = "no_listings_selector"
#: Признаки выдачи в DOM (FX-04).
LISTING_SELECTOR = '[data-bem*="offer"], .___1h2y, div[class*="ListingItem"]'

#: Коробка передач (как в ``spec.transmissions``) -> значение параметра URL.
TRANSMISSION_PARAMS = {
    "АКПП": "AUTOMATIC",
    "МКПП": "MECHANICAL",
    "Вариатор": "CVT",
    "Робот": "AMT",
}
#: Топливо (как в ``spec.fuels``) -> значение параметра URL.
FUEL_PARAMS = {
    "Бензин": "PETROL",
    "Дизель": "DIESEL",
    "Гибрид": "HYBRID",
    "Электро": "ELECTRO",
}
#: Имя параметра URL для типа топлива (не подтверждено, см. docstring модуля).
FUEL_PARAM_NAME = "fuel_type"

#: Значения из JSON/DOM -> единые русские названия ``Ad``.
TRANSMISSION_LABELS = {
    "AUTOMATIC": "АКПП", "MECHANICAL": "МКПП", "VARIATOR": "Вариатор",
    "CVT": "Вариатор", "ROBOT": "Робот", "AMT": "Робот",
}
FUEL_LABELS = {
    "GASOLINE": "Бензин", "PETROL": "Бензин", "DIESEL": "Дизель",
    "HYBRID": "Гибрид", "ELECTRO": "Электро", "LPG": "Газ",
}

CAPTCHA_URL_MARKERS = ("showcaptcha", "smartcaptcha")
CAPTCHA_HTML_MARKERS = ("smartcaptcha", "showcaptcha")
EMPTY_MARKERS = ("Ничего не найдено", "Объявления не найдены")
BLOCKED_STATUSES = (403, 429)

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _slug(value: str) -> str:
    """Slug для пути URL: нижний регистр, пробелы -> ``_``, экранирование."""
    return quote(value.strip().lower().replace(" ", "_"), safe="_-")


def _to_int(value: Any) -> Optional[int]:
    """Целое из числа или строки вида ``"1 250 000 ₽"``; иначе ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"\D", "", value)
        return int(digits) if digits else None
    return None


def _walk_offers(node: Any) -> Iterator[dict]:
    """Рекурсивно отдаёт словари, похожие на объявление auto.ru."""
    if isinstance(node, dict):
        if "id" in node and ("price_info" in node or "vehicle_info" in node):
            yield node
        else:
            for value in node.values():
                yield from _walk_offers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_offers(item)


class AutoRuParser(BaseSiteParser):
    """Парсер выдачи auto.ru на Playwright (см. исследование в docstring модуля)."""

    site_name = "auto.ru"

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
        """Собирает одну страницу выдачи auto.ru.

        Args:
            spec: спецификация задания.
            options: опции задания.
            page: номер страницы, с 1.
            stop_event: событие остановки (проверяется до и после загрузки).

        Returns:
            list[Ad]: объявления страницы; ``[]`` — пустая выдача или остановка.

        Raises:
            CaptchaDetectedError: CAPTCHA в URL (в т.ч. после таймаута
                навигации) или в HTML (без обхода).
            BlockedError: HTTP 403/429 (``detail="HTTP <код>"``), таймаут
                навигации вне CAPTCHA (``detail="goto_timeout"``) или нет
                признаков выдачи за 5 с (``detail="no_listings_selector"``).
            ParserError: нет ответа на запрос.
        """
        self._check_simulation(options)
        if options.get("test_fast_mode"):
            return super().fetch_page(spec, options, page, stop_event)
        if stop_event.is_set():
            return []

        url = self._build_url(spec, options, page)
        logger.info("[auto.ru] страница %d: %s", page, url)
        with PlaywrightBrowser(
            headless=self._headless, ignore_https_errors=self._ignore_ssl
        ) as browser:
            tab = browser.new_page()
            # FX-01: "commit" возвращается по заголовкам ответа. DOMContentLoaded
            # на антибот-заглушке не наступает, и 30 с уходили впустую.
            response = None
            goto_error: Optional[PlaywrightTimeoutError] = None
            try:
                response = tab.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="commit")
            except PlaywrightTimeoutError as exc:
                goto_error = exc

            # FX-02/FX-06: URL проверяем сразу, в том числе после таймаута.
            current_url = tab.url or ""
            response_url = (getattr(response, "url", "") or "") if response else ""
            for candidate in (current_url, response_url):
                if self._is_captcha_url(candidate):
                    raise CaptchaDetectedError(self.site_name, candidate)
            if goto_error is not None:  # FX-03: таймаут не на капче — сайт завис
                raise BlockedError(self.site_name, GOTO_TIMEOUT_REASON)
            if stop_event.is_set():
                return []
            if response is None:
                raise ParserError(f"auto.ru: no response for {url}")

            if response.status in BLOCKED_STATUSES:  # FX-07
                # Страница отказа может быть самой капчей — сначала смотрим HTML.
                self._raise_if_captcha_html(tab.content(), current_url or url)
                raise BlockedError(self.site_name, f"HTTP {response.status}")

            try:  # FX-04: выдача должна появиться за короткое время
                tab.wait_for_selector(
                    LISTING_SELECTOR, timeout=SELECTOR_TIMEOUT_MS, state="attached"
                )
            except PlaywrightTimeoutError:
                html = tab.content()
                logger.info("[auto.ru] финальный URL: %s, html_len=%d", current_url, len(html))
                self._raise_if_captcha_html(html, current_url or url)
                if any(m in html for m in EMPTY_MARKERS):
                    self._report_empty(html, current_url, page)
                    return []
                raise BlockedError(self.site_name, NO_LISTINGS_REASON)
            html = tab.content()
            logger.info("[auto.ru] финальный URL: %s, html_len=%d", current_url, len(html))

        # FX-07: капча в HTML (в т.ч. во фрейме), пустая выдача, разбор.
        self._raise_if_captcha_html(html, current_url or url)
        if any(m in html for m in EMPTY_MARKERS):
            self._report_empty(html, current_url, page)
            return []
        ads = self._parse_listings(html, spec)
        if not ads:
            self._report_empty(html, current_url, page)
        return ads

    def _report_empty(self, html: str, url: str, page: int) -> None:
        """Диагностика пустого результата: предупреждение в лог и дамп HTML.

        HTML сохраняется в :data:`DEBUG_DIR` для анализа селекторов; сбой
        записи не должен ронять сбор.

        Args:
            html: HTML страницы, давшей 0 объявлений.
            url: финальный URL страницы.
            page: номер страницы выдачи.
        """
        logger.warning(
            "[auto.ru] парсинг вернул 0 объявлений: url=%s, html_len=%d, "
            "has_next_data=%s, has_offers_key=%s",
            url, len(html), "__NEXT_DATA__" in html, '"offers"' in html,
        )
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            path = DEBUG_DIR / f"autoru_page_{page}_{int(time.time())}.html"
            path.write_text(html, encoding="utf-8")
            logger.warning("[auto.ru] HTML сохранён: %s", path)
        except OSError:
            logger.warning("[auto.ru] не удалось сохранить HTML для анализа", exc_info=True)

    @staticmethod
    def _is_captcha_url(url: str) -> bool:
        """``True``, если URL указывает на страницу CAPTCHA."""
        lowered = url.lower()
        return any(m in lowered for m in CAPTCHA_URL_MARKERS)

    def _raise_if_captcha_html(self, html: str, url: str) -> None:
        """Бросает :class:`CaptchaDetectedError`, если в HTML есть признаки CAPTCHA."""
        lowered = html.lower()
        if any(m in lowered for m in CAPTCHA_HTML_MARKERS):
            raise CaptchaDetectedError(self.site_name, url)

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
        """No-op: браузер создаётся и закрывается в каждом ``fetch_page`` (4.8)."""

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------
    def _build_url(self, spec: dict, options: dict, page: int) -> str:
        """Строит URL выдачи по фильтрам ``spec`` (см. исследование в docstring модуля).

        Args:
            spec: спецификация (``region`` — slug региона, по умолчанию
                ``moskva``; ``manufacturer``, ``model``, ``year_from``,
                ``year_to``, ``transmissions``, ``fuels``; необязательные
                ``manufacturer_slug``/``model_slug`` переопределяют slug).
            options: опции задания (не используются, зарезервировано).
            page: номер страницы, с 1.

        Returns:
            str: готовый URL.
        """
        region = _slug(str(spec.get("region") or DEFAULT_REGION))
        make = spec.get("manufacturer_slug") or _slug(str(spec.get("manufacturer", "")))
        model = spec.get("model_slug") or _slug(str(spec.get("model", "")))
        params: list[tuple[str, Any]] = []
        if spec.get("year_from"):
            params.append(("year_from", spec["year_from"]))
        if spec.get("year_to"):
            params.append(("year_to", spec["year_to"]))
        for name in spec.get("transmissions") or []:
            if name in TRANSMISSION_PARAMS:
                params.append(("transmission", TRANSMISSION_PARAMS[name]))
        for name in spec.get("fuels") or []:
            if name in FUEL_PARAMS:
                params.append((FUEL_PARAM_NAME, FUEL_PARAMS[name]))
        params.append(("page", page))
        return f"{BASE_URL}/{region}/cars/{make}/{model}/all/?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Разбор выдачи
    # ------------------------------------------------------------------
    def _parse_listings(self, html: str, spec: dict) -> list[Ad]:
        """Извлекает объявления: сначала ``__NEXT_DATA__``, затем DOM.

        Args:
            html: HTML страницы выдачи.
            spec: спецификация (марка/модель — запасные значения полей).

        Returns:
            list[Ad]: объявления без дублей по ``url``.
        """
        ads = self._parse_next_data(html, spec)
        if not ads:
            ads = self._parse_dom(html, spec)
        unique: dict[str, Ad] = {}
        for ad in ads:
            unique.setdefault(ad.url, ad)
        return list(unique.values())

    def _parse_next_data(self, html: str, spec: dict) -> list[Ad]:
        """Объявления из JSON ``__NEXT_DATA__`` (приоритет 1)."""
        match = _NEXT_DATA_RE.search(html)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except ValueError:
            logger.warning("[auto.ru] __NEXT_DATA__ не разобран как JSON")
            return []
        ads = []
        for offer in _walk_offers(data):
            ad = self._offer_to_ad(offer, spec)
            if ad is not None:
                ads.append(ad)
        return ads

    def _offer_to_ad(self, offer: dict, spec: dict) -> Optional[Ad]:
        """Преобразует JSON-объявление в :class:`Ad` (``None`` — нет id)."""
        offer_id = offer.get("id")
        if not offer_id:
            return None
        vehicle = offer.get("vehicle_info") or {}
        mark = vehicle.get("mark_info") or {}
        model = vehicle.get("model_info") or {}
        tech = vehicle.get("tech_param") or {}
        price_info = offer.get("price_info") or {}
        displacement = _to_int(tech.get("displacement"))
        url = offer.get("url") or (
            f"{BASE_URL}/cars/used/sale/"
            f"{str(mark.get('code') or spec.get('manufacturer', '')).lower()}/"
            f"{str(model.get('code') or spec.get('model', '')).lower()}/{offer_id}/"
        )
        transmission = str(tech.get("transmission") or "").upper()
        fuel = str(tech.get("engine_type") or "").upper()
        return Ad(
            url=url,
            price=_to_int(price_info.get("RUR") or price_info.get("price")),
            year=_to_int((offer.get("documents") or {}).get("year")),
            manufacturer=mark.get("name") or spec.get("manufacturer"),
            model=model.get("name") or spec.get("model"),
            transmission=TRANSMISSION_LABELS.get(transmission) or tech.get("transmission"),
            fuel=FUEL_LABELS.get(fuel) or tech.get("engine_type"),
            engine_volume=(
                round(displacement / 1000, 1) if displacement is not None else None
            ),
            mileage=_to_int((offer.get("state") or {}).get("mileage")),
            raw_json={
                "source": "auto.ru",
                "id": offer_id,
                "price_info": price_info,
                "documents": offer.get("documents"),
                "mileage": (offer.get("state") or {}).get("mileage"),
                "tech_param": tech,
            },
        )

    def _parse_dom(self, html: str, spec: dict) -> list[Ad]:
        """Объявления из DOM-блоков ``ListingItem`` (запасной вариант)."""
        blocks = re.split(r'(?=<div[^>]*class="ListingItem[\s"])', html)[1:]
        ads: list[Ad] = []
        for block in blocks:
            link = re.search(
                r'<a[^>]*class="[^"]*ListingItemTitle__link[^"]*"[^>]*href="([^"]+)"', block
            ) or re.search(
                r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*ListingItemTitle__link', block
            )
            if not link:
                continue
            url = html_lib.unescape(link.group(1))
            if url.startswith("/"):
                url = BASE_URL + url
            text = self._text(block)
            ads.append(Ad(
                url=url,
                price=self._dom_int(block, "ListingItem__price"),
                year=self._dom_int(block, "ListingItem__year"),
                manufacturer=spec.get("manufacturer"),
                model=spec.get("model"),
                transmission=self._dom_label(text, TRANSMISSION_DOM_LABELS),
                fuel=self._dom_label(text, FUEL_DOM_LABELS),
                engine_volume=self._dom_volume(text),
                mileage=self._dom_int(block, "ListingItem__kmAge"),
                raw_json={"source": "auto.ru", "via": "dom"},
            ))
        return ads

    @staticmethod
    def _text(fragment: str) -> str:
        """Текст фрагмента без тегов."""
        return html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))

    @staticmethod
    def _dom_int(block: str, css_class: str) -> Optional[int]:
        """Целое из элемента с классом ``css_class`` (цена, год, пробег)."""
        match = re.search(rf'class="[^"]*{css_class}[^"]*"[^>]*>(.*?)</', block, re.DOTALL)
        return _to_int(re.sub(r"<[^>]+>", "", match.group(1))) if match else None

    @staticmethod
    def _dom_label(text: str, labels: dict[str, str]) -> Optional[str]:
        """Первое найденное в тексте слово из словаря ``признак -> название``."""
        lowered = text.lower()
        for needle, label in labels.items():
            if needle in lowered:
                return label
        return None

    @staticmethod
    def _dom_volume(text: str) -> Optional[float]:
        """Объём двигателя из текста вида ``2.0 л``."""
        match = re.search(r"(\d[.,]\d)\s*л\b", text)
        return float(match.group(1).replace(",", ".")) if match else None


TRANSMISSION_DOM_LABELS = {
    "автомат": "АКПП", "механика": "МКПП", "вариатор": "Вариатор", "робот": "Робот",
}
FUEL_DOM_LABELS = {
    "бензин": "Бензин", "дизель": "Дизель", "гибрид": "Гибрид", "электро": "Электро",
}
