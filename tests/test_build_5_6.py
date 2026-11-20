"""5.6: единый dist/ с тремя exe и общим _internal (build.py + aprice.spec)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXES = ["APrice-Builder", "APrice-Server", "APrice-Client"]


def load_build(name="aprice_build_56"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_unified_spec():
    """Исполняет aprice.spec с заглушками PyInstaller; возвращает всё, что он вызвал."""
    rec = {"analysis": [], "exe": [], "coll": None, "collected": []}

    class Obj:
        def __init__(self, *a, **kw):
            self.kw = kw
            self.pure, self.scripts, self.binaries, self.datas = [], [], [], []

    def collect_all(name):
        rec["collected"].append(name)
        return [], [], []

    def analysis(*a, **kw):
        rec["analysis"].append(kw)
        return Obj(**kw)

    def exe(*a, **kw):
        rec["exe"].append(kw)
        return Obj(**kw)

    def coll(*a, **kw):
        rec["coll"] = (a, kw)
        return Obj(**kw)

    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = collect_all
    hooks.collect_submodules = lambda n: [n]
    mods = {"PyInstaller": types.ModuleType("PyInstaller"),
            "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
            "PyInstaller.utils.hooks": hooks}
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    ns = {"SPECPATH": str(ROOT / "build_scripts"), "Analysis": analysis, "PYZ": Obj, "EXE": exe, "COLLECT": coll}
    try:
        text = (ROOT / "build_scripts" / "aprice.spec").read_text(encoding="utf-8")
        exec(compile(text, "aprice.spec", "exec"), ns)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return rec


def test_unified_spec_has_three_exes_and_one_collect():
    rec = run_unified_spec()
    assert [e["name"] for e in rec["exe"]] == EXES
    assert all(e["exclude_binaries"] is True for e in rec["exe"])  # onedir
    args, kw = rec["coll"]
    assert len(args) == 9 and kw["name"] == "APrice"  # 3 × (exe, binaries, datas) в одном COLLECT


def test_unified_spec_keeps_per_component_includes_and_excludes():
    rec = run_unified_spec()
    builder, server, client = rec["analysis"]
    assert "playwright" in builder["excludes"] and "playwright" in client["excludes"]
    assert "playwright" not in server["excludes"] and "matplotlib" in server["excludes"]
    assert "playwright.sync_api" in server["hiddenimports"] and "PyQt6.QtNetwork" in server["hiddenimports"]
    assert "matplotlib.backends.backend_qtagg" in client["hiddenimports"]
    assert rec["collected"] == ["playwright", "matplotlib"]
    for a in rec["analysis"]:
        assert not (set(a["hiddenimports"]) & set(a["excludes"]))
        assert {"PyQt6.QtWidgets", "PyQt6.QtGui", "PyQt6.QtCore"}.isdisjoint(a["excludes"])


def test_unified_spec_components_do_not_bundle_each_others_code():
    builder, server, client = run_unified_spec()["analysis"]
    assert "server" not in builder["hiddenimports"] and "client" not in builder["hiddenimports"]
    assert "builder" not in server["hiddenimports"] and "client" not in server["hiddenimports"]
    assert "server.protocol" in client["hiddenimports"] and "server" not in client["hiddenimports"]


def test_unified_spec_does_not_bundle_chromium_and_only_builder_gets_db():
    text = (ROOT / "build_scripts" / "aprice.spec").read_text(encoding="utf-8")
    assert "ms-playwright" not in text and "onefile" not in text.lower()
    builder, server, client = run_unified_spec()["analysis"]
    assert server["datas"] == [] and client["datas"] == []
    assert all(d[1] == "data" for d in builder["datas"])


def test_flatten_unified_moves_everything_up(tmp_path):
    build = load_build()
    src = tmp_path / "APrice"
    (src / "_internal").mkdir(parents=True)
    (src / "_internal" / "x.dll").write_bytes(b"d")
    for n in EXES:
        (src / f"{n}.exe").write_bytes(b"MZ")
    build.flatten_unified(tmp_path)
    assert not src.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["_internal"] + [f"{n}.exe" for n in EXES])
    assert (tmp_path / "_internal" / "x.dll").is_file()


def test_flatten_unified_refuses_to_overwrite(tmp_path):
    build = load_build()
    (tmp_path / "APrice").mkdir()
    (tmp_path / "APrice" / "a.exe").write_bytes(b"1")
    (tmp_path / "a.exe").write_bytes(b"0")
    with pytest.raises(FileExistsError):
        build.flatten_unified(tmp_path)


def _fake_run_factory(root, calls, exes=EXES, code=0):
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if code == 0:
            out = root / "dist" / "APrice"
            (out / "_internal").mkdir(parents=True, exist_ok=True)
            for n in exes:
                (out / f"{n}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(cmd, code)
    return fake_run


def test_build_py_default_is_single_dist(monkeypatch, tmp_path):
    build = load_build()
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "README.md").write_text("r", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(build.subprocess, "run", _fake_run_factory(tmp_path, calls))
    assert build.main(root=tmp_path) == 0
    assert [Path(c[3]).name for c in calls] == ["aprice.spec"]  # один запуск PyInstaller
    dist = tmp_path / "dist"
    assert all((dist / f"{n}.exe").is_file() for n in EXES)
    assert (dist / "_internal").is_dir() and not (dist / "APrice").exists()
    assert not (dist / "Server").exists() and (dist / "README.md").is_file()


def test_build_py_unified_fails_when_exe_missing(monkeypatch, tmp_path):
    build = load_build()
    (tmp_path / "build_scripts").mkdir()
    monkeypatch.setattr(build.subprocess, "run", _fake_run_factory(tmp_path, [], exes=EXES[:2]))
    assert build.main(root=tmp_path) == 1


def test_build_py_unified_stops_on_pyinstaller_error(monkeypatch, tmp_path):
    build = load_build()
    (tmp_path / "build_scripts").mkdir()
    monkeypatch.setattr(build.subprocess, "run", _fake_run_factory(tmp_path, [], code=3))
    assert build.main(root=tmp_path) == 3


def test_separate_mode_still_available(monkeypatch, tmp_path):
    build = load_build()
    (tmp_path / "build_scripts").mkdir()
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(Path(cmd[3]).name)
        folder, exe = {"builder.spec": ("Builder", EXES[0]), "server.spec": ("Server", EXES[1]),
                       "client.spec": ("Client", EXES[2])}[Path(cmd[3]).name]
        (tmp_path / "dist" / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / "dist" / folder / f"{exe}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    assert build.main(root=tmp_path, separate=True) == 0
    assert calls == ["builder.spec", "server.spec", "client.spec"]
