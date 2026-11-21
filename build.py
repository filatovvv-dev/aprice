"""Сборка APrice: единая папка ``dist/`` с тремя .exe и общим ``_internal`` (5.6; до этого — 5.1).

Запуск (на Windows 10/11 x64, из корня проекта, в активном виртуальном окружении)::

    pip install -r requirements.txt pyinstaller
    python build.py            # единый dist/: 3 exe + общий _internal (build_scripts/aprice.spec)
    python build.py --no-installer  # без создания установщика (Inno Setup)
    python build.py --separate # запасной вариант: dist/{Builder,Server,Client}, у каждого свой _internal

Скрипт:

0. генерирует иконки (``generate_icons.py`` → ``build_scripts/assets/*.ico``);
1. очищает ``dist/`` и ``build/``;
2. запускает PyInstaller для ``aprice.spec`` (режим onedir, ``--clean --noconfirm``) и
   переносит ``dist/APrice/*`` в ``dist/``; с ``--separate`` — по очереди ``builder.spec``,
   ``server.spec``, ``client.spec`` (три отдельные папки);
3. копирует ``README.md`` и ``requirements.txt`` в ``dist/``;
4. если найден Inno Setup 6 (``ISCC.exe``), собирает установщик
   ``dist/APrice_Setup_v<версия>.exe`` по ``build_scripts/installer.iss``; нет — пропускает;
5. сообщает «Сборка завершена. Файлы в dist/».

При ошибке PyInstaller сборка останавливается с его кодом возврата — полный лог
остаётся в консоли (его нужно прислать для подбора ``hiddenimports``).
"""

from __future__ import annotations

import os
import re
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
#: скрипт Inno Setup и стандартные места ISCC.exe (6.1)
INSTALLER_SCRIPT = "installer.iss"
ICONS_SCRIPT = "generate_icons.py"
ISCC_PATHS: tuple[str, ...] = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
)
INNO_DOWNLOAD_URL = "https://jrsoftware.org/isinfo.php"


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


def generate_icons(root: Path) -> bool:
    """Обновляет иконки перед сборкой (``generate_icons.py``).

    Нет скрипта или Pillow — только предупреждение: готовые ``.ico`` (если лежат в
    ``build_scripts/assets``) остаются, а spec-файлы переживают их отсутствие.

    Returns:
        bool: ``True``, если иконки созданы заново.
    """
    script = root / ICONS_SCRIPT
    if not script.is_file():
        print(f"Предупреждение: {ICONS_SCRIPT} не найден, иконки не обновлены.", file=sys.stderr)
        return False
    code = subprocess.run([sys.executable, str(script)], cwd=root).returncode
    if code != 0:
        print("Предупреждение: иконки не созданы (pip install -r requirements-dev.txt); "
              "сборка продолжится с имеющимися.", file=sys.stderr)
        return False
    return True


def find_iscc(paths: Optional[tuple[str, ...]] = None) -> Optional[str]:
    """Ищет ``ISCC.exe`` (Inno Setup 6): стандартные пути, затем ``PATH``."""
    for candidate in (paths if paths is not None else ISCC_PATHS):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("ISCC") or shutil.which("ISCC.exe")


def installer_version(script: Path) -> str:
    """Читает ``#define MyAppVersion`` из ``installer.iss`` (для сообщения)."""
    try:
        text = script.read_text(encoding="utf-8-sig")
    except OSError:
        return "?"
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', text)
    return match.group(1) if match else "?"


def build_installer(root: Path, iscc: Optional[str] = None) -> int:
    """Собирает установщик через Inno Setup; нет компилятора — подсказка и ``0``.

    Returns:
        int: ``0`` — готово или пропущено, иначе код ISCC.
    """
    script = root / "build_scripts" / INSTALLER_SCRIPT
    iscc = iscc or find_iscc()
    if iscc is None:
        print("Inno Setup не найден. Пропускаю создание инсталлятора.")
        print(f"Скачайте Inno Setup 6 с {INNO_DOWNLOAD_URL} и запустите "
              f"build_scripts/{INSTALLER_SCRIPT} вручную.")
        return 0
    if not script.is_file():
        print(f"Предупреждение: {script} не найден, установщик пропущен.", file=sys.stderr)
        return 0
    print("Запуск Inno Setup...")
    code = subprocess.run([iscc, str(script)], cwd=root).returncode
    if code != 0:
        print(f"ОШИБКА: Inno Setup завершился с кодом {code}.", file=sys.stderr)
        return code
    print(f"Инсталлятор успешно создан в dist/APrice_Setup_v{installer_version(script)}.exe")
    return 0


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


def main(root: Optional[Path] = None, separate: bool = False, installer: bool = True) -> int:
    """Выполняет полную сборку.

    Args:
        root: корень проекта (по умолчанию — каталог этого файла).
        separate: ``True`` — старая раскладка из трёх папок (запасной вариант).
        installer: ``False`` — не создавать установщик Inno Setup. В режиме
            ``separate`` установщик не создаётся (он рассчитан на единый ``dist/``).

    Returns:
        int: ``0`` при успехе, иначе код ошибки.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    specs_dir = root / "build_scripts"

    # APRICE_NO_INSTALLER=1 — то же, что --no-installer (удобно для CI и тестов).
    installer = installer and not os.environ.get("APRICE_NO_INSTALLER")
    generate_icons(root)
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

    if installer and not separate:
        code = build_installer(root)
        if code != 0:
            return code
    elif installer and separate:
        print("Установщик не создаётся в режиме --separate (нужен единый dist/).")

    print("\nСборка завершена. Файлы в dist/")
    return 0


if __name__ == "__main__":
    _args = sys.argv[1:]
    sys.exit(main(separate="--separate" in _args, installer="--no-installer" not in _args))
