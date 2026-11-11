"""Абстракция парсеров площадок APrice-Server (подзадача 3.3).

Модуль определяет:

* иерархию исключений парсеров (:class:`ParserError` и наследники);
* внутренний формат объявления :class:`Ad` (совместим с
  ``StateStore.save_ads`` через :meth:`Ad.to_dict`);
* абстрактный базовый класс :class:`BaseSiteParser` с общим поведением
  пауз, прерывания по ``stop_event`` и вызова ``progress_cb``.

Заглушки площадок (auto.ru / drom.ru / avito.ru / bibika.ru) эмулируют
реальный сбор без сетевых запросов: паузы реализованы через
``stop_event.wait(timeout)``, поэтому остановка задания действует почти
мгновенно (S-12).
"""

from __future__ import annotations

import json
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields
from threading import Event
from typing import Any, Callable, ClassVar, Optional

from common.config import Config
from common.utils import interruptible_sleep

logger = logging.getLogger("aprice.server.parsers")

#: Callback прогресса: (page, ads_collected) -> None
ProgressCallback = Callable[[int, int], None]


# ----------------------------------------------------------------------
# Исключения (п. 2.1)
# ----------------------------------------------------------------------
class ParserError(Exception):
    """Базовая ошибка парсера площадки."""


class CaptchaDetectedError(ParserError):
    """Обнаружена CAPTCHA (без попыток обхода — принципиальное требование ТЗ).

    Атрибуты:
        site: домен площадки (например ``"auto.ru"``).
        url: адрес страницы CAPTCHA для ручного прохождения.
    """

    def __init__(self, site: str, url: str) -> None:
        """Инициализирует ошибку капчи.

        Args:
            site: домен площадки.
            url: URL страницы CAPTCHA.
        """
        super().__init__(f"CAPTCHA detected on {site}: {url}")
        self.site = site
        self.url = url


class BlockedError(ParserError):
    """Площадка заблокировала доступ (403, «Доступ ограничен»)."""

    def __init__(self, site: str, detail: str = "") -> None:
        """Инициализирует ошибку блокировки.

        Args:
            site: домен площадки.
            detail: подробности (код ответа, текст страницы).
        """
        super().__init__(f"Blocked on {site}: {detail}")
        self.site = site
        self.detail = detail


class UnsupportedSiteError(ParserError):
    """Площадка явно не поддерживается (например, SberAuto в v1 — S-15)."""


# ----------------------------------------------------------------------
# Внутренний формат объявления (п. 2.2)
# ----------------------------------------------------------------------
@dataclass
class Ad:
    """Объявление о продаже автомобиля — внутренний формат парсера.

    Поля соответствуют схеме ``collected_ads`` StateStore (S-08).
    ``url`` обязателен; остальные поля опциональны.
    """

    url: str
    price: Optional[int] = None
    year: Optional[int] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    transmission: Optional[str] = None
    fuel: Optional[str] = None
    engine_volume: Optional[float] = None
    mileage: Optional[int] = None
    raw_json: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        """Возвращает представление для ``StateStore.save_ads``.

        Returns:
            dict: все поля объявления; ``raw_json`` остаётся словарём —
            сериализацию в JSON выполняет StateStore.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ad":
        """Собирает :class:`Ad` из словаря (игнорирует лишние ключи).

        Args:
            data: словарь с полями объявления.

        Returns:
            Ad: экземпляр объявления.

        Raises:
            ValueError: если ``url`` отсутствует или пуст.
        """
        url = data.get("url")
        if not url:
            raise ValueError("Ad requires non-empty 'url'")
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        raw = filtered.get("raw_json")
        if isinstance(raw, str):
            # Из БД (StateStore.load_ads) raw_json приходит JSON-строкой.
            try:
                filtered["raw_json"] = json.loads(raw)
            except (ValueError, TypeError):
                pass
        return cls(**filtered)


# ----------------------------------------------------------------------
# Абстрактный парсер (п. 2.3)
# ----------------------------------------------------------------------
class BaseSiteParser(ABC):
    """Базовый класс парсера одной площадки.

    Потомки задают :attr:`site_name` и переопределяют поведение при
    необходимости; общая логика заглушки (паузы, лимиты, прерывание,
    прогресс) реализована в :meth:`_collect_stub`.
    """

    #: Домен площадки; уникален среди зарегистрированных парсеров.
    site_name: ClassVar[str] = ""

    #: Диапазон паузы между страницами в обычном режиме, сек.
    PAGE_DELAY_SEC: ClassVar[tuple[float, float]] = (1.0, 3.0)

    #: Диапазон паузы между страницами в test_fast_mode, сек.
    FAST_PAGE_DELAY_SEC: ClassVar[tuple[float, float]] = (0.1, 0.3)

    #: Интервал проверки stop_event во время паузы, сек (S-12);
    #: передаётся в :func:`common.utils.interruptible_sleep`.
    STOP_CHECK_INTERVAL_SEC: ClassVar[float] = 0.5

    #: Диапазон количества фейковых объявлений на страницу у заглушек.
    ADS_PER_PAGE_RANGE: ClassVar[tuple[int, int]] = (20, 50)

    def __init__(self, config: Config) -> None:
        """Инициализирует парсер.

        Args:
            config: конфигурация приложения (используется секция ``server``).
        """
        self._config = config

    # -------------------------- публичный API -------------------------
    @abstractmethod
    def collect(
        self,
        spec: dict,
        options: dict,
        stop_event: Event,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> list[Ad]:
        """Собирает объявления по спецификации.

        Args:
            spec: спецификация задания (mode, manufacturer, model, годы...).
            options: опции задания (max_pages_per_site, max_ads_per_site,
                test_fast_mode, simulate_captcha, simulate_block...).
            stop_event: событие остановки — проверять каждые 0.5 с (S-12).
            progress_cb: вызывается после каждой страницы: ``(page, ads)``.

        Returns:
            list[Ad]: собранные объявления (при остановке — частичные).

        Raises:
            CaptchaDetectedError: обнаружена CAPTCHA (без попыток обхода).
            BlockedError: площадка заблокировала доступ.
        """

    # ------------------------ хелперы потомков ------------------------
    def _page_delays(self, options: dict) -> tuple[float, float]:
        """Возвращает диапазон паузы между страницами.

        Args:
            options: опции задания; флаг ``test_fast_mode`` сокращает паузы.

        Returns:
            tuple[float, float]: ``(min_sec, max_sec)``.
        """
        if options.get("test_fast_mode"):
            return self.FAST_PAGE_DELAY_SEC
        return self.PAGE_DELAY_SEC

    def _random_ad(self, spec: dict, index: int) -> Ad:
        """Генерирует одно реалистичное фейковое объявление (для заглушек).

        Args:
            spec: спецификация задания (марка/модель/годы берутся из него).
            index: порядковый номер объявления внутри страницы (для url).

        Returns:
            Ad: сгенерированное объявление.
        """
        transmissions = ["МКПП", "АКПП", "Вариатор", "Робот", "DSG"]
        fuels = ["Бензин", "Дизель", "Гибрид", "Электро"]
        year_from = int(spec.get("year_from") or 2010)
        year_to = int(spec.get("year_to") or 2024)
        if year_to < year_from:
            year_to = year_from
        year = random.randint(year_from, year_to)
        return Ad(
            url=(
                f"https://{self.site_name}/cars/"
                f"{index}-{random.randint(10**6, 10**7 - 1)}"
            ),
            price=random.randint(500_000, 5_000_000),
            year=year,
            manufacturer=spec.get("manufacturer", ""),
            model=spec.get("model", ""),
            transmission=random.choice(transmissions),
            fuel=random.choice(fuels),
            engine_volume=round(random.uniform(1.0, 5.0), 1),
            mileage=random.randint(0, 250_000),
            raw_json={"source": "stub", "site": self.site_name, "index": index},
        )

    def _check_simulation(self, options: dict) -> None:
        """Эмуляция CAPTCHA/блокировки по флагам ``options`` (для тестов 3.4).

        Args:
            options: опции задания; ключи ``simulate_captcha`` /
                ``simulate_block`` содержат имя площадки-мишени.

        Raises:
            CaptchaDetectedError: задана эмуляция капчи для этой площадки.
            BlockedError: задана эмуляция блокировки для этой площадки.
        """
        captcha_site = options.get("simulate_captcha")
        if captcha_site == self.site_name:
            raise CaptchaDetectedError(
                self.site_name,
                f"https://{self.site_name}/showcaptcha?redirect=1",
            )
        block_site = options.get("simulate_block")
        if block_site == self.site_name:
            raise BlockedError(
                self.site_name, "HTTP 403, страница «Доступ ограничен»"
            )

    def _collect_stub(
        self,
        spec: dict,
        options: dict,
        stop_event: Event,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> list[Ad]:
        """Общий алгоритм заглушки: страницы → паузы → прогресс → лимиты.

        Используется всеми четырьмя заглушками площадок. Поведение
        имитирует реальный парсер: сначала проверка симуляции ошибок,
        затем последовательный обход «страниц» выдачи с прерываемыми
        паузами и соблюдением лимитов S-17.

        Args:
            spec: спецификация задания.
            options: опции задания.
            stop_event: событие остановки.
            progress_cb: колбэк прогресса ``(page, ads)``.

        Returns:
            list[Ad]: собранные объявления (частичные при остановке).

        Raises:
            CaptchaDetectedError: эмуляция капчи активна для площадки.
            BlockedError: эмуляция блокировки активна для площадки.
        """
        self._check_simulation(options)

        max_pages = int(options.get("max_pages_per_site") or 10)
        max_ads = int(options.get("max_ads_per_site") or 500)
        delay_min, delay_max = self._page_delays(options)

        ads: list[Ad] = []
        page = 0
        while page < max_pages and len(ads) < max_ads:
            if stop_event.is_set():
                logger.info(
                    "[%s] остановлено до страницы %d", self.site_name, page + 1
                )
                break

            page += 1
            per_page = random.randint(*self.ADS_PER_PAGE_RANGE)
            per_page = min(per_page, max_ads - len(ads))
            batch = [self._random_ad(spec, i) for i in range(per_page)]
            ads.extend(batch)

            logger.info(
                "[%s] страница %d/%d, собрано %d объявлений",
                self.site_name, page, max_pages, len(ads),
            )
            if progress_cb is not None:
                progress_cb(page, len(ads))

            if page >= max_pages or len(ads) >= max_ads:
                break

            if not interruptible_sleep(
                stop_event,
                random.uniform(delay_min, delay_max),
                check_interval=self.STOP_CHECK_INTERVAL_SEC,
            ):
                logger.info("[%s] пауза прервана остановкой", self.site_name)
                break

        return ads
