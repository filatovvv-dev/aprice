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


# ----------------------------------------------------------------------
# 4.5: умное обновление — проверка date.txt
# ----------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

DATE_URL = "https://mirror.test/date.txt"


class TextResponse(FakeResponse):
    def __init__(self, text, status=200):
        super().__init__(b"", status=status)
        self.text = text


def local_db(tmp_path, when: datetime, name="aprice.db") -> Path:
    path = tmp_path / name
    path.write_bytes(b"x")
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    return path


LOCAL = datetime(2026, 11, 17, 12, 0, 0)


def test_check_for_updates_same_date(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse("2026-11-17T12:00:00"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (False, None)


def test_check_for_updates_newer_remote(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse("2026-11-17T12:00:01\n"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (True, None)


def test_check_for_updates_older_remote(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse("2026-11-16T23:59:59"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (False, None)


def test_check_for_updates_missing_local_db_needs_download(tmp_path):
    get = FakeGet(TextResponse("2020-01-01T00:00:00"))
    assert db_update.check_for_updates(DATE_URL, tmp_path / "none.db", get=get) == (True, None)


def test_check_for_updates_handles_timezones_and_bom(tmp_path):
    db = local_db(tmp_path, datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None))
    same = FakeGet(TextResponse("﻿2026-11-17T15:00:00+03:00\r\n"))  # == 12:00 UTC
    assert db_update.check_for_updates(DATE_URL, db, get=same) == (False, None)
    newer = FakeGet(TextResponse("2026-11-17T15:00:01+03:00"))
    assert db_update.check_for_updates(DATE_URL, db, get=newer) == (True, None)


def test_check_for_updates_prefers_metadata_last_update_over_mtime(tmp_path):
    db = tmp_path / "aprice.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO metadata VALUES ('last_update', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()  # mtime = «сейчас» (свежий файл), но метка в базе — старая
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("2026-06-01T00:00:00"))) == (True, None)


def test_check_for_updates_resolves_yandex_public_link(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(FakeResponse(json_data={"href": "https://downloader.disk.test/date"}),
                  TextResponse("2027-01-01T00:00:00"))
    assert db_update.check_for_updates("https://disk.yandex.ru/i/0AjHf2zhnKITrA", db, get=get) == (True, None)
    assert get.urls[0].startswith(db_update.YANDEX_API)
    assert get.urls[1] == "https://downloader.disk.test/date"


@pytest.mark.parametrize("body", ["", "   \n", "<!DOCTYPE html><html>captcha</html>", "вчера", "32.13.2026"])
def test_check_for_updates_unrecognized_date_means_no_update(tmp_path, body, caplog):
    """5.5: нераспознанная дата -> warning в лог и «обновление не требуется»."""
    db = local_db(tmp_path, LOCAL)
    with caplog.at_level("WARNING", logger="aprice.client.db_update"):
        assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse(body))) == (False, None)
    assert any("Дата обновления не распознана" in r.message or "пуст" in r.message for r in caplog.records)


def test_check_for_updates_rejects_oversized_page(tmp_path):
    db = local_db(tmp_path, LOCAL)
    with pytest.raises(DbUpdateError):
        db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("x" * 5000)))


def test_check_for_updates_network_error_and_bad_url(tmp_path):
    db = local_db(tmp_path, LOCAL)
    with pytest.raises(DbUpdateError):
        db_update.check_for_updates(DATE_URL, db, get=FakeGet(requests.ConnectionError("нет сети")))
    with pytest.raises(DbUpdateError):
        db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("", status=404)))
    with pytest.raises(DbUpdateError):
        db_update.check_for_updates("не ссылка", db, get=FakeGet())


def test_download_sets_mtime_to_publication_date(target, mirror):
    ts = datetime(2026, 11, 17, 12, 0, 0).timestamp()
    download_database(URL, target, get=FakeGet(FakeResponse(mirror)), mtime=ts)
    assert abs(target.stat().st_mtime - ts) < 1


def test_check_for_updates_uses_first_line_of_date_file(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse("2026-11-17T12:00:00\n2030-01-01T00:00:00\n"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (False, None)


# ----------------------------------------------------------------------
# 5.5: parse_date — DD.MM.YYYY и ISO 8601
# ----------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("17.11.2026", datetime(2026, 11, 17)),
    ("17.11.2026 12:30:45", datetime(2026, 11, 17, 12, 30, 45)),
    ("  17.11.2026 \r\n", datetime(2026, 11, 17)),
    ("\ufeff17.11.2026", datetime(2026, 11, 17)),
    ("01.02.2026", datetime(2026, 2, 1)),  # день.месяц, а не месяц.день
    ("2026-11-17T12:00:00", datetime(2026, 11, 17, 12)),
    ("2026-11-17 12:00:00", datetime(2026, 11, 17, 12)),
    ("2026-11-17", datetime(2026, 11, 17)),
    ("17.11.2026\n2030-01-01", datetime(2026, 11, 17)),  # берётся первая строка
])
def test_parse_date_formats(text, expected):
    assert db_update.parse_date(text) == expected


def test_parse_date_iso_with_offset_and_z():
    got = db_update.parse_date("2026-11-17T15:00:00+03:00")
    assert got.utcoffset() == timedelta(hours=3) and got.hour == 15
    assert db_update.parse_date("2026-11-17T12:00:00Z").utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", ["", "  ", "\n\n", "вчера", "2026/11/17", "17-11-2026", "32.01.2026",
                                  "17.13.2026", "<html>", "17.11.26"])
def test_parse_date_unrecognized_returns_none_and_warns(text, caplog):
    with caplog.at_level("WARNING", logger="aprice.client.db_update"):
        assert db_update.parse_date(text) is None
    assert caplog.records  # warning записан


def test_check_for_updates_with_russian_date(tmp_path):
    db = local_db(tmp_path, datetime(2026, 11, 17, 0, 0, 0))
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("17.11.2026"))) == (False, None)
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("18.11.2026"))) == (True, None)
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("16.11.2026"))) == (False, None)
    assert db_update.check_for_updates(
        DATE_URL, db, get=FakeGet(TextResponse("17.11.2026 00:00:01"))) == (True, None)


def test_metadata_last_update_may_be_russian_date(tmp_path):
    db = tmp_path / "aprice.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO metadata VALUES ('last_update', '01.01.2026')")
    conn.commit()
    conn.close()
    assert db_update.local_db_date(db) == datetime(2026, 1, 1)


# ----------------------------------------------------------------------
# 5.5 (манифест): вторая строка date.txt — актуальная ссылка на базу
# ----------------------------------------------------------------------
NEW_URL = "https://new.url/db"


def test_manifest_with_url(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse(f"18.11.2026\n{NEW_URL}"))  # локальная — 17.11.2026 12:00 → новее
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (True, NEW_URL)


def test_manifest_without_url(tmp_path):
    db = local_db(tmp_path, LOCAL)
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse("18.11.2026"))) == (True, None)
    assert db_update.check_for_updates(
        DATE_URL, db, get=FakeGet(TextResponse("18.11.2026\n   \n"))) == (True, None)  # пустая 2-я строка


def test_manifest_same_date(tmp_path):
    db = local_db(tmp_path, datetime(2026, 11, 17))
    get = FakeGet(TextResponse(f"17.11.2026\n{NEW_URL}"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (False, None)  # ссылку не отдаём
    older = FakeGet(TextResponse(f"16.11.2026\n{NEW_URL}"))
    assert db_update.check_for_updates(DATE_URL, db, get=older) == (False, None)


@pytest.mark.parametrize("text", ["мусор\n" + NEW_URL, "", "<html>x</html>", "32.13.2026\n" + NEW_URL])
def test_manifest_invalid_date(tmp_path, text):
    db = local_db(tmp_path, LOCAL)
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse(text))) == (False, None)


@pytest.mark.parametrize("second", ["не ссылка", "ftp://x/db", "javascript:alert(1)", "//x/db", "http://"])
def test_manifest_second_line_must_be_http_url(tmp_path, second, caplog):
    db = local_db(tmp_path, LOCAL)
    with caplog.at_level("WARNING", logger="aprice.client.db_update"):
        got = db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse(f"18.11.2026\n{second}")))
    assert got == (True, None)  # нельзя «подсунуть» не-ссылку: запасной db_update_url
    assert any("не ссылка" in r.message for r in caplog.records)


def test_manifest_tolerates_bom_blank_lines_crlf_and_extra_lines(tmp_path):
    db = local_db(tmp_path, LOCAL)
    text = f"\ufeff\r\n18.11.2026\r\n\r\n  {NEW_URL}  \r\nлишняя строка\r\n"
    assert db_update.check_for_updates(DATE_URL, db, get=FakeGet(TextResponse(text))) == (True, NEW_URL)


def test_manifest_with_iso_date_and_url(tmp_path):
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse(f"2026-11-18T09:00:00\n{NEW_URL}"))
    assert db_update.check_for_updates(DATE_URL, db, get=get) == (True, NEW_URL)


def test_manifest_url_is_resolved_via_yandex_for_date_file_only(tmp_path):
    """Ссылка на базу из манифеста не запрашивается при проверке — только date.txt."""
    db = local_db(tmp_path, LOCAL)
    get = FakeGet(TextResponse(f"18.11.2026\n{NEW_URL}"))
    db_update.check_for_updates(DATE_URL, db, get=get)
    assert get.urls == [DATE_URL]


def test_parse_manifest_fields():
    m = db_update.parse_manifest(f"17.11.2026\n{NEW_URL}")
    assert m.date == datetime(2026, 11, 17) and m.db_url == NEW_URL
    assert db_update.parse_manifest("17.11.2026") == db_update.Manifest(datetime(2026, 11, 17), None)
    bad = db_update.parse_manifest(f"мусор\n{NEW_URL}")
    assert bad.date is None and bad.db_url == NEW_URL  # URL нужен первому запуску даже при мусорной дате


# ----------------------------------------------------------------------
# 5.7: классификация сетевых ошибок
# ----------------------------------------------------------------------
@pytest.mark.parametrize("exc", [
    requests.ConnectionError("нет сети"), requests.Timeout("таймаут"), ConnectionResetError("обрыв"),
    TimeoutError("таймаут сокета"),
])
def test_network_failures_become_db_network_error(tmp_path, exc):
    with pytest.raises(db_update.DbNetworkError):
        db_update.download_database("https://mirror.test/a.db", tmp_path / "a.db", get=FakeGet(exc))
    with pytest.raises(db_update.DbNetworkError):
        db_update.check_for_updates("https://mirror.test/date.txt", tmp_path / "a.db", get=FakeGet(exc))


def test_network_failure_while_resolving_yandex_link(tmp_path):
    with pytest.raises(db_update.DbNetworkError):
        db_update.download_database("https://disk.yandex.ru/d/abc", tmp_path / "a.db",
                                    get=FakeGet(requests.ConnectionError("нет сети")))


def test_connection_drop_midstream_is_network_error(tmp_path):
    class Dropping(FakeResponse):
        def iter_content(self, chunk_size=1):
            yield b"abc"
            raise ConnectionResetError("обрыв посреди загрузки")

    with pytest.raises(db_update.DbNetworkError):
        db_update.download_database("https://mirror.test/a.db", tmp_path / "a.db", get=FakeGet(Dropping(b"abc")))
    assert not list(tmp_path.glob("*.download"))


def test_http_error_is_plain_update_error(tmp_path):
    class Gone(FakeResponse):
        def raise_for_status(self):
            raise requests.HTTPError("404")

    with pytest.raises(db_update.DbUpdateError) as info:
        db_update.download_database("https://mirror.test/a.db", tmp_path / "a.db", get=FakeGet(Gone(b"")))
    assert not isinstance(info.value, db_update.DbNetworkError)


def test_thread_flags_network_error(qapp, tmp_path):
    from client.settings_dialog import DbUpdateThread

    def downloader(url, target, **kw):
        raise db_update.DbNetworkError("нет сети")

    t = DbUpdateThread("https://m/a.db", tmp_path / "a.db", downloader=downloader)
    got = []
    t.failed.connect(got.append)
    t.run()
    assert t.network_error is True and got == ["нет сети"]

    def plain(url, target, **kw):
        raise db_update.DbUpdateError("битый файл")

    t2 = DbUpdateThread("https://m/a.db", tmp_path / "a.db", downloader=plain)
    t2.run()
    assert t2.network_error is False
