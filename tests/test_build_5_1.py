"""Тесты подзадачи 5.1: пути рядом с .exe, копирование БД, проверка Chromium, build.py.

Windows-сборку PyInstaller в этих тестах не запускаем: проверяются логика путей
(с подменой ``sys.frozen``/``sys.executable``/``sys._MEIPASS``), установка Chromium
на моках и состав ``.spec``-файлов.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common import paths
from server import browser_check

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# Пути
# ----------------------------------------------------------------------
def test_base_dir_in_dev_is_project_root():
    assert paths.get_base_dir() == ROOT
    assert paths.get_resource_dir() == ROOT


def test_base_dir_frozen_is_exe_dir(monkeypatch, tmp_path):
    exe = tmp_path / "Server" / "APrice-Server.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(exe.parent / "_internal"), raising=False)
    assert paths.get_base_dir() == exe.parent.resolve()
    assert paths.get_resource_dir() == exe.parent / "_internal"


def test_constants_follow_frozen_base_dir(monkeypatch, tmp_path):
    """После «замораживания» config/logs/data и все SQLite — рядом с .exe."""
    from common import constants

    exe = tmp_path / "App" / "APrice-Client.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    try:
        importlib.reload(paths)
        importlib.reload(constants)
        base = exe.parent.resolve()
        assert constants.BASE_DIR == base
        assert constants.CONFIG_DIR == base / "config"
        assert constants.LOGS_DIR == base / "logs"
        assert constants.DB_FILE == base / "data" / "aprice.db"
        assert constants.SERVER_STATE_DB == base / "data" / "aprice_server_state.db"
    finally:
        monkeypatch.undo()
        importlib.reload(paths)
        importlib.reload(constants)
    assert constants.BASE_DIR == ROOT


def test_ensure_runtime_dirs_creates_three_dirs(tmp_path):
    made = paths.ensure_runtime_dirs(tmp_path)
    assert {p.name for p in made} == {"config", "logs", "data"}
    assert all(p.is_dir() for p in made)
    paths.ensure_runtime_dirs(tmp_path)  # идемпотентно


# ----------------------------------------------------------------------
# Копирование aprice.db из ресурсов
# ----------------------------------------------------------------------
def _make_resource(tmp_path: Path, payload: bytes = b"RESOURCE-DB") -> Path:
    res = tmp_path / "_internal"
    (res / "data").mkdir(parents=True)
    (res / "data" / "aprice.db").write_bytes(payload)
    return res


def test_bootstrap_copies_db_when_missing(tmp_path):
    res = _make_resource(tmp_path)
    target = tmp_path / "exe" / "data" / "aprice.db"
    assert paths.bootstrap_reference_db(target, res) is True
    assert target.read_bytes() == b"RESOURCE-DB"
    assert not list(target.parent.glob("*.tmp"))


def test_bootstrap_never_overwrites_existing_db(tmp_path):
    res = _make_resource(tmp_path)
    target = tmp_path / "exe" / "data" / "aprice.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"USER-UPDATED")
    assert paths.bootstrap_reference_db(target, res) is False
    assert target.read_bytes() == b"USER-UPDATED"


def test_bootstrap_without_resource_is_noop(tmp_path):
    target = tmp_path / "data" / "aprice.db"
    assert paths.bootstrap_reference_db(target, tmp_path / "nothing") is False
    assert not target.exists()


def test_bootstrap_same_file_is_noop(tmp_path):
    """В режиме разработки ресурс и рабочая база — один файл."""
    (tmp_path / "data").mkdir()
    # целевого файла нет, источник совпадает с целью — копировать нечего
    assert paths.bootstrap_reference_db(tmp_path / "data" / "aprice.db", tmp_path) is False


# ----------------------------------------------------------------------
# Chromium
# ----------------------------------------------------------------------
class FakeUi:
    def __init__(self, result="run"):
        self.errors: list[str] = []
        self.ran = 0
        self._result = result

    def run(self, work):
        self.ran += 1
        return work() if self._result == "run" else self._result

    def show_error(self, message):
        self.errors.append(message)


def test_ensure_chromium_skips_install_when_present():
    ui = FakeUi()
    ok = browser_check.ensure_chromium(
        ui, is_installed=lambda: True, installer=lambda: pytest.fail("не должен ставить")
    )
    assert ok is True and ui.ran == 0


def test_ensure_chromium_installs_when_missing():
    state = {"installed": False}

    def installer():
        state["installed"] = True
        return True, "ok"

    ui = FakeUi()
    ok = browser_check.ensure_chromium(ui, is_installed=lambda: state["installed"], installer=installer)
    assert ok is True and ui.ran == 1 and ui.errors == []


def test_ensure_chromium_failure_shows_required_message():
    ui = FakeUi()
    ok = browser_check.ensure_chromium(
        ui, is_installed=lambda: False, installer=lambda: (False, "SSL: CERTIFICATE_VERIFY_FAILED")
    )
    assert ok is False
    assert len(ui.errors) == 1
    assert ui.errors[0].startswith(
        "Не удалось установить браузер. Проверьте соединение или установите вручную"
    )
    assert "Chromium не найден по пути" in ui.errors[0]  # 5.2: плюс подсказка с путём


def test_ensure_chromium_cancel_shows_error():
    ui = FakeUi(result=None)
    assert browser_check.ensure_chromium(ui, is_installed=lambda: False) is False
    assert len(ui.errors) == 1 and ui.errors[0].startswith(browser_check.INSTALL_FAILED_MESSAGE)


def test_install_chromium_reports_nonzero_exit():
    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="SSL error")

    ok, details = browser_check.install_chromium(runner)
    assert ok is False and "SSL error" in details


def test_install_chromium_success_and_command():
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    ok, _ = browser_check.install_chromium(runner)
    assert ok is True
    assert seen["argv"][-2:] == ["install", "chromium"]


def test_install_chromium_handles_missing_executable():
    def runner(argv, **kw):
        raise FileNotFoundError("playwright")

    ok, details = browser_check.install_chromium(runner)
    assert ok is False and "playwright" in details


def test_chromium_installed_checks_executable_path(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"

    class FakePw:
        chromium = SimpleNamespace(executable_path=str(exe))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import playwright.sync_api as api

    monkeypatch.setattr(api, "sync_playwright", lambda: FakePw())
    assert browser_check.chromium_installed() is False
    exe.write_bytes(b"x")
    assert browser_check.chromium_installed() is True


def test_chromium_installed_false_when_playwright_breaks(monkeypatch):
    import playwright.sync_api as api

    def boom():
        raise RuntimeError("driver missing")

    monkeypatch.setattr(api, "sync_playwright", boom)
    assert browser_check.chromium_installed() is False


# ----------------------------------------------------------------------
# build.py и .spec
# ----------------------------------------------------------------------
SPECS = {
    "builder": ("APrice-Builder", "builder/main.py"),
    "server": ("APrice-Server", "server/main.py"),
    "client": ("APrice-Client", "client/main.py"),
}


# Что каждый компонент реально использует (5.1-fix): включить / исключить.
EXCLUDES = {
    "builder": {"matplotlib", "playwright", "pandas", "numpy", "openpyxl"},
    "server": {"matplotlib", "pandas", "numpy", "openpyxl"},
    "client": {"playwright"},
}
COLLECT_ALL = {
    "builder": set(),
    "server": {"playwright"},
    "client": {"matplotlib"},
}
HIDDEN = {
    "builder": {"PyQt6.QtWidgets", "sqlite3"},
    "server": {"PyQt6.QtNetwork", "playwright.sync_api", "sqlite3"},
    "client": {"matplotlib.backends.backend_qtagg", "pandas", "numpy", "openpyxl", "requests", "sqlite3"},
}


def _spec_namespace(key: str, specpath: "str | None" = None) -> dict:
    """Исполняет .spec с заглушками PyInstaller и возвращает его переменные."""
    captured: dict = {}

    class Rec:
        def __init__(self, *a, **kw):
            self.kw = kw
            self.pure = []
            self.scripts, self.binaries, self.datas = [], [], []

    def collect_all(name):
        captured.setdefault("collected", []).append(name)
        return [], [], []

    ns = {
        "SPECPATH": specpath or str(ROOT / "build_scripts"),
        "Analysis": lambda *a, **kw: captured.update(analysis=kw) or Rec(**kw),
        "PYZ": Rec, "EXE": lambda *a, **kw: captured.update(exe=kw) or Rec(**kw),
        "COLLECT": lambda *a, **kw: captured.update(coll=kw) or Rec(**kw),
    }
    import types

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = collect_all
    hooks.collect_submodules = lambda n: [n]
    pkg = {"PyInstaller": types.ModuleType("PyInstaller"),
           "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
           "PyInstaller.utils.hooks": hooks}
    saved = {k: sys.modules.get(k) for k in pkg}
    sys.modules.update(pkg)
    try:
        exec(compile((ROOT / "build_scripts" / f"{key}.spec").read_text(encoding="utf-8"), f"{key}.spec", "exec"), ns)
        captured["datas"] = ns.get("datas")
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return captured


@pytest.mark.parametrize("key", SPECS)
def test_spec_files_are_onedir_with_required_hooks(key):
    name, entry = SPECS[key]
    text = (ROOT / "build_scripts" / f"{key}.spec").read_text(encoding="utf-8")
    assert f"name='{name}'" in text and entry in text
    assert "exclude_binaries=True" in text and "COLLECT(" in text  # onedir
    assert "onefile" not in text.lower()
    cap = _spec_namespace(key)
    assert set(cap.get("collected", [])) == COLLECT_ALL[key]
    assert cap["analysis"]["hiddenimports"]
    assert HIDDEN[key] <= set(cap["analysis"]["hiddenimports"])


@pytest.mark.parametrize("key", SPECS)
def test_spec_excludes_and_includes_are_disjoint(key):
    """Исключённое не включено (и наоборот): excludes ⊇ ожидаемого, hiddenimports ∩ excludes = ∅."""
    cap = _spec_namespace(key)
    excludes = set(cap["analysis"]["excludes"])
    hidden = set(cap["analysis"]["hiddenimports"])
    assert EXCLUDES[key] <= excludes
    assert not (hidden & excludes)
    assert not (set(cap.get("collected", [])) & excludes)
    # Лишние модули Qt не собираем, нужные не исключаем
    assert {"PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.QtCore"}.isdisjoint(excludes)


def test_components_do_not_bundle_each_others_code():
    assert "builder" not in " ".join(_spec_namespace("server")["analysis"]["hiddenimports"]).split()
    client_hidden = set(_spec_namespace("client")["analysis"]["hiddenimports"])
    assert "server.protocol" in client_hidden and "server" not in client_hidden
    assert "client" not in _spec_namespace("builder")["analysis"]["hiddenimports"]


def test_only_builder_spec_bundles_database():
    for key in SPECS:
        text = (ROOT / "build_scripts" / f"{key}.spec").read_text(encoding="utf-8")
        has_db = "('data/aprice.db', 'data')" in text 
        assert has_db is (key == "builder"), key


def test_no_chromium_bundled_in_specs():
    for key in SPECS:
        text = (ROOT / "build_scripts" / f"{key}.spec").read_text(encoding="utf-8")
        assert "ms-playwright" not in text


def test_build_py_steps(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("aprice_build", ROOT / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    root = tmp_path
    (root / "build_scripts").mkdir()
    (root / "dist" / "stale").mkdir(parents=True)
    (root / "build" / "stale").mkdir(parents=True)
    (root / "README.md").write_text("r", encoding="utf-8")
    (root / "requirements.txt").write_text("q", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        spec_name = Path(cmd[3]).name
        for key, (folder, exe) in {
            "builder.spec": ("Builder", "APrice-Builder"),
            "server.spec": ("Server", "APrice-Server"),
            "client.spec": ("Client", "APrice-Client"),
        }.items():
            if key == spec_name:
                (root / "dist" / folder).mkdir(parents=True, exist_ok=True)
                (root / "dist" / folder / f"{exe}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    assert build.main(root=root, separate=True) == 0
    assert not (root / "dist" / "stale").exists()
    assert not (root / "build" / "stale").exists()
    assert [Path(c[3]).name for c in calls] == ["builder.spec", "server.spec", "client.spec"]
    for c in calls:
        assert c[1:3] == ["-m", "PyInstaller"]
        assert c[4:] == ["--clean", "--noconfirm"]
    assert (root / "dist" / "README.md").read_text(encoding="utf-8") == "r"
    assert (root / "dist" / "requirements.txt").read_text(encoding="utf-8") == "q"


def test_build_py_stops_on_failure(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("aprice_build2", ROOT / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    (tmp_path / "build_scripts").mkdir()
    monkeypatch.setattr(
        build.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3)
    )
    assert build.main(root=tmp_path, separate=True) != 0
