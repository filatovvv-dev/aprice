# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация APrice-Server (режим onedir, 5.1 / 5.1-fix).

Сборка: ``python build.py`` из корня проекта (или
``pyinstaller build_scripts/server.spec --clean --noconfirm``).
Результат: ``dist/Server/APrice-Server.exe``.

Состав: только то, что реально импортирует APrice-Server (PyQt6 (+QtNetwork для IPC), playwright, sqlite3).
Лишние тяжёлые пакеты отрезаны через ``excludes``.
Chromium НЕ вшивается: при первом запуске Server докачивает его сам
(server/browser_check.py). Драйвер Playwright (node) берётся из collect_all('playwright').
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH — каталог этого .spec (build_scripts/); корень проекта — на уровень выше.
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

# Иконка exe (6.1): build_scripts/assets/icon_*.ico создаёт generate_icons.py.
# Нет файла — сборка не падает, exe получит стандартную иконку.
def _icon(name):
    path = os.path.join(ROOT, 'build_scripts', 'assets', name)
    if os.path.isfile(path):
        return path
    print('ВНИМАНИЕ: иконка %s не найдена (python generate_icons.py).' % path)
    return None


datas, binaries, hiddenimports = [], [], []
for _collected in (
    collect_all('playwright'),   # driver (node + cli.js) для `playwright install`
):
    datas += _collected[0]
    binaries += _collected[1]
    hiddenimports += _collected[2]

hiddenimports += [
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtNetwork', 'PyQt6.sip',
    'playwright.sync_api',
    'sqlite3',   # встроен в Python, но PyInstaller иногда его теряет
]
# Собственные пакеты проекта (динамические загрузки не пропустить).
for _pkg in ('common', 'server'):
    hiddenimports += collect_submodules(_pkg)

# Пакеты, которые этому компоненту НЕ нужны (см. ТЗ 5.1-fix, п. 1–3).
# Другие Qt-биндинги конфликтуют с PyQt6; ненужные модули Qt не тянем.
excludes = [
    'matplotlib', 'pandas', 'numpy', 'openpyxl',
    'PyQt5', 'PySide2', 'PySide6', 'tkinter', 'pytest', 'IPython',
    # неиспользуемые модули Qt (пакет PyQt6 целиком не собираем)
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineQuick', 'PyQt6.QtWebChannel', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.Qt3DCore', 'PyQt6.QtPositioning', 'PyQt6.QtRemoteObjects', 'PyQt6.QtHelp', 'PyQt6.QtDBus'
]

a = Analysis(
    [os.path.join(ROOT, 'server/main.py')],
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
    name='APrice-Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI-приложение без консольного окна
    icon=_icon('icon_server.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Server',
)
