"""Пакет парсеров площадок APrice-Server (подзадача 3.3).

Содержит абстракцию :class:`BaseSiteParser`, внутренний формат
:class:`Ad`, иерархию исключений и фабрику :func:`get_parser`.

Фабрика знает четыре поддерживаемые площадки (auto.ru, drom.ru,
avito.ru, bibika.ru). SberAuto в v1 не поддерживается (S-15) — для него
фабрика бросает :class:`UnsupportedSiteError`; отдельного модуля нет.
"""

from __future__ import annotations

import logging

from common.config import Config
from server.parsers.avito import AvitoParser
from server.parsers.autoru import AutoRuParser
from server.parsers.base import (
    Ad,
    BaseSiteParser,
    BlockedError,
    CaptchaDetectedError,
    PageCallback,
    ParserError,
    ProgressCallback,
    UnsupportedSiteError,
)
from server.parsers.bibika import BibikaParser
from server.parsers.drom import DromParser

logger = logging.getLogger("aprice.server.parsers")

#: Реестр «домен -> класс парсера» для фабрики.
PARSER_REGISTRY: dict[str, type[BaseSiteParser]] = {
    AutoRuParser.site_name: AutoRuParser,
    DromParser.site_name: DromParser,
    AvitoParser.site_name: AvitoParser,
    BibikaParser.site_name: BibikaParser,
}

#: Площадки, явно не поддерживаемые в v1 (S-15).
UNSUPPORTED_SITES: frozenset[str] = frozenset({"sberauto.com"})


def get_parser(site_name: str, config: Config) -> BaseSiteParser:
    """Возвращает экземпляр парсера для площадки.

    Args:
        site_name: домен площадки (``"auto.ru"``, ``"drom.ru"``,
            ``"avito.ru"``, ``"bibika.ru"``).
        config: конфигурация приложения, передаваемая парсеру.

    Returns:
        BaseSiteParser: готовый к использованию парсер.

    Raises:
        UnsupportedSiteError: площадка не поддерживается (в т.ч.
            ``"sberauto.com"``) или неизвестна.
    """
    if site_name in UNSUPPORTED_SITES:
        raise UnsupportedSiteError(
            f"Площадка {site_name!r} не поддерживается в v1 (S-15)"
        )
    parser_cls = PARSER_REGISTRY.get(site_name)
    if parser_cls is None:
        raise UnsupportedSiteError(f"Неизвестная площадка: {site_name!r}")
    return parser_cls(config)


__all__ = [
    "Ad",
    "BaseSiteParser",
    "BlockedError",
    "CaptchaDetectedError",
    "PageCallback",
    "ParserError",
    "ProgressCallback",
    "UnsupportedSiteError",
    "AutoRuParser",
    "DromParser",
    "AvitoParser",
    "BibikaParser",
    "PARSER_REGISTRY",
    "UNSUPPORTED_SITES",
    "get_parser",
]
