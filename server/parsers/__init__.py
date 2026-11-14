"""Пакет парсеров площадок APrice-Server (подзадача 3.3).

Содержит абстракцию :class:`BaseSiteParser`, внутренний формат
:class:`Ad`, иерархию исключений и фабрику :func:`get_parser`.

Фабрика знает три поддерживаемые площадки (auto.ru, drom.ru, avito.ru).
SberAuto (S-15) и bibika.ru (отключена решением Заказчика) не поддерживаются —
для них фабрика бросает :class:`UnsupportedSiteError`; отдельных модулей нет.
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
from server.parsers.drom import DromParser

logger = logging.getLogger("aprice.server.parsers")

#: Реестр «домен -> класс парсера» для фабрики.
PARSER_REGISTRY: dict[str, type[BaseSiteParser]] = {
    AutoRuParser.site_name: AutoRuParser,
    DromParser.site_name: DromParser,
    AvitoParser.site_name: AvitoParser,
}

#: Площадки, явно не поддерживаемые: sberauto.com (S-15) и bibika.ru (отключена
#: решением Заказчика).
UNSUPPORTED_SITES: frozenset[str] = frozenset({"sberauto.com", "bibika.ru"})


def get_parser(site_name: str, config: Config) -> BaseSiteParser:
    """Возвращает экземпляр парсера для площадки.

    Args:
        site_name: домен площадки (``"auto.ru"``, ``"drom.ru"``,
            ``"avito.ru"``).
        config: конфигурация приложения, передаваемая парсеру.

    Returns:
        BaseSiteParser: готовый к использованию парсер.

    Raises:
        UnsupportedSiteError: площадка не поддерживается (в т.ч.
            ``"sberauto.com"``, ``"bibika.ru"``) или неизвестна.
    """
    if site_name in UNSUPPORTED_SITES:
        raise UnsupportedSiteError(
            f"Площадка {site_name!r} не поддерживается (отключена)"
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
    "PARSER_REGISTRY",
    "UNSUPPORTED_SITES",
    "get_parser",
]
