import hashlib
import os
from datetime import datetime, timezone
from unittest import mock

import pytest

from backups import pg_backup as pb


# ---------------------------------------------------------------- hour gate
@pytest.mark.parametrize("utc, expected", [
    (datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc), True),    # CDT 03:05
    (datetime(2026, 7, 1, 9, 5, tzinfo=timezone.utc), False),   # CDT 04:05
    (datetime(2026, 12, 1, 9, 5, tzinfo=timezone.utc), True),   # CST 03:05
    (datetime(2026, 12, 1, 8, 5, tzinfo=timezone.utc), False),  # CST 02:05
    # Nov 1 2026: DST ends at 07:00Z, so 08:05Z = 02:05 CST and 09:05Z = 03:05 CST.
    (datetime(2026, 11, 1, 8, 5, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 1, 9, 5, tzinfo=timezone.utc), True),
    # Mar 8 2026: DST starts at 08:00Z, so 08:05Z = 03:05 CDT.
    (datetime(2026, 3, 8, 8, 5, tzinfo=timezone.utc), True),
    (datetime(2026, 3, 8, 9, 5, tzinfo=timezone.utc), False),
])
def test_should_run(utc, expected):
    assert pb.should_run(utc, 3) is expected


def test_exactly_one_run_per_day_all_year():
    from datetime import timedelta
    d = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(366):
        runs = [pb.should_run(d.replace(hour=h, minute=5), 3) for h in (8, 9)]
        assert sum(runs) == 1, d
        d += timedelta(days=1)


def test_main_exits_zero_outside_window():
    with mock.patch.object(pb, "run_backup") as rb:
        rc = pb.main([], now_utc=datetime(2026, 7, 1, 9, 5, tzinfo=timezone.utc))
    assert rc == 0
    rb.assert_not_called()


# ---------------------------------------------------------------- retention
def _f(name, path=None):
    return {".tag": "file", "name": name, "path_display": path or f"/{name}"}


def test_retention_selection():
    now = datetime(2026, 11, 10, 9, 5, tzinfo=timezone.utc)
    entries = [
        _f("2026-08-01T09_05Z.dump"), _f("2026-08-01T09_05Z.json"),        # old -> delete
        _f("2026-09-10T09_05Z.dump"),                                       # 61 days -> delete
        _f("2026-09-12T08_05Z.dir.tar.gz"),                                 # 59 days -> keep
        _f("2026-11-09T09_05Z.dump"), _f("2026-11-09T09_05Z.json"),        # new -> keep
        _f("notes.txt"), _f("2020-01-01T00_00Z.dump.bak"),                  # not pattern
        {".tag": "folder", "name": "2020-01-01T00_00Z.dump", "path_display": "/2020-01-01T00_00Z.dump"},
        _f("2020-01-01T00_00Z.dump", "/sub/2020-01-01T00_00Z.dump"),        # not top level
    ]
    out = pb.select_for_deletion(entries, now, retention_days=60, min_keep=2)
    assert out == ["/2026-08-01T09_05Z.dump", "/2026-08-01T09_05Z.json", "/2026-09-10T09_05Z.dump"]


def test_retention_min_keep_protects_old_dumps():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    entries = [_f("2026-01-01T09_05Z.dump"), _f("2026-01-01T09_05Z.json"),
               _f("2026-01-02T09_05Z.dump")]
    assert pb.select_for_deletion(entries, now, 60, min_keep=7) == []
    assert pb.select_for_deletion(entries, now, 60, min_keep=1) == [
        "/2026-01-01T09_05Z.dump", "/2026-01-01T09_05Z.json"]


# ---------------------------------------------------------------- content hash
def test_dropbox_content_hash(tmp_path):
    data = os.urandom(pb.DROPBOX_BLOCK * 2 + 123)
    p = tmp_path / "x.bin"
    p.write_bytes(data)
    blocks = [data[i:i + pb.DROPBOX_BLOCK] for i in range(0, len(data), pb.DROPBOX_BLOCK)]
    expected = hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in blocks)).hexdigest()
    assert pb.dropbox_content_hash(str(p)) == expected


def test_dropbox_content_hash_empty(tmp_path):
    p = tmp_path / "e"
    p.write_bytes(b"")
    assert pb.dropbox_content_hash(str(p)) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------- row counting
def test_count_rows_in_sql():
    sql = [
        "SET x;\n",
        "COPY public.customers (id, name) FROM stdin;\n", "1\ta\n", "2\tb\n", "\\.\n",
        "COPY public.optin_signups (id) FROM stdin;\n", "\\.\n",
    ]
    assert pb.count_rows_in_sql(sql) == {"customers": 2, "optin_signups": 0}
    with pytest.raises(pb.BackupError):
        pb.check_required_counts({"customers": 2, "optin_signups": 0}, ["customers", "optin_signups"])


# ---------------------------------------------------------------- failure path
def test_failure_exits_nonzero_and_alerts(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:supersecret@host/db")
    boom = pb.BackupError("pg_dump failed: connection to postgresql://u:supersecret@host/db refused")
    with mock.patch.object(pb, "run_backup", side_effect=boom), \
         mock.patch.object(pb, "send_alert", return_value=False) as alert:
        rc = pb.main(["--force"], now_utc=datetime(2026, 11, 10, 9, 5, tzinfo=timezone.utc))
    assert rc == 1
    alert.assert_called_once()
    assert "supersecret" not in alert.call_args[0][1]


def test_send_alert_without_smtp_is_noop(monkeypatch):
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)
    with mock.patch("smtplib.SMTP") as smtp:
        assert pb.send_alert("s", "b") is False
    smtp.assert_not_called()


def test_hash_mismatch_fails(monkeypatch, tmp_path):
    """Upload verification failure must raise (=> nonzero exit)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    dump = tmp_path / "d.dump"
    dump.write_bytes(b"data")
    fake = mock.Mock()
    fake.upload.return_value = {"path_lower": "/d.dump"}
    fake.metadata.return_value = {"content_hash": "wrong", "size": 4}
    with mock.patch.object(pb, "check_pg_tools", return_value="pg_dump (PostgreSQL) 17.6"), \
         mock.patch.object(pb, "export_pg_dump", return_value=str(dump)), \
         mock.patch.object(pb, "verify_dump", return_value=({"customers": 1, "optin_signups": 1}, {})):
        with pytest.raises(pb.BackupError, match="mismatch"):
            pb.run_backup(datetime(2026, 11, 10, 9, 5, tzinfo=timezone.utc), dropbox=fake)
    fake.delete.assert_not_called()


def test_happy_path_mocked(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    dump = tmp_path / "2026-11-10T09_05Z.dump"
    dump.write_bytes(b"x" * 100)
    fake = mock.Mock()
    uploads = {}

    def up(local, dest):
        uploads[dest] = pb.dropbox_content_hash(local)
        return {"path_lower": dest, "content_hash": uploads[dest]}
    fake.upload.side_effect = up
    fake.metadata.side_effect = lambda p: {"content_hash": uploads[p], "size": 100}
    fake.list_root.return_value = [_f("2026-01-01T09_05Z.dump")] + [
        _f(f"2026-11-0{i}T09_05Z.dump") for i in range(1, 9)]
    with mock.patch.object(pb, "check_pg_tools", return_value="17"), \
         mock.patch.object(pb, "export_pg_dump", return_value=str(dump)), \
         mock.patch.object(pb, "verify_dump", return_value=({"customers": 5, "optin_signups": 1}, {})):
        res = pb.run_backup(datetime(2026, 11, 10, 9, 5, tzinfo=timezone.utc), dropbox=fake,
                            list_only_retention=True)
    assert res["retention_candidates"] == ["/2026-01-01T09_05Z.dump"]
    fake.delete.assert_not_called()
    assert set(uploads) == {"/2026-11-10T09_05Z.dump", "/2026-11-10T09_05Z.json"}


def test_upload_session_used_for_big_files(tmp_path):
    p = tmp_path / "big"
    p.write_bytes(b"a" * (pb.UPLOAD_CHUNK * 2 + 5))
    d = pb.Dropbox.__new__(pb.Dropbox)
    calls = []

    def content(endpoint, arg, data):
        calls.append((endpoint, len(data)))
        return {"session_id": "S"} if endpoint.endswith("start") else {}
    d._content = content
    d.upload(str(p), "/big")
    assert [c[0] for c in calls] == ["files/upload_session/start", "files/upload_session/append_v2",
                                     "files/upload_session/finish"]
    assert sum(c[1] for c in calls) == pb.UPLOAD_CHUNK * 2 + 5


def test_archive_versions():
    toc = ";\n; Dumped from database version: 17.6 (Debian 17.6-1.pgdg120+1)\n; Dumped by pg_dump version: 17.6\n"
    assert pb.archive_versions(toc) == {"server_version": "17.6", "dumped_by": "17.6"}
