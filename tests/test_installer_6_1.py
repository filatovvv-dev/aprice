"""6.1: иконки (generate_icons.py), icon= в spec, installer.iss, build.py (иконки + ISCC)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "build_scripts" / "assets"
ISS = ROOT / "build_scripts" / "installer.iss"
EXES = ["APrice-Builder", "APrice-Server", "APrice-Client"]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen():
    return load(ROOT / "generate_icons.py", "generate_icons_61")


@pytest.fixture(scope="module")
def icons(gen, tmp_path_factory):
    out = tmp_path_factory.mktemp("icons")
    return {p.name: p for p in gen.generate_icons(out)}


# ----------------------------------------------------------------- иконки
def test_three_icons_created(icons):
    assert sorted(icons) == ["icon_builder.ico", "icon_client.ico", "icon_server.ico"]
    assert all(p.stat().st_size > 1000 for p in icons.values())


@pytest.mark.parametrize("name", ["icon_client.ico", "icon_server.ico", "icon_builder.ico"])
def test_icon_has_all_embedded_sizes(icons, name):
    sizes = Image.open(icons[name]).ico.sizes()
    assert {(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)} <= set(sizes)


def _rgba(path):
    img = Image.open(path)
    img.size = (256, 256)
    return img.convert("RGBA")


@pytest.mark.parametrize("name,rgb", [
    ("icon_client.ico", (30, 111, 217)),    # синий
    ("icon_server.ico", (211, 47, 47)),     # красный
    ("icon_builder.ico", (46, 139, 62)),    # зелёный
])
def test_icon_colors_rounded_corners_and_white_letters(icons, name, rgb):
    img = _rgba(icons[name])
    assert img.size == (256, 256)
    assert img.getpixel((0, 0))[3] == 0                      # скруглённый угол прозрачен
    assert img.getpixel((128, 4))[:3] == rgb                 # фон нужного цвета
    pixels = list(img.getdata())
    white = sum(1 for r, g, b, a in pixels if r > 240 and g > 240 and b > 240 and a == 255)
    assert white > 2000                                      # крупные белые буквы


def test_icons_differ_from_each_other(icons):
    data = [_rgba(p).tobytes() for p in icons.values()]
    assert len(set(data)) == 3


def test_text_is_centered_and_large(gen):
    img = gen.render_icon("S", (211, 47, 47))
    xs = [x for x in range(256) for y in range(256) if (lambda p: p[0] > 240 and p[1] > 240 and p[2] > 240 and p[3] == 255)(img.getpixel((x, y)))]
    assert abs((min(xs) + max(xs)) / 2 - 128) <= 4 and (max(xs) - min(xs)) > 80  # по центру и крупно


def test_main_returns_error_without_pillow(gen, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert gen.main() == 1
    assert "Pillow" in capsys.readouterr().err


def test_repo_contains_generated_icons():
    for n in ("client", "server", "builder"):
        assert (ASSETS / f"icon_{n}.ico").is_file()


def test_pillow_in_requirements_dev():
    assert re.search(r"(?im)^pillow\b", (ROOT / "requirements-dev.txt").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- spec
def test_unified_spec_sets_icon_for_each_exe():
    from tests.test_build_5_6 import run_unified_spec

    rec = run_unified_spec()
    icons = {e["name"]: Path(e["icon"]).name for e in rec["exe"]}
    assert icons == {"APrice-Builder": "icon_builder.ico", "APrice-Server": "icon_server.ico",
                     "APrice-Client": "icon_client.ico"}
    assert all(Path(e["icon"]).is_file() for e in rec["exe"])


@pytest.mark.parametrize("spec,icon", [("builder", "icon_builder.ico"), ("server", "icon_server.ico"),
                                        ("client", "icon_client.ico")])
def test_separate_specs_reference_icons(spec, icon):
    text = (ROOT / "build_scripts" / f"{spec}.spec").read_text(encoding="utf-8")
    assert f"icon=_icon('{icon}')" in text


# ----------------------------------------------------------------- installer.iss
@pytest.fixture(scope="module")
def iss():
    raw = ISS.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "installer.iss должен быть UTF-8 с BOM (кириллица)"
    return raw.decode("utf-8-sig")


def sections(text):
    result, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^\[(\w+)\]\s*$", line)
        if m:
            current = m.group(1)
            result[current] = []
        elif current and line.strip() and not line.lstrip().startswith(";"):
            result[current].append(line)
    return result


def test_iss_setup_section_values(iss):
    setup = "\n".join(sections(iss)["Setup"])
    assert '#define MyAppName "АвтоПрайс"' in iss and '#define MyAppVersion "1.0.0"' in iss
    for expected in ("AppName={#MyAppName}", "AppVersion={#MyAppVersion}", r"DefaultDirName={autopf}\APrice",
                     "DefaultGroupName={#MyAppName}", r"OutputDir=..\dist",
                     "OutputBaseFilename=APrice_Setup_v{#MyAppVersion}", "Compression=lzma2/ultra64"):
        assert expected in setup, expected


def test_iss_installs_whole_dist_without_runtime_dirs(iss):
    files = "\n".join(sections(iss)["Files"])
    assert r'Source: "..\dist\*"' in files and "recursesubdirs" in files
    for excluded in ("config\\*", "logs\\*", "data\\*", "browser\\*", "APrice_Setup*.exe"):
        assert excluded in files


def test_iss_shortcuts(iss):
    icons = "\n".join(sections(iss)["Icons"])
    desktop = [l for l in icons.splitlines() if "{autodesktop}" in l]
    assert len(desktop) == 1 and "АвтоПрайс — Оценка авто" in desktop[0]
    assert "APrice-Client.exe" in desktop[0] or "{#ClientExe}" in desktop[0]
    assert "icon_client.ico" in desktop[0] and "Tasks: desktopicon" in desktop[0]
    start = [l for l in icons.splitlines() if "{group}" in l]
    assert any("{#ClientExe}" in l for l in start) and any("{#BuilderExe}" in l for l in start)


def test_iss_optional_run_after_install(iss):
    run = "\n".join(sections(iss)["Run"])
    assert "{#ClientExe}" in run and "postinstall" in run and "Запустить АвтоПрайс" in run
    assert "skipifsilent" in run


def test_iss_runtime_dirs_writable_for_users(iss):
    dirs = "\n".join(sections(iss)["Dirs"])
    for name in ("config", "logs", "data", "browser"):
        assert rf'{{app}}\{name}"; Permissions: users-modify' in dirs


def test_iss_referenced_assets_exist(iss):
    for rel in re.findall(r'Source: "(assets\\[^"]+)"', iss):
        assert (ROOT / "build_scripts" / rel.replace("\\", "/")).is_file(), rel
    assert "SetupIconFile=assets\\icon_client.ico" in iss
    for exe in EXES:
        assert exe in iss.replace("{#ClientExe}", "APrice-Client").replace("{#BuilderExe}", "APrice-Builder") \
            .replace("{#ServerExe}", "APrice-Server")


# ----------------------------------------------------------------- build.py
@pytest.fixture
def build(monkeypatch):
    monkeypatch.delenv("APRICE_NO_INSTALLER", raising=False)  # здесь установщик как раз проверяем
    return load(ROOT / "build.py", "build_61")


def test_env_var_disables_installer(tmp_path, monkeypatch):
    build = load(ROOT / "build.py", "build_61_env")
    (tmp_path / "build_scripts").mkdir()
    monkeypatch.setenv("APRICE_NO_INSTALLER", "1")
    monkeypatch.setattr(build.subprocess, "run", _fake_pyinstaller(tmp_path, []))
    monkeypatch.setattr(build, "find_iscc", lambda *a: pytest.fail("ISCC не должен искаться"))
    assert build.main(root=tmp_path) == 0


def test_find_iscc_standard_and_path(build, tmp_path, monkeypatch):
    fake = tmp_path / "ISCC.exe"
    fake.write_bytes(b"")
    assert build.find_iscc((str(fake),)) == str(fake)
    monkeypatch.setattr(build.shutil, "which", lambda n: None)
    assert build.find_iscc((str(tmp_path / "no.exe"),)) is None
    monkeypatch.setattr(build.shutil, "which", lambda n: "/x/ISCC" if n == "ISCC" else None)
    assert build.find_iscc(()) == "/x/ISCC"


def test_installer_version_read_from_script(build):
    assert build.installer_version(ISS) == "1.0.0"
    assert build.installer_version(ISS.with_name("nope.iss")) == "?"


def test_build_installer_skipped_without_iscc(build, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(build, "find_iscc", lambda *a: None)
    assert build.build_installer(tmp_path) == 0
    out = capsys.readouterr().out
    assert "Inno Setup не найден" in out and "jrsoftware.org" in out


def test_build_installer_runs_iscc(build, tmp_path, capsys, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "build_scripts" / "installer.iss").write_text('#define MyAppVersion "2.5.0"', encoding="utf-8")
    calls = []
    monkeypatch.setattr(build.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0))
    assert build.build_installer(tmp_path, iscc="C:/ISCC.exe") == 0
    assert calls[0][0] == ["C:/ISCC.exe", str(tmp_path / "build_scripts" / "installer.iss")]
    assert "APrice_Setup_v2.5.0.exe" in capsys.readouterr().out


def test_build_installer_failure_returns_code(build, tmp_path, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "build_scripts" / "installer.iss").write_text("", encoding="utf-8")
    monkeypatch.setattr(build.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2))
    assert build.build_installer(tmp_path, iscc="x") == 2


def _fake_pyinstaller(tmp_path, order):
    def fake_run(cmd, **kw):
        name = Path(cmd[1]).name if (cmd[0] == "ISCC" or Path(cmd[1]).name.endswith(".py")) else Path(cmd[3]).name
        order.append(name)
        if name == "aprice.spec":
            out = tmp_path / "dist" / "APrice"
            (out / "_internal").mkdir(parents=True, exist_ok=True)
            for n in EXES:
                (out / f"{n}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(cmd, 0)
    return fake_run


def test_main_generates_icons_first_then_builds_then_installer(build, tmp_path, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "generate_icons.py").write_text("", encoding="utf-8")
    order: list = []
    monkeypatch.setattr(build.subprocess, "run", _fake_pyinstaller(tmp_path, order))
    monkeypatch.setattr(build, "find_iscc", lambda *a: "ISCC")
    (tmp_path / "build_scripts" / "installer.iss").write_text("", encoding="utf-8")
    assert build.main(root=tmp_path) == 0
    assert order == ["generate_icons.py", "aprice.spec", "installer.iss"]


def test_main_continues_when_icon_generation_fails(build, tmp_path, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "generate_icons.py").write_text("", encoding="utf-8")
    inner = _fake_pyinstaller(tmp_path, [])

    def run(cmd, **kw):
        if Path(cmd[1]).name == "generate_icons.py":
            return subprocess.CompletedProcess(cmd, 1)
        return inner(cmd, **kw)

    monkeypatch.setattr(build.subprocess, "run", run)
    monkeypatch.setattr(build, "find_iscc", lambda *a: None)
    assert build.main(root=tmp_path) == 0


def test_main_no_installer_flag_and_separate_skip_iscc(build, tmp_path, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    order: list = []
    monkeypatch.setattr(build.subprocess, "run", _fake_pyinstaller(tmp_path, order))
    monkeypatch.setattr(build, "find_iscc", lambda *a: pytest.fail("ISCC не должен искаться"))
    assert build.main(root=tmp_path, installer=False) == 0
    assert build.main(root=tmp_path, separate=True) in (0, 1)  # separate: без установщика (exe-заглушек нет)


def test_main_fails_when_iscc_fails(build, tmp_path, monkeypatch):
    (tmp_path / "build_scripts").mkdir()
    (tmp_path / "build_scripts" / "installer.iss").write_text("", encoding="utf-8")
    inner = _fake_pyinstaller(tmp_path, [])
    monkeypatch.setattr(build.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 5) if cmd[0] == "ISCC" else inner(cmd, **kw))
    monkeypatch.setattr(build, "find_iscc", lambda *a: "ISCC")
    assert build.main(root=tmp_path) == 5
