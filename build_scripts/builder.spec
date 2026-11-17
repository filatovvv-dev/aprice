# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спецификация APrice-Builder (режим onedir, подзадача 5.1).

Сборка: ``python build.py`` из корня проекта (или
``pyinstaller build_scripts/builder.spec --clean --noconfirm``).
Результат: ``dist/Builder/APrice-Builder.exe``.
Вшивает data/aprice.db (полную справочную базу).
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH — каталог этого .spec (build_scripts/); корень проекта — на уровень выше.
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

datas, binaries, hiddenimports = [], [], []

# Тяжёлые пакеты с плагинами/данными: PyInstaller сам их не находит целиком.
for _collected in (
    collect_all('PyQt6'),        # плагины Qt (platforms, styles), QtNetwork и т.д.
    collect_all('matplotlib'),   # mpl-data, шрифты, backend_qtagg
    collect_all('playwright'),   # driver (node + cli.js) для `playwright install`
):
    datas += _collected[0]
    binaries += _collected[1]
    hiddenimports += _collected[2]

hiddenimports += [
    'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets', 'PyQt6.QtNetwork', 'PyQt6.sip',
    'matplotlib.backends.backend_qtagg',
    'playwright.sync_api',
    'sqlite3', 'pandas', 'numpy', 'openpyxl', 'requests',
]
# Собственные пакеты проекта (парсеры и пр. могут подгружаться динамически).
for _pkg in ('common', 'builder', 'server', 'client'):
    hiddenimports += collect_submodules(_pkg)

# Базовая справочная база. Кладётся в ресурсы (_internal/data/aprice.db);
# при первом запуске Builder копирует её в data/ рядом с .exe (common.paths).
_db = os.path.join(ROOT, 'data', 'aprice.db')
if not os.path.isfile(_db):
    raise SystemExit('Не найден %s — положите полную базу справочников перед сборкой.' % _db)
datas += [(_db, 'data')]   # ('data/aprice.db', 'data'), путь абсолютный

a = Analysis(
    [os.path.join(ROOT, 'builder/main.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Другие Qt-биндинги конфликтуют с PyQt6; tkinter не нужен.
    excludes=['PyQt5', 'PySide2', 'PySide6', 'tkinter'],
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
