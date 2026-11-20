"""5.6: зеркало для загрузки Chromium с откатом на официальный CDN."""

from __future__ import annotations

import subprocess

import pytest

from server import browser_check as bc


def cp(argv, code, out=""):
    return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def test_default_is_npmmirror():
    assert bc.mirror_url({}) == "https://npmmirror.com/mirrors/playwright"


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "official", "cdn"])
def test_mirror_can_be_disabled(value):
    assert bc.mirror_url({bc.MIRROR_ENV: value}) is None


def test_custom_mirror_url():
    assert bc.mirror_url({bc.MIRROR_ENV: " https://my.mirror/pw "}) == "https://my.mirror/pw"


@pytest.mark.parametrize("name", ["PLAYWRIGHT_DOWNLOAD_HOST", "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST"])
def test_users_own_playwright_host_wins(name):
    assert bc.mirror_url({name: "https://corp.mirror"}) is None


def test_build_command_sets_download_host_only_with_mirror(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    _argv, env = bc.build_install_command("https://m/pw")
    assert env["PLAYWRIGHT_DOWNLOAD_HOST"] == "https://m/pw"
    _argv, env = bc.build_install_command(None)
    assert "PLAYWRIGHT_DOWNLOAD_HOST" not in env


def test_install_uses_mirror_first(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    hosts = []

    def runner(argv, **kw):
        hosts.append(kw["env"].get("PLAYWRIGHT_DOWNLOAD_HOST"))
        return cp(argv, 0)

    ok, _ = bc.install_chromium(runner, environ={})
    assert ok is True and hosts == [bc.DEFAULT_MIRROR_URL]  # один запуск, без отката


def test_install_falls_back_to_official_cdn_when_mirror_fails(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    hosts = []

    def runner(argv, **kw):
        host = kw["env"].get("PLAYWRIGHT_DOWNLOAD_HOST")
        hosts.append(host)
        return cp(argv, 1 if host else 0, "mirror 404" if host else "ok")

    ok, _ = bc.install_chromium(runner, environ={})
    assert ok is True and hosts == [bc.DEFAULT_MIRROR_URL, None]


def test_install_failure_on_both_reports_both_outputs(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    outs = iter(["mirror down", "cdn down"])
    ok, details = bc.install_chromium(lambda argv, **kw: cp(argv, 1, next(outs)), environ={})
    assert ok is False and "mirror down" in details and "cdn down" in details


def test_disabled_mirror_runs_once_on_official_cdn(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    calls = []

    def runner(argv, **kw):
        calls.append(kw["env"].get("PLAYWRIGHT_DOWNLOAD_HOST"))
        return cp(argv, 1, "fail")

    ok, _ = bc.install_chromium(runner, environ={bc.MIRROR_ENV: "off"})
    assert ok is False and calls == [None]  # без зеркала повторять нечего


def test_oserror_on_mirror_also_falls_back(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    calls = []

    def runner(argv, **kw):
        calls.append(kw["env"].get("PLAYWRIGHT_DOWNLOAD_HOST"))
        if len(calls) == 1:
            raise OSError("нет сети")
        return cp(argv, 0)

    assert bc.install_chromium(runner, environ={})[0] is True and len(calls) == 2
