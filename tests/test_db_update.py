"""Тесты client.db_update (4.4): скачивание, проверка целостности, бэкап. Без сети."""

import os
import sqlite3
from pathlib import Path

import pytest
import requests

from client import db_update
from client.db_update import DbUpdateError, db_age_days, download_database, validate_database


def make_db(path: Path, manufacturers=("BMW", "Audi"), tables=True, extra_rows=0) -> bytes:
    conn = sqlite3.connect(path)
    if tables:
        conn.execute("CREATE TABLE manufacturers (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE models (id INTEGER PRIMARY KEY, manufacturer_id INTEGER, name TEXT)")
        conn.executemany("INSERT INTO manufacturers(name) VALUES (?)", [(m,) for m in manufacturers])
        conn.executemany("INSERT INTO models(manufacturer_id, name) VALUES (1, ?)",
                         [(f"M{i}",) for i in range(extra_rows)])
    else:
        conn.execute("CREATE TABLE other (x)")
    conn.commit()
    conn.close()
    return path.read_bytes()


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, json_data=None, chunk=1000):
        self._body, self.status, self.headers = body, status, headers or {}
        self._json, self._chunk = json_data, chunk

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Error")

    def iter_content(self, chunk_size=None):
        for i in range(0, len(self._body), self._chunk):
            yield self._body[i:i + self._chunk]

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeGet:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def mirror(tmp_path) -> bytes:
    return make_db(tmp_path / "mirror.db", ("BMW", "Audi", "Lada"), extra_rows=300)


@pytest.fixture
def target(tmp_path) -> Path:
    path = tmp_path / "data" / "aprice.db"
    path.parent.mkdir()
    make_db(path, ("OLD",))
    return path


def names(path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM manufacturers ORDER BY id")]
    finally:
        conn.close()


URL = "https://example.test/aprice.db"


def leftovers(target: Path) -> list[str]:
    return sorted(p.name for p in target.parent.iterdir())


def test_download_replaces_database_and_keeps_backup(target, mirror):
    get = FakeGet(FakeResponse(mirror, headers={"Content-Length": str(len(mirror))}))
    result = download_database(URL, target, get=get)
    assert names(target) == ["BMW", "Audi", "Lada"]
    assert result.manufacturers == 3 and result.size == len(mirror) and result.path == target
    assert result.backup == target.with_name("aprice.db.bak")
    assert names(result.backup) == ["OLD"]  # резервная копия — прежняя база
    assert leftovers(target) == ["aprice.db", "aprice.db.bak"]  # временного файла нет
    assert get.urls == [URL]


def test_download_without_existing_database_has_no_backup(tmp_path, mirror):
    target = tmp_path / "fresh" / "aprice.db"
    result = download_database(URL, target, get=FakeGet(FakeResponse(mirror)))
    assert result.backup is None and names(target) == ["BMW", "Audi", "Lada"]


def test_uses_requests_get_by_default(target, mirror, monkeypatch):
    fake = FakeGet(FakeResponse(mirror))
    monkeypatch.setattr(db_update.requests, "get", fake)
    download_database(URL, target)
    assert fake.urls == [URL] and names(target)[0] == "BMW"


def test_default_target_is_constants_db_file(tmp_path, mirror, monkeypatch):
    path = tmp_path / "aprice.db"
    monkeypatch.setattr(db_update.constants, "DB_FILE", path)
    download_database(URL, get=FakeGet(FakeResponse(mirror)))
    assert path.exists()


def test_progress_is_reported(target, mirror):
    seen = []
    download_database(URL, target, get=FakeGet(FakeResponse(
        mirror, headers={"Content-Length": str(len(mirror))}, chunk=500)),
        progress=lambda done, total: seen.append((done, total)))
    assert seen and seen[-1] == (len(mirror), len(mirror))
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)


def test_html_instead_of_database_is_rejected_and_target_untouched(target):
    with pytest.raises(DbUpdateError, match="не является базой SQLite"):
        download_database(URL, target, get=FakeGet(FakeResponse(b"<html>Not found</html>")))
    assert names(target) == ["OLD"] and leftovers(target) == ["aprice.db"]


def test_corrupted_database_is_rejected(target, tmp_path):
    good = make_db(tmp_path / "g.db", ("BMW",), extra_rows=2000)
    broken = bytearray(good)
    for offset in range(8192, len(broken) - 100, 997):  # порча страниц данных, заголовок цел
        broken[offset:offset + 40] = b"\xff" * 40
    with pytest.raises(DbUpdateError, match="целостности|не читается"):
        download_database(URL, target, get=FakeGet(FakeResponse(bytes(broken))))
    assert names(target) == ["OLD"] and leftovers(target) == ["aprice.db"]
    assert not target.with_name("aprice.db.bak").exists()


def test_database_without_reference_tables_is_rejected(target, tmp_path):
    body = make_db(tmp_path / "x.db", tables=False)
    with pytest.raises(DbUpdateError, match="нет таблиц справочника"):
        download_database(URL, target, get=FakeGet(FakeResponse(body)))
    assert names(target) == ["OLD"]


def test_empty_reference_is_rejected(target, tmp_path):
    body = make_db(tmp_path / "e.db", manufacturers=())
    with pytest.raises(DbUpdateError, match="пуста"):
        download_database(URL, target, get=FakeGet(FakeResponse(body)))
    assert names(target) == ["OLD"]


@pytest.mark.parametrize("failure", [
    FakeResponse(status=404),
    requests.ConnectionError("нет сети"),
    requests.Timeout("таймаут"),
])
def test_network_failure_keeps_target(target, failure):
    with pytest.raises(DbUpdateError, match="Не удалось скачать"):
        download_database(URL, target, get=FakeGet(failure))
    assert names(target) == ["OLD"] and leftovers(target) == ["aprice.db"]


@pytest.mark.parametrize("url", ["", "   ", "ftp://example.test/a.db", "example.test/a.db", None])
def test_bad_url_is_rejected_without_request(target, url):
    get = FakeGet()
    with pytest.raises(DbUpdateError, match="Укажите ссылку"):
        download_database(url, target, get=get)
    assert get.urls == []


def test_oversized_download_is_rejected(target, mirror, monkeypatch):
    monkeypatch.setattr(db_update, "MAX_BYTES", 100)
    with pytest.raises(DbUpdateError, match="слишком большой"):
        download_database(URL, target, get=FakeGet(FakeResponse(mirror)))
    assert names(target) == ["OLD"] and leftovers(target) == ["aprice.db"]
    with pytest.raises(DbUpdateError, match="слишком большой"):
        download_database(URL, target, get=FakeGet(FakeResponse(
            mirror, headers={"Content-Length": "999999"})))


def test_replace_failure_keeps_target_and_cleans_tmp(target, mirror, monkeypatch):
    def boom(src, dst):
        raise PermissionError("файл занят")
    monkeypatch.setattr(db_update.os, "replace", boom)
    with pytest.raises(DbUpdateError, match="занят"):
        download_database(URL, target, get=FakeGet(FakeResponse(mirror)))
    assert names(target) == ["OLD"]
    assert leftovers(target) == ["aprice.db", "aprice.db.bak"]  # tmp убран; бэкап безвреден


def test_yandex_disk_public_link_is_resolved_first(target, mirror):
    public = "https://disk.yandex.ru/d/AbCdEf"
    get = FakeGet(FakeResponse(json_data={"href": "https://downloader.test/file"}),
                  FakeResponse(mirror))
    download_database(public, target, get=get)
    assert get.urls[0].startswith(db_update.YANDEX_API) and "AbCdEf" in get.urls[0]
    assert get.urls[1] == "https://downloader.test/file"
    assert names(target)[0] == "BMW"


def test_yandex_disk_without_href_or_with_error(target):
    public = "https://yadi.sk/d/xyz"
    with pytest.raises(DbUpdateError, match="не вернул ссылку"):
        download_database(public, target, get=FakeGet(FakeResponse(json_data={})))
    with pytest.raises(DbUpdateError, match="Яндекс.Диска"):
        download_database(public, target, get=FakeGet(FakeResponse(status=404)))
    assert names(target) == ["OLD"]


def test_validate_database_returns_manufacturer_count(tmp_path):
    path = tmp_path / "v.db"
    make_db(path, ("A", "B", "C", "D"))
    assert validate_database(path) == 4


def test_db_age_days(tmp_path):
    path = tmp_path / "a.db"
    path.write_bytes(b"x")
    mtime = path.stat().st_mtime
    assert db_age_days(path, now=mtime + 3 * 86400 + 5) == 3
    assert db_age_days(path, now=mtime - 100) == 0
    assert db_age_days(tmp_path / "none.db") is None


def test_failed_integrity_check_verdict_is_rejected(target, mirror, monkeypatch):
    """Вердикт PRAGMA integrity_check != ok отклоняет базу, даже если файл читается."""
    real_connect = sqlite3.connect

    class Conn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                return type("R", (), {"fetchall": lambda self: [("row 7 missing from index idx",)]})()
            return self._conn.execute(sql, *args)

        def close(self):
            self._conn.close()

    monkeypatch.setattr(db_update.sqlite3, "connect", lambda *a, **k: Conn(real_connect(*a, **k)))
    with pytest.raises(DbUpdateError, match="целостности"):
        download_database(URL, target, get=FakeGet(FakeResponse(mirror)))
    assert names(target) == ["OLD"] and leftovers(target) == ["aprice.db"]
