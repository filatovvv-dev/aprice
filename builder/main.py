"""Точка входа APrice-Builder.

Запуск из корня проекта::

    python -m builder.main

Порядок инициализации: загрузка конфигурации (создание settings.json при
первом запуске), настройка логирования, создание схемы БД, показ GUI-окна.
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from builder.database import Database
from builder.gui import BuilderWindow
from common.config import Config
from common.logger import setup_logging


def main() -> int:
    """Создаёт приложение и запускает главный цикл Builder’а.

    Returns:
        int: код завершения процесса.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("APrice-Builder")
    app.setOrganizationName("APrice")

    config = Config().load()
    gui_handler = setup_logging(config, component="builder")

    # Создаём/проверяем схему БД до показа окна (критерий: db создаётся
    # при первом запуске Builder).
    with Database() as db:
        db.init_schema()

    window = BuilderWindow(config, gui_handler)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
