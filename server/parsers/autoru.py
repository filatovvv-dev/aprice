"""Парсер выдачи auto.ru (подзадача 3.7).

Реальная реализация на Playwright (:class:`~server.parsers.browser.PlaywrightBrowser`).
Планировщик ``TaskManager`` вызывает :meth:`AutoRuParser.fetch_page` — **одну**
страницу выдачи за вызов; паузы между запросами выдерживает он сам.

При ``use_stub_data`` (и ``simulate_captcha`` / ``simulate_block``) парсер ведёт
себя как заглушка 3.3: автотесты не ходят в сеть. ``test_fast_mode`` только
сокращает паузы.

Исследование структуры auto.ru
------------------------------
Структура подтверждена **ручным анализом реальной страницы** (смоук-тест 3.7:
HTML загружается, ``__NEXT_DATA__`` в нём нет). В среде разработки auto.ru
недоступен, поэтому код проверен только на моках и фрагментах HTML; финальная
проверка — смоук на Windows.

* **URL:** ``https://auto.ru/{регион}/cars/{марка}/{модель}/all/?year_from=..&year_to=..
  &transmission=..&fuel_type=..&page=N``. Значения ``transmission``
  (``AUTOMATIC``/``MECHANICAL``/``CVT``/``AMT``) и ``fuel_type``
  (``PETROL``/``DIESEL``/``HYBRID``/``ELECTRO``) — по промпту 3.7, **не
  подтверждены** (см. :data:`TRANSMISSION_PARAMS`, :data:`FUEL_PARAMS`,
  :data:`FUEL_PARAM_NAME`).
* **Выдача:** карточки — прямые потомки ``div.ListingCars__items``
  (:data:`CARDS_SELECTOR`); контейнер появляется после React-рендеринга,
  поэтому после ``goto(wait_until="commit")`` ждём селектор до 10 с.
* **Поля карточки** берутся из её видимого текста (``inner_text``) регулярными
  выражениями — они устойчивее динамических CSS-классов: цена — число перед
  ``₽``, пробег — перед ``км``, год — ``19xx``/``20xx``, объём — ``N.N л``,
  коробка и топливо — по ключевым словам. Ссылка — ``href`` первого ``<a>``.
  Марка и модель берутся из ``spec``. Весь текст карточки сохраняется в
  ``raw_json["text"]`` для отладки.
* ``__NEXT_DATA__`` и ``app-config`` не используются (объявлений в них нет).
* Поле, которое не нашлось, равно ``None``; разбор не падает.

CAPTCHA не обходится: ``showcaptcha`` в URL/HTML или ``smartcaptcha`` в HTML
→ :class:`CaptchaDetectedError`; HTTP 403/429 → :class:`BlockedError`.
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
LISTINGS_TIMEOUT_MS = 10_000
#: Причины :class:`BlockedError` (в ``detail``) — различают сценарии отказа.
GOTO_TIMEOUT_REASON = "goto_timeout"
NO_LISTINGS_REASON = "no_listings_selector"
#: Карточки объявлений: прямые потомки контейнера выдачи.
CARDS_SELECTOR = "div.ListingCars__items > div"

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

CAPTCHA_URL_MARKERS = ("showcaptcha", "smartcaptcha")
CAPTCHA_HTML_MARKERS = ("smartcaptcha", "showcaptcha")
EMPTY_MARKERS = ("Ничего не найдено", "Объявления не найдены")
BLOCKED_STATUSES = (403, 429)


def _slug(value: str) -> str:
    """Slug для пути URL: нижний регистр, пробелы -> ``_``, экранирование."""
    return quote(value.strip().lower().replace(" ", "_"), safe="_-")


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
                карточек выдачи за 10 с (``detail="no_listings_selector"``).
            ParserError: нет ответа на запрос.
        """
        self._check_simulation(options)
        if self._use_stub(options):
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

            try:  # карточки рисует React уже после commit — ждём контейнер выдачи
                tab.wait_for_selector(
                    CARDS_SELECTOR, state="attached", timeout=LISTINGS_TIMEOUT_MS
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
            ads = self._parse_listings_from_page(tab, spec)  # пока вкладка открыта

        # FX-07: капча в HTML (в т.ч. во фрейме), пустая выдача, разбор.
        self._raise_if_captcha_html(html, current_url or url)
        if any(m in html for m in EMPTY_MARKERS):
            self._report_empty(html, current_url, page)
            return []
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
    def _parse_listings_from_page(self, tab: Any, spec: dict) -> list[Ad]:
        """Извлекает объявления из карточек ``div.ListingCars__items > div``.

        Один вызов в браузер забирает у каждой карточки ``href`` первой ссылки
        и видимый текст; дальше разбор идёт регулярными выражениями
        (:meth:`_card_to_ad`). Карточки без ссылки и без цены, года и пробега
        (баннеры, заглушки загрузки) пропускаются; год вне ``[year_from, year_to]`` из
        ``spec`` отбрасывается (страховка от игнорирования фильтра URL; год ``None``
        проходит).

        Args:
            tab: открытая страница Playwright.
            spec: спецификация (источник марки и модели).

        Returns:
            list[Ad]: объявления без дублей по ``url``; ``[]`` — карточек нет.
        """
        cards = tab.locator(CARDS_SELECTOR).evaluate_all(CARDS_SCRIPT) or []
        unique: dict[str, Ad] = {}
        out_of_range = 0
        for card in cards:
            ad = self._card_to_ad(card.get("href"), card.get("text") or "", spec)
            if ad is None:
                continue
            if not self._year_in_range(ad.year, spec):
                out_of_range += 1
                logger.debug("[auto.ru] год %s вне диапазона, пропущено: %s", ad.year, ad.url)
                continue
            unique.setdefault(ad.url, ad)
        if out_of_range and not unique:
            self._warn_all_out_of_range(out_of_range, spec)
        return list(unique.values())

    def _card_to_ad(self, href: Optional[str], text: str, spec: dict) -> Optional[Ad]:
        """Собирает :class:`Ad` из ссылки и текста карточки.

        Args:
            href: ``href`` первой ссылки карточки (``None`` — ссылки нет).
            text: видимый текст карточки.
            spec: спецификация (марка и модель).

        Returns:
            Optional[Ad]: объявление или ``None``, если карточка не похожа
            на объявление.
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
            transmission=self._find_label(text, TRANSMISSION_DOM_LABELS),
            fuel=self._find_label(text, FUEL_DOM_LABELS),
            engine_volume=self._extract_volume(text),
            mileage=mileage,
            raw_json={"source": "auto.ru", "text": text},
        )

    @staticmethod
    def _extract_price(text: str) -> Optional[int]:
        """Цена: число перед ``₽`` (``"7 700 000 ₽"`` -> ``7700000``)."""
        match = _PRICE_RE.search(text)
        return int(re.sub(r"\D", "", match.group(1))) if match else None

    @staticmethod
    def _extract_mileage(text: str) -> Optional[int]:
        """Пробег: число перед ``км`` (``"81 000 км"`` -> ``81000``)."""
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


#: Число с пробелами-разделителями тысяч (группы строго по 3 цифры, без перевода
#: строки, начало — не посреди числа), чтобы год или другое число на соседней
#: строке не «слипались» с суммой или пробегом.
_NUMBER = r"(?<!\d)(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)"
_PRICE_RE = re.compile(_NUMBER + r"\s*₽")
_MILEAGE_RE = re.compile(_NUMBER + r"\s*км", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")
_VOLUME_RE = re.compile(r"(\d[.,]\d)\s*л(?![а-яё])", re.IGNORECASE)

#: JS для ``Locator.evaluate_all``: ``href`` первой ссылки и текст каждой карточки.
CARDS_SCRIPT = (
    "els => els.map(e => ({"
    "href: (e.querySelector('a') || {}).href || null, "
    "text: e.innerText || ''}))"
)

TRANSMISSION_DOM_LABELS = {
    "акпп": "АКПП", "автомат": "АКПП",
    "мкпп": "МКПП", "механик": "МКПП", "механич": "МКПП",
    "вариатор": "Вариатор", "робот": "Робот",
}
FUEL_DOM_LABELS = {
    "бензин": "Бензин", "дизель": "Дизель", "гибрид": "Гибрид", "электро": "Электро",
}
