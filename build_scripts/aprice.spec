# -*- mode: python ; coding: utf-8 -*-
"""Единая PyInstaller-спецификация APrice (5.6): три .exe и ОБЩИЙ ``_internal``.

Сборка: ``python build.py`` (содержимое ``dist/APrice`` переносится в ``dist/``).
Результат::

    dist/APrice-Builder.exe
    dist/APrice-Server.exe
    dist/APrice-Client.exe
    dist/_internal/            <- общий для всех трёх

Приём — «несколько EXE в одном COLLECT» (штатная возможность PyInstaller, режим onedir):
у каждого компонента свой ``Analysis`` (свои hiddenimports/excludes, свой PYZ внутри
.exe, поэтому модули проекта не смешиваются), а бинарники и данные собираются в
одну папку; общие DLL (Qt, Python) лежат в ``_internal`` один раз. Версии библиотек
одни и те же (одно окружение), поэтому конфликтов нет.
Если объединение по какой-то причине не собирается — ``python build.py --separate``
вернёт прежнюю раскладку ``dist/{Builder,Server,Client}`` (build_scripts/{builder,server,client}.spec).

Chromium НЕ вшивается (server/browser_check.py докачивает его). Вшита только справочная
база Builder (data/aprice.db, если она есть).
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


# Ненужные модули Qt не тянем (пакет PyQt6 целиком не собираем); чужие биндинги конфликтуют с PyQt6.
COMMON_EXCLUDES = [
    'PyQt5', 'PySide2', 'PySide6', 'tkinter', 'pytest', 'IPython',
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineQuick', 'PyQt6.QtWebChannel', 'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtDesigner', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort', 'PyQt6.Qt3DCore', 'PyQt6.QtPositioning', 'PyQt6.QtRemoteObjects', 'PyQt6.QtHelp', 'PyQt6.QtDBus',
]
QT_BASE = ['PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'sqlite3']

# Справочная база Builder: кладётся в _internal/data/aprice.db; Builder копирует её в data/.
_db = os.path.join(ROOT, 'data', 'aprice.db')
_builder_datas = []
if os.path.isfile(_db):
    _builder_datas += [(_db, 'data')]   # ('data/aprice.db', 'data'), путь абсолютный
else:
    print('ВНИМАНИЕ: %s не найден — Builder будет собран без вшитой базы.' % _db)

ICONS = {'APrice-Builder': 'icon_builder.ico', 'APrice-Server': 'icon_server.ico', 'APrice-Client': 'icon_client.ico'}

#: компонент -> (имя exe, точка входа, пакеты проекта, hiddenimports, collect_all, excludes, datas)
COMPONENTS = [
    ('APrice-Builder', 'builder/main.py', ('common', 'builder'),
     QT_BASE, [],
     ['matplotlib', 'playwright', 'pandas', 'numpy', 'openpyxl'], _builder_datas),
    ('APrice-Server', 'server/main.py', ('common', 'server'),
     QT_BASE + ['PyQt6.QtNetwork', 'PyQt6.sip', 'playwright.sync_api'], ['playwright'],
     ['matplotlib', 'pandas', 'numpy', 'openpyxl'], []),
    ('APrice-Client', 'client/main.py', ('common', 'client'),
     QT_BASE + ['matplotlib.backends.backend_qtagg', 'pandas', 'numpy', 'openpyxl', 'requests',
                'server.protocol'],  # контракт IPC (используется common.ipc_client)
     ['matplotlib'],
     ['playwright'], []),
]

collect_args = []
for name, entry, pkgs, hidden, collected, excludes, extra_datas in COMPONENTS:
    datas, binaries, hiddenimports = list(extra_datas), [], list(hidden)
    for pkg in collected:
        _c = collect_all(pkg)
        datas += _c[0]
        binaries += _c[1]
        hiddenimports += _c[2]
    for pkg in pkgs:   # собственные пакеты проекта (динамические загрузки не пропустить)
        hiddenimports += collect_submodules(pkg)
    a = Analysis(
        [os.path.join(ROOT, entry)],
        pathex=[ROOT],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        runtime_hooks=[],
        excludes=COMMON_EXCLUDES + excludes,
        noarchive=False,
    )
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,   # onedir: библиотеки лежат рядом, а не внутри exe
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,           # GUI-приложение без консольного окна
        icon=_icon(ICONS[name]),  # 6.1: у каждого exe своя иконка
    )
    collect_args += [exe, a.binaries, a.datas]

coll = COLLECT(
    *collect_args,
    strip=False,
    upx=False,
    name='APrice',   # build.py переносит содержимое dist/APrice в dist/
)
