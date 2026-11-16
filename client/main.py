"""Точка входа APrice-Client: ``python -m client.main`` (подзадача 4.1)."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from common import constants
from common.config import Config
from common.logger import setup_logging
from common.window_manager import WindowManager
from client.controller import ClientController
from client.gui import MainWindow

logger = logging.getLogger("aprice.client.main")

APPLICATION_NAME = "APrice-Client"
WINDOW_ID = "client_main"


def main(argv: Optional[list[str]] = None) -> int:
    """Создаёт окно и контроллер, запускает цикл событий. Возвращает код выхода."""
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName(APPLICATION_NAME)

    config = Config().load()
    setup_logging(component="client", config=config)

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
