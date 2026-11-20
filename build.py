"""Сборка APrice: единая папка ``dist/`` с тремя .exe и общим ``_internal`` (5.6; до этого — 5.1).

Запуск (на Windows 10/11 x64, из корня проекта, в активном виртуальном окружении)::

    pip install -r requirements.txt pyinstaller
    python build.py            # единый dist/: 3 exe + общий _internal (build_scripts/aprice.spec)
    python build.py --separate # запасной вариант: dist/{Builder,Server,Client}, у каждого свой _internal

Скрипт:

1. очищает ``dist/`` и ``build/``;
2. запускает PyInstaller для ``aprice.spec`` (режим onedir, ``--clean --noconfirm``) и
   переносит ``dist/APrice/*`` в ``dist/``; с ``--separate`` — по очереди ``builder.spec``,
   ``server.spec``, ``client.spec`` (три отдельные папки);
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
#: единая спецификация (5.6) и имена трёх exe, которые должны лежать прямо в dist/
UNIFIED_SPEC = "aprice.spec"
UNIFIED_FOLDER = "APrice"
EXE_NAMES: tuple[str, ...] = tuple(exe for _, _, exe in TARGETS)


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


def flatten_unified(dist: Path, folder: str = UNIFIED_FOLDER) -> None:
    """Переносит содержимое ``dist/<folder>`` в ``dist/`` (exe и общий ``_internal``).

    Raises:
        FileExistsError: в ``dist/`` уже есть файл с таким именем (чужой мусор).
    """
    src = dist / folder
    for item in sorted(src.iterdir()):
        target = dist / item.name
        if target.exists():
            raise FileExistsError(f"{target} уже существует")
        shutil.move(str(item), str(target))
    src.rmdir()


def build_unified(root: Path, specs_dir: Path) -> int:
    """Единая сборка: один PyInstaller, три exe + общий ``_internal`` в ``dist/``."""
    code = run_pyinstaller(specs_dir / UNIFIED_SPEC, root)
    if code != 0:
        print(f"ОШИБКА: PyInstaller завершился с кодом {code} ({UNIFIED_SPEC}).", file=sys.stderr)
        print("Запасной вариант: python build.py --separate", file=sys.stderr)
        return code or 1
    dist = root / "dist"
    try:
        flatten_unified(dist)
    except (OSError, FileNotFoundError) as exc:
        print(f"ОШИБКА: не удалось собрать единый dist/: {exc}", file=sys.stderr)
        return 1
    for exe_name in EXE_NAMES:
        if find_exe(dist, exe_name) is None:
            print(f"ОШИБКА: после сборки не найден {exe_name}.exe в dist/.", file=sys.stderr)
            return 1
    return 0


def build_separate(root: Path, specs_dir: Path) -> int:
    """Запасная сборка: по spec на компонент, каждый в своей папке ``dist/<Имя>``."""
    for spec_name, folder, exe_name in TARGETS:
        code = run_pyinstaller(specs_dir / spec_name, root)
        if code != 0:
            print(f"ОШИБКА: PyInstaller завершился с кодом {code} ({spec_name}).", file=sys.stderr)
            return code or 1
        if find_exe(root / "dist" / folder, exe_name) is None:
            print(f"ОШИБКА: после сборки {spec_name} не найден {exe_name}.exe.", file=sys.stderr)
            return 1
    return 0


def main(root: Optional[Path] = None, separate: bool = False) -> int:
    """Выполняет полную сборку.

    Args:
        root: корень проекта (по умолчанию — каталог этого файла).
        separate: ``True`` — старая раскладка из трёх папок (запасной вариант).

    Returns:
        int: ``0`` при успехе, иначе код ошибки.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    specs_dir = root / "build_scripts"

    clean(root)
    code = build_separate(root, specs_dir) if separate else build_unified(root, specs_dir)
    if code != 0:
        return code

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
    sys.exit(main(separate="--separate" in sys.argv[1:]))
