# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация APrice-Builder (режим onedir, 5.1 / 5.1-fix).

Сборка: ``python build.py`` из корня проекта (или
``pyinstaller build_scripts/builder.spec --clean --noconfirm``).
Результат: ``dist/Builder/APrice-Builder.exe``.

Состав: только то, что реально импортирует APrice-Builder (PyQt6, sqlite3; HTTP через urllib).
Лишние тяжёлые пакеты отрезаны через ``excludes``.
Вшивает data/aprice.db (полную справочную базу). HTML-фолбэк через Playwright в
собранном Builder недоступен (playwright исключён); основной источник — API cars-base.ru.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH — каталог этого .spec (build_scripts/); корень проекта — на уровень выше.
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

datas, binaries, hiddenimports = [], [], []

hiddenimports += [
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
    'sqlite3',   # встроен в Python, но PyInstaller иногда его теряет
]
# Собственные пакеты проекта (динамические загрузки не пропустить).
for _pkg in ('common', 'builder'):
    hiddenimports += collect_submodules(_pkg)

# Пакеты, которые этому компоненту НЕ нужны (см. ТЗ 5.1-fix, п. 1–3).
# Другие Qt-биндинги конфликтуют с PyQt6; ненужные модули Qt не тянем.
excludes = [
    'matplotlib', 'playwright', 'pandas', 'numpy', 'openpyxl',
    'PyQt5', 'PySide2', 'PySide6', 'tkinter', 'pytest', 'IPython',
    # неиспользуемые модули Qt (пакет PyQt6 целиком не собираем)
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineQuick', 'PyQt6.QtWebChannel', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.Qt3DCore', 'PyQt6.QtPositioning', 'PyQt6.QtRemoteObjects', 'PyQt6.QtHelp', 'PyQt6.QtDBus'
]

# Базовая справочная база. Кладётся в ресурсы (_internal/data/aprice.db);
# при первом запуске Builder копирует её в data/ рядом с .exe (common.paths).
# Если файла нет — сборка НЕ падает (5.5: Client базу больше не вшивает, а скачивает
# сам): Builder без вшитой базы создаст пустую схему и наполнит её при сборе.
_db = os.path.join(ROOT, 'data', 'aprice.db')
if os.path.isfile(_db):
    datas += [(_db, 'data')]   # ('data/aprice.db', 'data'), путь абсолютный
else:
    print('ВНИМАНИЕ: %s не найден — Builder будет собран без вшитой базы.' % _db)

a = Analysis(
    [os.path.join(ROOT, 'builder/main.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir: библиотеки лежат рядом, а не внутри exe
    name='APrice-Builder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI-приложение без консольного окна
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Builder',
)
