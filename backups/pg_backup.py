"""
Nightly backup of the PestPod Render Postgres (pestpod-db) to Dropbox.

Runs as a Render Cron Job. Render cron schedules are UTC, so the job is
scheduled at 08:05 and 09:05 UTC and exits early (status 0) unless the
America/Chicago hour is 3. That gives one run at ~3:05 AM Central year-round
(CDT = UTC-5 -> 08:05 UTC, CST = UTC-6 -> 09:05 UTC).

Each run:
  1. Export pestpod-db: pg_dump -Fc over the INTERNAL DATABASE_URL (default),
     or the Render API "Postgres export" (BACKUP_SOURCE=render_export).
  2. Upload the dump plus an integrity JSON (sha256, size, row counts) to the
     Dropbox app-folder app "PestPod Backups" (paths are relative to that
     app folder).
  3. Verify: Dropbox content_hash == local Dropbox content hash, size matches,
     pg_restore --list succeeds, required tables have > 0 rows.
  4. Retention: delete backup files older than RETENTION_DAYS (default 60)
     in the app folder only, only names matching the backup pattern, and
     always keep at least MIN_KEEP_BACKUPS dumps.
  5. On any failure: log, try an alert email (only if SMTP env vars are set),
     and exit nonzero so Render's built-in "cron job failed" notification fires.

Env vars (secrets are never logged):
  DATABASE_URL                   internal connection string (pg_dump source)
  DROPBOX_BACKUP_APP_KEY         Dropbox app key
  DROPBOX_BACKUP_APP_SECRET      Dropbox app secret
  DROPBOX_BACKUP_REFRESH_TOKEN   Dropbox OAuth refresh token (offline access)
  BACKUP_SOURCE                  pg_dump (default) | render_export
  RENDER_BACKUP_API_KEY          only for BACKUP_SOURCE=render_export
  RENDER_POSTGRES_ID             only for BACKUP_SOURCE=render_export
  BACKUP_HOUR_CT                 default 3
  RETENTION_DAYS                 default 60
  MIN_KEEP_BACKUPS               default 7
  REQUIRED_NONEMPTY_TABLES       default "customers,optin_signups"
  PG_MAJOR                       expected pg_dump/server major, default 17
  ALERT_EMAIL_TO                 default bigwheat@pm.me
  ALERT_SMTP_HOST/PORT/USER/PASSWORD/FROM   optional; unset = no custom email
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("pestpod.backup")
CT = ZoneInfo("America/Chicago")

DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_API = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT = "https://content.dropboxapi.com/2"
RENDER_API = "https://api.render.com/v1"

DROPBOX_BLOCK = 4 * 1024 * 1024
UPLOAD_CHUNK = 8 * 1024 * 1024          # multiple of 4 MiB, per Dropbox guidance
SINGLE_UPLOAD_MAX = 8 * 1024 * 1024     # above this, use an upload session

BACKUP_NAME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}Z)\.(?P<ext>dump|dir\.tar\.gz|json)$"
)


class BackupError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise BackupError(f"missing required env var {name}")
    return val


_SECRET_PATTERNS = [
    re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"),
    re.compile(r"([?&](?:auth|X-Amz-[A-Za-z]+|token)=)[^&\s\"']+"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + "REDACTED" + ("@" if m.group(0).endswith("@") else ""), text)
    return text


def should_run(now_utc: datetime, hour_ct: int = 3) -> bool:
    """True only when the America/Chicago local hour equals hour_ct."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(CT).hour == hour_ct


def backup_stamp(now_utc: datetime) -> str:
    return now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H_%MZ")


def dropbox_content_hash(path: str) -> str:
    """Dropbox content_hash: sha256 over the concatenated sha256 of each 4 MiB block."""
    overall = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(DROPBOX_BLOCK)
            if not block:
                break
            overall.update(hashlib.sha256(block).digest())
    return overall.hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def select_for_deletion(entries: list[dict], now_utc: datetime,
                        retention_days: int = 60, min_keep: int = 7) -> list[str]:
    """Pick app-folder paths to delete.

    Only files whose name matches BACKUP_NAME_RE, older than retention_days.
    Never drops the number of remaining dumps below min_keep.
    """
    cutoff = now_utc - timedelta(days=retention_days)
    parsed = []
    for e in entries:
        if e.get(".tag") != "file":
            continue
        name = e.get("name", "")
        m = BACKUP_NAME_RE.match(name)
        if not m:
            continue
        # Only top-level files of the app folder.
        path = e.get("path_lower") or e.get("path_display") or ""
        if path.count("/") != 1:
            continue
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H_%MZ").replace(tzinfo=timezone.utc)
        parsed.append((ts, m.group("ext"), e.get("path_display") or path))

    dump_stamps = sorted({ts for ts, ext, _ in parsed if ext != "json"}, reverse=True)
    protected = set(dump_stamps[:min_keep])
    return sorted(p for ts, ext, p in parsed if ts < cutoff and ts not in protected)


# --------------------------------------------------------------------------- Dropbox
class Dropbox:
    def __init__(self, app_key: str, app_secret: str, refresh_token: str, session=None):
        self.s = session or requests.Session()
        r = self.s.post(DROPBOX_TOKEN_URL, data={"grant_type": "refresh_token",
                                                 "refresh_token": refresh_token},
                        auth=(app_key, app_secret), timeout=30)
        if r.status_code != 200:
            raise BackupError(f"Dropbox token refresh failed: HTTP {r.status_code}")
        self.token = r.json()["access_token"]

    def _h(self, extra=None):
        h = {"Authorization": f"Bearer {self.token}"}
        h.update(extra or {})
        return h

    def _rpc(self, endpoint: str, payload: dict) -> dict:
        r = self.s.post(f"{DROPBOX_API}/{endpoint}", headers=self._h(), json=payload, timeout=60)
        if r.status_code != 200:
            raise BackupError(f"Dropbox {endpoint} failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def _content(self, endpoint: str, arg: dict, data: bytes) -> dict:
        r = self.s.post(f"{DROPBOX_CONTENT}/{endpoint}",
                        headers=self._h({"Dropbox-API-Arg": json.dumps(arg),
                                         "Content-Type": "application/octet-stream"}),
                        data=data, timeout=300)
        if r.status_code != 200:
            raise BackupError(f"Dropbox {endpoint} failed: HTTP {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def upload(self, local_path: str, dest: str) -> dict:
        size = os.path.getsize(local_path)
        commit = {"path": dest, "mode": "add", "autorename": False, "mute": True,
                  "strict_conflict": True}
        with open(local_path, "rb") as fh:
            if size <= SINGLE_UPLOAD_MAX:
                return self._content("files/upload", commit, fh.read())
            first = fh.read(UPLOAD_CHUNK)
            sid = self._content("files/upload_session/start", {"close": False}, first)["session_id"]
            offset = len(first)
            while True:
                chunk = fh.read(UPLOAD_CHUNK)
                if not chunk or offset + len(chunk) >= size:
                    return self._content("files/upload_session/finish",
                                         {"cursor": {"session_id": sid, "offset": offset},
                                          "commit": commit}, chunk)
                self._content("files/upload_session/append_v2",
                              {"cursor": {"session_id": sid, "offset": offset}, "close": False},
                              chunk)
                offset += len(chunk)

    def metadata(self, path: str) -> dict:
        return self._rpc("files/get_metadata", {"path": path})

    def list_root(self) -> list[dict]:
        res = self._rpc("files/list_folder", {"path": "", "recursive": False})
        entries = list(res.get("entries", []))
        while res.get("has_more"):
            res = self._rpc("files/list_folder/continue", {"cursor": res["cursor"]})
            entries.extend(res.get("entries", []))
        return entries

    def delete(self, path: str) -> None:
        self._rpc("files/delete_v2", {"path": path})


# --------------------------------------------------------------------------- export
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise BackupError(f"{cmd[0]} failed (exit {p.returncode}): {redact(p.stderr)[-800:]}")
    return p


def check_pg_tools(expected_major: int) -> str:
    out = _run(["pg_dump", "--version"]).stdout.strip()
    m = re.search(r"(\d+)\.\d+", out)
    if not m or int(m.group(1)) != expected_major:
        raise BackupError(f"pg_dump major version mismatch: {out!r}, expected {expected_major}")
    return out


def export_pg_dump(db_url: str, workdir: str, stamp: str) -> str:
    path = os.path.join(workdir, f"{stamp}.dump")
    _run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
          "--file", path, "--dbname", db_url])
    return path


def export_render_api(api_key: str, pg_id: str, workdir: str, stamp: str,
                      timeout_s: int = 1800, session=None) -> tuple[str, str]:
    """Trigger a Render logical export, wait for it, download the .dir.tar.gz."""
    s = session or requests.Session()
    h = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def list_exports():
        r = s.get(f"{RENDER_API}/postgres/{pg_id}/export", headers=h, timeout=30)
        if r.status_code != 200:
            raise BackupError(f"Render list exports failed: HTTP {r.status_code}")
        return r.json()

    before = {e["id"] for e in list_exports()}
    r = s.post(f"{RENDER_API}/postgres/{pg_id}/export", headers=h, timeout=30)
    if r.status_code not in (200, 201, 202):
        raise BackupError(f"Render create export failed: HTTP {r.status_code} {r.text[:200]}")
    deadline = time.time() + timeout_s
    new = None
    while time.time() < deadline:
        fresh = [e for e in list_exports() if e["id"] not in before and e.get("url")]
        if fresh:
            new = sorted(fresh, key=lambda e: e["createdAt"])[-1]
            break
        time.sleep(15)
    if not new:
        raise BackupError("Render export did not become available in time")
    # Name the file by the export's own timestamp (e.g. 2026-11-10T11_40Z).
    export_stamp = new["id"].rsplit("/", 1)[-1].replace(":", "_")
    path = os.path.join(workdir, f"{export_stamp}.dir.tar.gz")
    with s.get(new["url"], stream=True, timeout=600) as dl:
        if dl.status_code != 200:
            raise BackupError(f"Render export download failed: HTTP {dl.status_code}")
        with open(path, "wb") as fh:
            for chunk in dl.iter_content(1024 * 1024):
                fh.write(chunk)
    return path, export_stamp


def restorable_target(dump_path: str, workdir: str) -> list[str]:
    """pg_restore args pointing at the archive (extracting .dir.tar.gz if needed)."""
    if dump_path.endswith(".dump"):
        return ["--format=custom", dump_path]
    if dump_path.endswith(".dir.tar.gz"):
        out = os.path.join(workdir, "extract")
        os.makedirs(out, exist_ok=True)
        with tarfile.open(dump_path, "r:gz") as tf:
            tf.extractall(out, filter="data")
        for root, _dirs, files in os.walk(out):
            if "toc.dat" in files:
                return ["--format=directory", root]
        raise BackupError("no toc.dat found in Render export archive")
    raise BackupError(f"unknown dump format: {dump_path}")


_COPY_RE = re.compile(r"^COPY (?:(?P<schema>[\w\"]+)\.)?(?P<table>[\w\"]+) .*FROM stdin;$")


def count_rows_in_sql(lines) -> dict[str, int]:
    counts: dict[str, int] = {}
    current = None
    for line in lines:
        line = line.rstrip("\n")
        if current is None:
            m = _COPY_RE.match(line)
            if m:
                current = m.group("table").strip('"')
                counts.setdefault(current, 0)
        elif line == "\\.":
            current = None
        else:
            counts[current] += 1
    return counts


def archive_versions(toc: str) -> dict[str, str]:
    out = {}
    for key, label in (("server_version", "Dumped from database version"),
                       ("dumped_by", "Dumped by pg_dump version")):
        m = re.search(rf"^;\s*{label}:\s*(\S+)", toc, re.M)
        if m:
            out[key] = m.group(1)
    return out


def verify_dump(dump_path: str, workdir: str, expected_major: int = 17) -> tuple[dict[str, int], dict[str, str]]:
    """pg_restore --list must succeed; returns (row counts, archive versions)."""
    target = restorable_target(dump_path, workdir)
    toc = _run(["pg_restore", "--list", *target]).stdout
    if "TABLE DATA" not in toc:
        raise BackupError("pg_restore --list shows no TABLE DATA entries")
    versions = archive_versions(toc)
    sv = versions.get("server_version", "")
    if not sv.startswith(f"{expected_major}."):
        raise BackupError(f"archive server version {sv!r} is not Postgres {expected_major}")
    p = subprocess.Popen(["pg_restore", "--data-only", "--file", "-", *target],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    counts = count_rows_in_sql(p.stdout)
    err = p.stderr.read()
    if p.wait() != 0:
        raise BackupError(f"pg_restore --data-only failed: {redact(err)[-500:]}")
    return counts, versions


def check_required_counts(counts: dict[str, int], required: list[str]) -> None:
    bad = [t for t in required if counts.get(t, 0) <= 0]
    if bad:
        raise BackupError(f"required tables empty or missing in dump: {', '.join(bad)}")


# --------------------------------------------------------------------------- alert
def send_alert(subject: str, body: str) -> bool:
    """Email via SMTP only if ALERT_SMTP_* is configured. Returns True if sent."""
    host = os.environ.get("ALERT_SMTP_HOST")
    if not host:
        log.warning("no ALERT_SMTP_HOST set; relying on Render's cron-failure notification email")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("ALERT_SMTP_FROM") or os.environ.get("ALERT_SMTP_USER", "")
        msg["To"] = os.environ.get("ALERT_EMAIL_TO", "bigwheat@pm.me")
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("ALERT_SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.starttls()
            if os.environ.get("ALERT_SMTP_USER"):
                smtp.login(os.environ["ALERT_SMTP_USER"], os.environ.get("ALERT_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True
    except Exception as exc:  # alert failure must never mask the real failure
        log.error("alert email failed: %s", type(exc).__name__)
        return False


# --------------------------------------------------------------------------- main
def run_backup(now_utc: datetime, *, list_only_retention: bool = False,
               dropbox: Dropbox | None = None) -> dict:
    source = _env("BACKUP_SOURCE", "pg_dump")
    required = [t.strip() for t in _env("REQUIRED_NONEMPTY_TABLES", "customers,optin_signups").split(",") if t.strip()]
    pg_major = int(_env("PG_MAJOR", "17"))
    retention_days = int(_env("RETENTION_DAYS", "60"))
    min_keep = int(_env("MIN_KEEP_BACKUPS", "7"))

    workdir = tempfile.mkdtemp(prefix="pgbackup-")
    try:
        tools = check_pg_tools(pg_major)
        stamp = backup_stamp(now_utc)
        if source == "pg_dump":
            dump = export_pg_dump(_env("DATABASE_URL", required=True), workdir, stamp)
        elif source == "render_export":
            dump, stamp = export_render_api(_env("RENDER_BACKUP_API_KEY", required=True),
                                            _env("RENDER_POSTGRES_ID", required=True), workdir, stamp)
        else:
            raise BackupError(f"unknown BACKUP_SOURCE {source!r}")
        name = os.path.basename(dump)
        log.info("export ok: %s (%d bytes)", name, os.path.getsize(dump))

        counts, versions = verify_dump(dump, workdir, pg_major)
        check_required_counts(counts, required)
        log.info("pg_restore --list ok; row counts: %s", counts)

        local_hash = dropbox_content_hash(dump)
        integrity = {
            "file": name,
            "size_bytes": os.path.getsize(dump),
            "sha256": sha256_file(dump),
            "dropbox_content_hash": local_hash,
            "row_counts": counts,
            "source": source,
            "archive_versions": versions,
            "pg_client_version": tools,
            "created_utc": now_utc.astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        json_path = os.path.join(workdir, f"{stamp}.json")
        with open(json_path, "w") as fh:
            json.dump(integrity, fh, indent=2, sort_keys=True)

        dbx = dropbox or Dropbox(_env("DROPBOX_BACKUP_APP_KEY", required=True),
                                 _env("DROPBOX_BACKUP_APP_SECRET", required=True),
                                 _env("DROPBOX_BACKUP_REFRESH_TOKEN", required=True))
        meta = dbx.upload(dump, f"/{name}")
        remote = dbx.metadata(meta.get("path_lower", f"/{name}"))
        if remote.get("content_hash") != local_hash or int(remote.get("size", -1)) != integrity["size_bytes"]:
            raise BackupError("Dropbox content_hash/size mismatch after upload")
        jmeta = dbx.upload(json_path, f"/{stamp}.json")
        if jmeta.get("content_hash") != dropbox_content_hash(json_path):
            raise BackupError("Dropbox content_hash mismatch for integrity JSON")
        log.info("uploaded + verified /%s and /%s.json", name, stamp)

        to_delete = select_for_deletion(dbx.list_root(), now_utc, retention_days, min_keep)
        if list_only_retention:
            log.info("retention (list-only): would delete %d file(s): %s", len(to_delete), to_delete)
        else:
            for p in to_delete:
                dbx.delete(p)
            log.info("retention: deleted %d file(s)", len(to_delete))
        integrity["retention_candidates"] = to_delete
        integrity["dropbox_paths"] = [f"/{name}", f"/{stamp}.json"]
        return integrity
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None, now_utc: datetime | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="PestPod nightly Postgres backup")
    ap.add_argument("--force", action="store_true", help="skip the 3 AM Central hour gate")
    ap.add_argument("--retention-list-only", action="store_true",
                    help="list retention candidates without deleting")
    args = ap.parse_args(argv)
    now = now_utc or datetime.now(timezone.utc)
    hour = int(os.environ.get("BACKUP_HOUR_CT", "3"))
    if not args.force and not should_run(now, hour):
        log.info("not %d AM in America/Chicago (now %s CT); exiting without backup",
                 hour, now.astimezone(CT).strftime("%H:%M"))
        return 0
    try:
        result = run_backup(now, list_only_retention=args.retention_list_only)
        log.info("backup OK: %s", json.dumps({k: result[k] for k in ("file", "size_bytes", "row_counts")}))
        return 0
    except Exception as exc:
        msg = redact(f"{type(exc).__name__}: {exc}")
        log.error("BACKUP FAILED: %s", msg)
        send_alert("PestPod DB backup FAILED",
                   f"The nightly pestpod-db backup failed at {now.isoformat(timespec='seconds')}.\n\n{msg}\n\n"
                   "Check the Render cron job logs (pestpod-db-backup).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
