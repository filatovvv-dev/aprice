"""Точка входа APrice-Client: ``python -m client.main`` (подзадача 4.1)."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from common import constants, paths
from common.config import Config
from common.logger import setup_logging
from common.window_manager import WindowManager
from client.controller import ClientController
from client.first_run import ensure_database
from client.gui import MainWindow
from client.server_launcher import ensure_server

logger = logging.getLogger("aprice.client.main")

APPLICATION_NAME = "APrice-Client"
WINDOW_ID = "client_main"


def _show_fatal(message: str) -> None:
    """Критическое сообщение без падения (само окно сообщения может не открыться)."""
    try:
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "АвтоПрайс", message)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось показать сообщение об ошибке")


def main(argv: Optional[list[str]] = None) -> int:
    """Создаёт окно и контроллер, запускает цикл событий. Возвращает код выхода."""
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName(APPLICATION_NAME)

    paths.ensure_runtime_dirs()  # config/, logs/, data/ — рядом с .exe
    config = Config().load()
    setup_logging(component="client", config=config)

    # 5.5: база не вшита в дистрибутив — при первом запуске скачиваем её с зеркала.
    # Не вышло — ошибка уже показана, основное окно не открываем (без марок/моделей оно бесполезно).
    # 5.7: любая непредвиденная ошибка здесь — не трейсбек, а сообщение и штатный выход (код 1).
    try:
        database_ready = ensure_database(config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Сбой при подготовке справочника")
        _show_fatal(f"Не удалось подготовить справочник: {exc}")
        return 1
    if not database_ready:
        logger.error("Client не запущен: нет справочника aprice.db")
        return 1

    # 5.6: Server не запущен — стартуем его (ждём порт, не замораживая интерфейс).
    ensure_server(config, on_wait=app.processEvents)

    window = MainWindow(show_vpn_warning=bool(config.get("client", "show_vpn_warning", True)))
    window_manager = WindowManager(settings_filename=constants.CLIENT_WINDOW_SETTINGS_FILE)
    # window_manager нужен контроллеру для позиционирования окна настроек (W-01..W-06).
    controller = ClientController(window, config, window_manager=window_manager)  # живёт до конца exec()
    app.aboutToQuit.connect(controller.shutdown)
    window_manager.apply(window, WINDOW_ID)

    window.show()
    controller.start_session()  # открыть сеанс и подхватить активное задание Server
    logger.info("Client started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
