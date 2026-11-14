"""Общая обёртка над Playwright Chromium для парсеров APrice-Server (3.7).

:class:`PlaywrightBrowser` собирает в одном месте настройки, общие для всех
реальных парсеров (auto.ru, drom.ru, avito.ru, bibika.ru):

* BR-01 — Chromium, режим ``headless`` из ``server.playwright_headless``;
* BR-02 — ``ignore_https_errors`` из ``server.ignore_https_errors``
  (MITM-прокси в сети Заказчика подменяет сертификаты);
* BR-03 — stealth-скрипт: ``navigator.webdriver``, ``plugins``,
  ``languages``, ``window.chrome``;
* BR-04 — User-Agent Chrome 124 / Windows 10 и согласованные ``sec-ch-ua*``;
* BR-05 — ``ru-RU``, ``Europe/Moscow``, окно 1920×1080;
* BR-06 — ``route.abort()`` для ``image``/``media``/``font``/``stylesheet``;
* BR-07 — настройки берутся из :class:`~common.config.Config`
  (см. :meth:`PlaywrightBrowser.from_config`), без хардкода в парсерах.

Браузер создаётся и закрывается контекстным менеджером; Playwright
**синхронный** и должен использоваться в одном потоке — это рабочий поток
планировщика ``TaskManager`` (``fetch_page`` вызывается из него).

Модуль не пытается обходить CAPTCHA: stealth-настройки лишь делают
автоматизированный браузер менее «шумным».
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from common.config import Config

try:  # Playwright ставится отдельно (requirements.txt + ``playwright install``).
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - зависит от окружения
    sync_playwright = None  # type: ignore[assignment]

logger = logging.getLogger("aprice.server.parsers.browser")

#: User-Agent Chrome 124 на Windows 10 (BR-04).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: Заголовки client hints, согласованные с :data:`USER_AGENT` (BR-04).
EXTRA_HEADERS = {
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.6,en;q=0.5",
}

LOCALE = "ru-RU"
TIMEZONE_ID = "Europe/Moscow"
VIEWPORT = {"width": 1920, "height": 1080}

#: Типы ресурсов, которые не нужны для разбора выдачи (BR-06, S-18).
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

#: Аргументы запуска Chromium: убирает флаг автоматизации Blink.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

#: JS, исполняемый до скриптов страницы (BR-03).
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
if (!window.chrome) { window.chrome = {runtime: {}}; }
"""


class PlaywrightBrowser:
    """Chromium под управлением Playwright с настройками APrice.

    Пример:
        >>> with PlaywrightBrowser(headless=True) as browser:
        ...     page = browser.new_page()
        ...     page.goto("https://example.com")
    """

    def __init__(self, headless: bool = True, ignore_https_errors: bool = True) -> None:
        """Сохраняет настройки; браузер запускается в :meth:`__enter__`.

        Args:
            headless: ``True`` — без окна (BR-01).
            ignore_https_errors: игнорировать ошибки сертификатов (BR-02).
        """
        self._headless = headless
        self._ignore_https_errors = ignore_https_errors
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None

    @classmethod
    def from_config(cls, config: Config) -> "PlaywrightBrowser":
        """Создаёт браузер по ``server.playwright_headless`` / ``server.ignore_https_errors``.

        Args:
            config: загруженная конфигурация (BR-07).
        """
        return cls(
            headless=bool(config.get("server", "playwright_headless", True)),
            ignore_https_errors=bool(config.get("server", "ignore_https_errors", True)),
        )

    # ------------------------------------------------------------------
    def __enter__(self) -> "PlaywrightBrowser":
        """Запускает Chromium и создаёт контекст с настройками BR-02..BR-06.

        Raises:
            RuntimeError: Playwright не установлен.
        """
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright не установлен: выполните "
                "'pip install playwright && playwright install chromium'"
            )
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                headless=self._headless, args=LAUNCH_ARGS
            )
            self._context = self._browser.new_context(
                ignore_https_errors=self._ignore_https_errors,
                user_agent=USER_AGENT,
                locale=LOCALE,
                timezone_id=TIMEZONE_ID,
                viewport=dict(VIEWPORT),
                extra_http_headers=dict(EXTRA_HEADERS),
            )
            self._context.add_init_script(STEALTH_SCRIPT)
            self._context.route("**/*", self._route_handler)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args: Any) -> None:
        """Закрывает контекст, браузер и Playwright; ошибки закрытия только логируются."""
        for name, closer in (
            ("context", self._context),
            ("browser", self._browser),
        ):
            if closer is not None:
                try:
                    closer.close()
                except Exception:  # noqa: BLE001 - закрытие не должно маскировать ошибку
                    logger.debug("Не удалось закрыть %s", name, exc_info=True)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось остановить Playwright", exc_info=True)
        self._context = self._browser = self._playwright = None

    # ------------------------------------------------------------------
    def new_page(self) -> Any:
        """Открывает новую страницу (stealth и route-abort уже на контексте).

        Raises:
            RuntimeError: браузер не запущен (вызов вне ``with``).
        """
        if self._context is None:
            raise RuntimeError("PlaywrightBrowser не запущен: используйте 'with'")
        return self._context.new_page()

    @staticmethod
    def _route_handler(route: Any) -> None:
        """Отменяет запросы тяжёлых ресурсов (BR-06), остальные пропускает."""
        request: Optional[Any] = getattr(route, "request", None)
        if request is not None and request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
