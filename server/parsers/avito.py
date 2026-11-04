"""Заглушка парсера avito.ru (подзадача 3.3).

Реальная реализация на Playwright появится в подзадаче 3.9. Известно:
avito применяет поведенческий анализ и отвечает 403 при подозрительной
активности — заглушка эмулирует блокировку через
``options["simulate_block"] == "avito.ru"``.
"""

from __future__ import annotations

import logging
from threading import Event
from typing import Optional

from server.parsers.base import BaseSiteParser, ProgressCallback

logger = logging.getLogger("aprice.server.parsers.avito")


class AvitoParser(BaseSiteParser):
    """Заглушка парсера каталога выдач avito.ru."""

    site_name = "avito.ru"

    def collect(
        self,
        spec: dict,
        options: dict,
        stop_event: Event,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> list:
        """Эмулирует сбор объявлений с avito.ru.

        Args:
            spec: спецификация задания из IPC.
            options: опции задания из IPC; поддерживает ключи
                ``test_fast_mode``, ``simulate_captcha``, ``simulate_block``,
                ``max_pages_per_site``, ``max_ads_per_site``.
            stop_event: событие остановки (S-12).
            progress_cb: колбэк прогресса ``(page, ads_collected)``.

        Returns:
            list: собранные (фейковые) объявления :class:`Ad`.

        Raises:
            CaptchaDetectedError: ``options["simulate_captcha"] == "avito.ru"``.
            BlockedError: ``options["simulate_block"] == "avito.ru"``.
        """
        logger.debug("[avito.ru] старт сбора (заглушка)")
        return self._collect_stub(spec, options, stop_event, progress_cb)
