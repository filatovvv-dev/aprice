"""Сборка APrice в три независимых каталога ``dist/{Builder,Server,Client}`` (этап 5.1).

Запуск (на Windows 10/11 x64, из корня проекта, в активном виртуальном окружении)::

    pip install -r requirements.txt pyinstaller
    python build.py

Скрипт:

1. очищает ``dist/`` и ``build/``;
2. по очереди запускает PyInstaller для ``builder.spec``, ``server.spec``,
   ``client.spec`` (режим ``--onedir``) с ключами ``--clean --noconfirm``;
3. копирует ``README.md`` и ``requirements.txt`` в ``dist/``;
4. сообщает «Сборка завершена. Файлы в dist/».

При ошибке PyInstaller сборка останавливается с его кодом возврата — полный лог
остаётся в консоли (его нужно прислать для подбора ``hiddenimports``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

#: (spec-файл, имя каталога в dist/, имя exe без расширения)
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("builder.spec", "Builder", "APrice-Builder"),
    ("server.spec", "Server", "APrice-Server"),
    ("client.spec", "Client", "APrice-Client"),
)
EXTRA_FILES: tuple[str, ...] = ("README.md", "requirements.txt")


def clean(root: Path) -> None:
    """Удаляет ``dist/`` и ``build/`` (если есть)."""
    for name in ("dist", "build"):
        shutil.rmtree(root / name, ignore_errors=True)


def run_pyinstaller(spec: Path, root: Path) -> int:
    """Запускает PyInstaller для одного ``.spec``.

    ``python -m PyInstaller`` равносилен команде ``pyinstaller`` из ТЗ, но не
    зависит от того, лежит ли ``Scripts/`` в ``PATH``.

    Returns:
        int: код возврата PyInstaller.
    """
    cmd = [sys.executable, "-m", "PyInstaller", str(spec), "--clean", "--noconfirm"]
    print(f"\n=== {' '.join(cmd)} ===", flush=True)
    return subprocess.run(cmd, cwd=root).returncode


def find_exe(folder: Path, exe_name: str) -> Optional[Path]:
    """Ищет исполняемый файл ``exe_name`` (``.exe`` на Windows) в каталоге сборки."""
    for candidate in (folder / f"{exe_name}.exe", folder / exe_name):
        if candidate.is_file():
            return candidate
    return None


def main(root: Optional[Path] = None) -> int:
    """Выполняет полную сборку.

    Args:
        root: корень проекта (по умолчанию — каталог этого файла).

    Returns:
        int: ``0`` при успехе, иначе код ошибки.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    specs_dir = root / "build_scripts"

    clean(root)
    for spec_name, folder, exe_name in TARGETS:
        code = run_pyinstaller(specs_dir / spec_name, root)
        if code != 0:
            print(f"ОШИБКА: PyInstaller завершился с кодом {code} ({spec_name}).", file=sys.stderr)
            return code or 1
        if find_exe(root / "dist" / folder, exe_name) is None:
            print(f"ОШИБКА: после сборки {spec_name} не найден {exe_name}.exe.", file=sys.stderr)
            return 1

    dist = root / "dist"
    for name in EXTRA_FILES:
        src = root / name
        if src.is_file():
            shutil.copy2(src, dist / name)
        else:
            print(f"Предупреждение: {name} не найден, пропущен.", file=sys.stderr)

    print("\nСборка завершена. Файлы в dist/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
