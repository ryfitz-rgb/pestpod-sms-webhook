"""
Side-by-side Postgres copy of the PestPod Google Sheet.

The Google Sheet stays the MAIN copy. This module only READS the Sheet and
replaces the Postgres snapshot; it never writes to the Sheet.

Env vars:
  DATABASE_URL     Render internal connection string for pestpod-db (unset = disabled)
  PG_SYNC_HOUR_CT  hour (America/Chicago) for the nightly copy, default 2
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("pestpod.pgsync")
CT = ZoneInfo("America/Chicago")
SYNC_TABS_PREFIXES = ("Customers", "Import Review")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id            BIGSERIAL PRIMARY KEY,
  source_tab    TEXT NOT NULL,
  sheet_row     INTEGER NOT NULL,
  account       TEXT,
  name          TEXT,
  phone         TEXT,
  address       TEXT,
  city          TEXT,
  service_type  TEXT,
  frequency     TEXT,
  next_due      TEXT,
  ok_to_text    TEXT,
  opt_in_status TEXT,
  tech          TEXT,
  raw           JSONB NOT NULL,
  synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_tab, sheet_row)
);
CREATE INDEX IF NOT EXISTS customers_phone_idx ON customers (phone);
CREATE TABLE IF NOT EXISTS optin_signups (
  id            BIGSERIAL PRIMARY KEY,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  name          TEXT,
  phone         TEXT NOT NULL,
  consent_text  TEXT NOT NULL,
  ip            TEXT,
  user_agent    TEXT,
  tech          TEXT,
  page_url      TEXT
);
CREATE INDEX IF NOT EXISTS optin_signups_phone_idx ON optin_signups (phone);
-- sms_consent: was the SMS consent box ticked? Rows saved before this column existed all
-- required the box, so they backfill to true; new rows default to false (no consent) and
-- the endpoint always writes the value explicitly.
ALTER TABLE optin_signups ADD COLUMN IF NOT EXISTS sms_consent BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE optin_signups ALTER COLUMN sms_consent SET DEFAULT false;
CREATE TABLE IF NOT EXISTS sync_runs (
  id          BIGSERIAL PRIMARY KEY,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  ok          BOOLEAN,
  counts      JSONB,
  error       TEXT
);
"""

FIELD_HEADERS = {
    "account": ("account",),
    "name": ("customer name", "name"),
    "phone": ("phone (+1xxxxxxxxxx)", "phone"),
    "address": ("address",),
    "city": ("city",),
    "service_type": ("service type",),
    "frequency": ("frequency",),
    "next_due": ("next due",),
    "ok_to_text": ("ok to text?", "ok to text"),
    "opt_in_status": ("opt-in status",),
    "tech": ("tech",),
}


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


def connect():
    import psycopg
    return psycopg.connect(os.environ["DATABASE_URL"].strip(), connect_timeout=10)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def rows_from_values(tab: str, values):
    """Turn a tab's values into customer dicts. Header = first row containing a phone column."""
    hdr_idx = None
    for i, r in enumerate(values[:10]):
        low = [str(c).strip().lower() for c in r]
        if any(h in low for h in FIELD_HEADERS["phone"]):
            hdr_idx = i
            break
    if hdr_idx is None:
        return []
    header = [str(c).strip() for c in values[hdr_idx]]
    low = [h.lower() for h in header]
    out = []
    for i, r in enumerate(values[hdr_idx + 1:], start=hdr_idx + 2):
        if not any(str(c).strip() for c in r):
            continue
        raw = {header[j]: r[j] for j in range(min(len(header), len(r))) if header[j]}
        rec = {"source_tab": tab, "sheet_row": i, "raw": raw}
        for field, names in FIELD_HEADERS.items():
            val = None
            for n in names:
                if n in low:
                    j = low.index(n)
                    val = str(r[j]).strip() if j < len(r) and str(r[j]).strip() else None
                    break
            rec[field] = val
        if not (rec["name"] or rec["phone"]):
            continue
        out.append(rec)
    return out


def replace_snapshot(conn, records):
    """Replace the customers copy in one transaction (Sheet is the source of truth)."""
    cols = ["source_tab", "sheet_row"] + list(FIELD_HEADERS) + ["raw"]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM customers")
            for rec in records:
                vals = [rec[c] if c != "raw" else json.dumps(rec["raw"]) for c in cols]
                cur.execute(
                    f"INSERT INTO customers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                    vals,
                )


def sync_once(svc, sheet_id, quote_tab) -> dict:
    """Read Customers + Import Review tabs (read-only) and copy them into Postgres."""
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])
            if s["properties"]["title"].startswith(SYNC_TABS_PREFIXES)]
    records, counts = [], {}
    for tab in tabs:
        vals = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=quote_tab(tab)).execute().get("values", [])
        recs = rows_from_values(tab, vals)
        counts[tab] = len(recs)
        records.extend(recs)
    with connect() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sync_runs DEFAULT VALUES RETURNING id")
            run_id = cur.fetchone()[0]
        conn.commit()
        try:
            replace_snapshot(conn, records)
            with conn.cursor() as cur:
                cur.execute("UPDATE sync_runs SET finished_at=now(), ok=true, counts=%s WHERE id=%s",
                            (json.dumps(counts), run_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE sync_runs SET finished_at=now(), ok=false, error=%s WHERE id=%s",
                            (type(e).__name__, run_id))
            conn.commit()
            raise
    log.info("pg sync ok: %s", counts)
    return counts


def save_optin(name, phone, consent_text, ip, user_agent, tech, page_url, sms_consent=False) -> int:
    """Store a web form submission. sms_consent=False rows are contacts only: nothing here
    (or anywhere else) marks them Opted in / OK to text or sends them a text."""
    with connect() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO optin_signups (name, phone, consent_text, ip, user_agent, tech, page_url, sms_consent) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (name, phone, consent_text, ip, user_agent, tech, page_url, bool(sms_consent)),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def status() -> dict:
    with connect() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT source_tab, count(*) FROM customers GROUP BY 1 ORDER BY 1")
            by_tab = {t: n for t, n in cur.fetchall()}
            cur.execute("SELECT count(*), count(*) FILTER (WHERE NOT sms_consent) FROM optin_signups")
            optins, no_consent = cur.fetchone()
            cur.execute("SELECT finished_at, ok, counts FROM sync_runs ORDER BY id DESC LIMIT 1")
            last = cur.fetchone()
    return {"customers_by_tab": by_tab, "optin_signups": optins,
            "optin_signups_no_sms_consent": no_consent,
            "last_sync": {"finished_at": last[0].isoformat() if last and last[0] else None,
                          "ok": last[1] if last else None} if last else None}


def next_run(now: datetime, hour: int) -> datetime:
    t = now.replace(hour=hour, minute=15, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def start_nightly(get_sheets, sheet_id, quote_tab) -> None:
    """Initial copy shortly after startup, then nightly at PG_SYNC_HOUR_CT:15 Central."""
    if not enabled():
        log.info("DATABASE_URL not set; Postgres side copy disabled")
        return
    hour = int(os.environ.get("PG_SYNC_HOUR_CT", "2"))

    def loop():
        delay = 30
        while True:
            time.sleep(delay)
            try:
                svc = get_sheets()
                if svc is None:
                    log.warning("pg sync skipped: Sheets unavailable")
                else:
                    sync_once(svc, sheet_id, quote_tab)
            except Exception as e:
                log.warning("pg sync failed: %s", type(e).__name__)
            now = datetime.now(CT)
            delay = max(60, (next_run(now, hour) - now).total_seconds())

    threading.Thread(target=loop, name="pg-nightly-sync", daemon=True).start()
