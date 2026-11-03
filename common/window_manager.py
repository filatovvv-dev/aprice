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

Дополнения задачи №2:

* **реестр окон + сигнал ``QApplication.aboutToQuit``** — геометрия
  сохраняется также при программном завершении (``app.quit()``, закрытие
  не через ``closeEvent``), п. 1.5;
* **параметр ``settings_filename``** — у каждого компонента свой INI-файл
  позиций окон (``aprice_builder_settings.ini`` / ``aprice_server_settings.ini``
  / ``aprice_client_settings.ini``), чтобы геометрии не пересекались, п. 2.1;
  старый общий файл ``aprice_settings.ini`` автоматически переименовывается
  в билдерский при первом запуске.

Хранение: ``QSettings(Format.IniFormat)`` — INI-файл рядом с исполняемым
файлом/корнем проекта (в каталоге ``config/``). Выбор INI вместо реестра
зафиксирован в README.md: он не требует прав администратора, переносится
вместе с portable-сборкой и одинаково работает на Windows/Linux.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QGuiApplication, QRect
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from common import constants

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "windows/{window_id}/geometry"


def _migrate_legacy_ini(target_name: str) -> None:
    """Переименовует общий INI v1 (``aprice_settings.ini``) в компонентный.

    Выполняется один раз: если целевой файл ещё не существует, а старый
    общий — существует, позиции окон наследуются без потери (задача №2, п. 2.1).
    """
    legacy = constants.CONFIG_DIR / constants.LEGACY_WINDOW_SETTINGS_FILE
    target = constants.CONFIG_DIR / target_name
    if legacy.exists() and not target.exists():
        try:
            legacy.rename(target)
            logger.info(
                "Устаревший INI позиций окон переименован: %s -> %s",
                legacy.name, target.name,
            )
        except OSError as exc:
            logger.warning("Не удалось переименовать %s: %s", legacy, exc)


class WindowManager:
    """Сохраняет и восстанавливает геометрию окон приложения.

    Экземпляр создаётся по одному на компонент (Builder/Server/Client);
    менеджер регистрирует обычные окна через :meth:`apply` и следит за их
    геометрией: при закрытии каждого окна (перехват ``closeEvent``) и при
    завершении всего приложения (``QApplication.aboutToQuit``).
    """

    def __init__(
        self,
        settings: QSettings | None = None,
        settings_filename: str = constants.BUILDER_WINDOW_SETTINGS_FILE,
    ) -> None:
        """Инициализирует менеджер.

        Args:
            settings: Необязательный готовый экземпляр ``QSettings``
                (удобен для тестов). По умолчанию создаётся INI-хранилище
                в каталоге ``config/``.
            settings_filename: Имя INI-файла позиций окон. Для Builder —
                ``"aprice_builder_settings.ini"``, для Server —
                ``"aprice_server_settings.ini"``, для Client —
                ``"aprice_client_settings.ini"`` (задача №2, п. 2.1).
        """
        if settings is not None:
            self._settings = settings
        else:
            constants.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _migrate_legacy_ini(settings_filename)
            self._settings = QSettings(
                str(constants.CONFIG_DIR / settings_filename),
                QSettings.Format.IniFormat,
            )
        # Реестр зарегистрированных окон: window_id -> виджет (п. 1.5).
        self._registry: dict[str, QWidget] = {}
        self._about_to_quit_connected = False

    # ------------------------------------------------------------------
    # Публичный API (W-06)
    # ------------------------------------------------------------------
    def apply(self, window: QWidget, window_id: str) -> None:
        """Применяет правила W-01/W-02/W-05 к обычному окну.

        Восстанавливает сохранённую геометрию (если она валидна) либо
        центрирует окно; регистрирует окно в реестре, вешает обработчик
        закрытия, сохраняющий позицию, и подключает сохранение по
        ``QApplication.aboutToQuit``.

        Args:
            window: Целевое окно (:class:`QWidget`).
            window_id: Уникальный идентификатор окна (например ``"builder_main"``).
        """
        restored = self._restore_geometry(window, window_id)
        if not restored:
            # W-01: нет сохранённой позиции — центр экрана.
            self.center(window)

        self._registry[window_id] = window
        self._connect_about_to_quit()

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
    def _connect_about_to_quit(self) -> None:
        """Однократно подключает сохранение всех окон при ``aboutToQuit``.

        Гарантирует сохранение геометрии даже тогда, когда окно закрывается
        минуя ``closeEvent`` — например, при ``QApplication.quit()`` или
        завершении по ``aboutToQuit`` из трей-кода Server (задача №2, п. 1.5).
        """
        if self._about_to_quit_connected:
            return
        app = QApplication.instance()
        if app is None:  # вне QApplication (юнит-тесты без GUI) — пропускаем
            return
        app.aboutToQuit.connect(self._save_all_geometries)
        self._about_to_quit_connected = True

    def _save_all_geometries(self) -> None:
        """Сохраняет геометрию всех зарегистрированных окон (aboutToQuit)."""
        for window_id, window in list(self._registry.items()):
            if window.isVisible():
                self._save_geometry(window, window_id)

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
