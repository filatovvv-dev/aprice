"""Менеджер окон проекта «АвтоПрайс» (требования W-01..W-06).

Класс :class:`WindowManager` инкапсулирует правила позиционирования окон
всех трёх компонентов комплекса (Builder / Server / Client):

* W-01 — первое открытие окна в центре экрана (по ``availableGeometry``,
  с учётом панели задач);
* W-02 — после перемещения позиция и размер сохраняются;
* W-03 — окна ошибок всегда в центре, без сохранения;
* W-04 — сохранённая позиция переживает перезапуск (``QSettings``);
* W-05 — если восстановленный прямоугольник не пересекается ни с одним
  экраном — окно центрируется;
* W-06 — единый интерфейс для всех окон: :meth:`apply` и :meth:`center_error`.

Хранение: ``QSettings(Format.IniFormat)`` — INI-файл рядом с исполняемым
файлом/корнем проекта (``config/aprice_settings.ini``). Выбор INI вместо
реестра зафиксирован в README.md: он не требует прав администратора,
переживается вместе с portable-сборкой и одинаково работает на Windows/Linux.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QGuiApplication, QRect
from PyQt6.QtWidgets import QDialog, QWidget

from common import constants

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "windows/{window_id}/geometry"


class WindowManager:
    """Сохраняет и восстанавливает геометрию окон приложения."""

    def __init__(self, settings: QSettings | None = None) -> None:
        """Инициализирует менеджер.

        Args:
            settings: Необязательный готовый экземпляр ``QSettings``.
                По умолчанию создаётся INI-хранилище в каталоге ``config/``.
        """
        if settings is not None:
            self._settings = settings
        else:
            constants.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self._settings = QSettings(
                str(constants.CONFIG_DIR / "aprice_settings.ini"),
                QSettings.Format.IniFormat,
            )

    # ------------------------------------------------------------------
    # Публичный API (W-06)
    # ------------------------------------------------------------------
    def apply(self, window: QWidget, window_id: str) -> None:
        """Применяет правила W-01/W-02/W-05 к обычному окну.

        Восстанавливает сохранённую геометрию (если она валидна) либо
        центрирует окно; вешает обработчик закрытия, сохраняющий позицию.

        Args:
            window: Целевое окно (:class:`QWidget`).
            window_id: Уникальный идентификатор окна (например ``"builder_main"``).
        """
        restored = self._restore_geometry(window, window_id)
        if not restored:
            # W-01: нет сохранённой позиции — центр экрана.
            self.center(window)

        original_closeEvent = window.closeEvent

        def _close_event(event) -> None:  # noqa: ANN001
            self._save_geometry(window, window_id)
            original_closeEvent(event)

        window.closeEvent = _close_event  # type: ignore[method-assign]

    def center_error(self, dialog: QDialog) -> None:
        """Центрирует диалог ошибки и НЕ сохраняет его позицию (W-03)."""
        self.center(dialog)

    @staticmethod
    def center(widget: QWidget) -> None:
        """Центрирует виджет относительно доступной области экрана (W-01).

        Учитывается панель задач: используется ``availableGeometry()``
        того экрана, на котором сейчас находится виджет (или основного).
        """
        widget.ensurePolished()
        screen = QGuiApplication.screenAt(widget.frameGeometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:  # теоретически невозможно при активном QApplication
            return
        available: QRect = screen.availableGeometry()
        frame = widget.frameGeometry()
        frame.moveCenter(available.center())
        widget.move(frame.topLeft())

    # ------------------------------------------------------------------
    # Внутренняя реализация
    # ------------------------------------------------------------------
    def _save_geometry(self, window: QWidget, window_id: str) -> None:
        """Сохраняет ``pos()`` и ``size()`` окна в QSettings (W-02, W-04)."""
        geometry = {
            "x": window.x(),
            "y": window.y(),
            "width": window.width(),
            "height": window.height(),
        }
        key = _GEOMETRY_KEY.format(window_id=window_id)
        for name, value in geometry.items():
            self._settings.setValue(f"{key}/{name}", value)
        self._settings.sync()
        logger.debug("Позиция окна '%s' сохранена: %s", window_id, geometry)

    def _restore_geometry(self, window: QWidget, window_id: str) -> bool:
        """Восстанавливает геометрию окна.

        Returns:
            bool: ``True``, если сохранённая позиция найдена и признана
            валидной; ``False`` — если нужно центрировать (W-01, W-05).
        """
        key = _GEOMETRY_KEY.format(window_id=window_id)
        if not self._settings.contains(f"{key}/x"):
            return False

        try:
            x = int(self._settings.value(f"{key}/x"))
            y = int(self._settings.value(f"{key}/y"))
            width = int(self._settings.value(f"{key}/width", 800))
            height = int(self._settings.value(f"{key}/height", 600))
        except (TypeError, ValueError):
            logger.warning("Повреждённая геометрия окна '%s' — центрирую", window_id)
            return False

        rect = QRect(x, y, max(width, 100), max(height, 100))
        if not self._intersects_any_screen(rect):
            # W-05: окно оказалось за пределами всех экранов.
            logger.info(
                "Сохранённая позиция окна '%s' вне экранов (%s) — центрирую",
                window_id,
                rect,
            )
            return False

        window.resize(rect.size())
        window.move(rect.topLeft())
        return True

    @staticmethod
    def _intersects_any_screen(rect: QRect) -> bool:
        """Проверяет пересечение прямоугольника с областью хотя бы одного экрана."""
        for screen in QGuiApplication.screens():
            if screen.availableGeometry().intersects(rect):
                return True
        return False
