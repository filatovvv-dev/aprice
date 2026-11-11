"""Заглушка парсера bibika.ru (подзадача 3.3).

Реальная реализация на Playwright появится в подзадаче 3.10. Структура
выдачи bibika.ru будет исследована там же; сейчас класс — обычная
заглушка без специфики.
"""

from __future__ import annotations

import logging
from threading import Event
from typing import Optional

from server.parsers.base import BaseSiteParser, PageCallback, ProgressCallback

logger = logging.getLogger("aprice.server.parsers.bibika")


class BibikaParser(BaseSiteParser):
    """Заглушка парсера каталога выдач bibika.ru."""

    site_name = "bibika.ru"

    def collect(
        self,
        spec: dict,
        options: dict,
        stop_event: Event,
        progress_cb: Optional[ProgressCallback] = None,
        on_page: Optional[PageCallback] = None,
    ) -> list:
        """Эмулирует сбор объявлений с bibika.ru.

        Args:
            spec: спецификация задания из IPC.
            options: опции задания из IPC; поддерживает ключи
                ``test_fast_mode``, ``simulate_captcha``, ``simulate_block``,
                ``max_pages_per_site``, ``max_ads_per_site``.
            stop_event: событие остановки (S-12).
            progress_cb: колбэк прогресса ``(page, ads_collected)``.
            on_page: колбэк постраничной отдачи объявлений (S-06).

        Returns:
            list: собранные (фейковые) объявления :class:`Ad`.

        Raises:
            CaptchaDetectedError: ``options["simulate_captcha"] == "bibika.ru"``.
            BlockedError: ``options["simulate_block"] == "bibika.ru"``.
        """
        logger.debug("[bibika.ru] старт сбора (заглушка)")
        return self._collect_stub(
            spec, options, stop_event, progress_cb, on_page
        )
