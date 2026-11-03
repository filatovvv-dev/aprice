"""Загрузка и сохранение конфигурации приложения (config/settings.json).

Класс :class:`Config` предоставляет доступ к настройкам по схеме
``секция -> ключ`` с подстановкой значений по умолчанию из
:data:`common.constants.DEFAULT_SETTINGS`.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from common import constants

logger = logging.getLogger(__name__)


class Config:
    """Хранит конфигурацию приложения и умеет читать/писать settings.json.

    Путь к файлу — ``config/settings.json`` относительно корня проекта
    (или каталога ``.exe`` для собранной версии). При отсутствии файла
    создаётся новый со значениями по умолчанию.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Инициализирует конфиг.

        Args:
            path: Необязательный путь к JSON-файлу. По умолчанию —
                :data:`common.constants.SETTINGS_FILE`.
        """
        self._path: Path = path or constants.SETTINGS_FILE
        self._data: dict[str, Any] = copy.deepcopy(constants.DEFAULT_SETTINGS)

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Path: путь к файлу settings.json."""
        return self._path

    @property
    def data(self) -> dict[str, Any]:
        """dict: полная копия текущих настроек."""
        return copy.deepcopy(self._data)

    # ------------------------------------------------------------------
    # Загрузка / сохранение
    # ------------------------------------------------------------------
    def load(self) -> "Config":
        """Загружает настройки из файла; при отсутствии — создаёт файл.

        Недостающие секции/ключи дополняются значениями по умолчанию.

        Returns:
            Config: self (для цепочек вызовов).
        """
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._merge_defaults(loaded)
                    self._data = loaded
                    logger.info("Конфигурация загружена: %s", self._path)
                else:
                    logger.warning(
                        "settings.json не является объектом — использованы значения по умолчанию"
                    )
                    self.save()
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Не удалось прочитать settings.json: %s", exc)
                self.save()
        else:
            logger.info("settings.json не найден — создаю со значениями по умолчанию")
            self.save()
        return self

    def save(self) -> None:
        """Сохраняет текущие настройки в файл (создаёт каталоги при необходимости)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        logger.debug("Конфигурация сохранена: %s", self._path)

    # ------------------------------------------------------------------
    # Доступ к значениям
    # ------------------------------------------------------------------
    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Возвращает значение ``section.key`` или ``default``.

        Args:
            section: Имя секции (например ``"builder"``).
            key: Имя ключа (например ``"cache_ttl_hours"``).
            default: Значение по умолчанию, если пара отсутствует.
        """
        sec = self._data.get(section)
        if isinstance(sec, dict) and key in sec:
            return sec[key]
        dflt_sec = constants.DEFAULT_SETTINGS.get(section)
        if isinstance(dflt_sec, dict) and key in dflt_sec:
            return dflt_sec[key]
        return default

    def set(self, section: str, key: str, value: Any) -> None:
        """Устанавливает значение ``section.key`` (без автоматического save()).

        Args:
            section: Имя секции.
            key: Имя ключа.
            value: Новое значение (должно быть сериализуемо в JSON).
        """
        self._data.setdefault(section, {})[key] = value

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _merge_defaults(self, target: dict[str, Any]) -> None:
        """Рекурсивно дополняет ``target`` недостающими значениями по умолчанию."""
        for section, values in constants.DEFAULT_SETTINGS.items():
            if section not in target or not isinstance(target[section], dict):
                target[section] = copy.deepcopy(values)
            else:
                for key, value in values.items():
                    target[section].setdefault(key, copy.deepcopy(value))
