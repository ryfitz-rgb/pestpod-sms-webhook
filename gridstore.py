"""Postgres-backed stand-in for the Google Sheets API (CUSTOMER_STORE=postgres).

Why this shape: the webhook (main.py), batch.py and the dashboard all talk to the
"PestPod Reminders v2" sheet through a small part of the Sheets v4 client. This module
serves that same part from Postgres, tab for tab and row for row, so the existing
matching, opt-in, reply, batch and dashboard logic runs unchanged when the store flips.

  * Data lives in grid_tabs / grid_rows (one JSONB array of raw cell values per row,
    the same values Sheets returns with UNFORMATTED_VALUE + SERIAL_NUMBER).
  * Customers columns I (Next Due) and K (OK to Text?) are never stored. They are computed
    on every read by formulas.py, and any write that touches them is refused.
  * Every cell change is recorded in grid_changes (old -> new, who), which gives an
    audit trail and lets a change be put back by hand.
  * customer_list_v is a typed, read-only SQL view of the Customers tab for psql/exports.

Nothing here talks to Google. Moving data in or out of the real sheet is done by the CLI
at the bottom (import / verify / export), and only when someone runs it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

import formulas

log = logging.getLogger("pestpod.gridstore")

CUSTOMERS_TAB = "Customers"
PROTECTED = {(CUSTOMERS_TAB, formulas.COL_I), (CUSTOMERS_TAB, formulas.COL_K)}
DEFAULT_COLS = 26

# Display formats the Customers tab uses (checked against the sheet's FORMATTED_VALUE).
DATE_HEADERS = {"last service date", "next due", "opt-in date", "cancelled on"}
DATETIME_HEADERS = {"proposed appointment"}


class GridError(Exception):
    pass


def store_mode() -> str:
    return (os.environ.get("CUSTOMER_STORE") or "sheet").strip().lower()


def use_postgres() -> bool:
    return store_mode() == "postgres"


# ---------------------------------------------------------------- A1 ranges
_A1 = re.compile(r"^([A-Z]*)(\d*)$")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letter(idx: int) -> str:
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_range(rng: str):
    """"'Tab'!A1:C5" -> (tab, c0, r0, c1, r1); missing bounds are None. Rows are 1-based."""
    rng = rng.strip()
    if rng.startswith("'"):
        i, out = 1, []
        while i < len(rng):
            if rng[i] == "'":
                if i + 1 < len(rng) and rng[i + 1] == "'":
                    out.append("'"); i += 2; continue
                break
            out.append(rng[i]); i += 1
        tab, rest = "".join(out), rng[i + 1:]
    else:
        tab, _, rest = rng.partition("!")
        rest = "!" + rest if rest else ""
    rest = rest[1:] if rest.startswith("!") else rest
    if not rest:
        return tab, None, None, None, None
    a, _, b = rest.upper().partition(":")
    ma, mb = _A1.match(a), _A1.match(b or a)
    if not ma or not mb:
        raise GridError(f"bad range {rng!r}")
    c0 = col_index(ma[1]) if ma[1] else None
    r0 = int(ma[2]) if ma[2] else None
    c1 = col_index(mb[1]) if mb[1] else None
    r1 = int(mb[2]) if mb[2] else None
    return tab, c0, r0, c1, r1


# ---------------------------------------------------------------- value conversion
_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


def user_entered(v):
    """What Sheets stores for a USER_ENTERED value (the cases this app writes)."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s.upper() in ("TRUE", "FALSE"):
        return s.upper() == "TRUE"
    m = _ISO_DATE.match(s)
    if m:
        from datetime import date
        try:
            return formulas.serial_from_date(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return v
    if _NUM.match(s):
        f = float(s)
        return int(f) if f.is_integer() and "." not in s else f
    if s.startswith("="):
        raise GridError("formulas cannot be written to the Postgres store")
    return v


def _fmt_number(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def formatted(v, header: str = "") -> str:
    """FORMATTED_VALUE rendering for the formats the PestPod sheet uses."""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        h = header.strip().lower()
        if h in DATE_HEADERS:
            return formulas.date_from_serial(v).isoformat()
        if h in DATETIME_HEADERS:
            d = formulas.date_from_serial(v)
            minutes = round((float(v) - int(v)) * 24 * 60)
            hh, mm = divmod(minutes, 60)
            ampm = "AM" if hh < 12 else "PM"
            return f"{d:%a} {d.isoformat()} {hh % 12 or 12}:{mm:02d} {ampm}"
        return _fmt_number(v)
    return str(v)


def _trim(row: list) -> list:
    row = list(row)
    while row and (row[-1] is None or row[-1] == ""):
        row.pop()
    return row


# ---------------------------------------------------------------- backends
class MemoryBackend:
    """In-process backend for tests."""

    def __init__(self, tabs=None):
        self._lock = threading.Lock()
        self.tabs = {}      # title -> {"sheet_id": int, "idx": int, "rows": {row_num: [cells]}}
        self.changes = []
        for title, rows in (tabs or {}).items():
            self.add_tab(title)
            for i, r in enumerate(rows, start=1):
                self.tabs[title]["rows"][i] = list(r)

    def list_tabs(self):
        return [(t, v["sheet_id"], v["idx"]) for t, v in sorted(self.tabs.items(), key=lambda kv: kv[1]["idx"])]

    def add_tab(self, title):
        with self._lock:
            if title in self.tabs:
                raise GridError(f"tab {title!r} already exists")
            self.tabs[title] = {"sheet_id": 1000 + len(self.tabs), "idx": len(self.tabs), "rows": {}}

    def rows(self, tab):
        if tab not in self.tabs:
            raise GridError(f"Unable to parse range: {tab}")
        return {k: list(v) for k, v in self.tabs[tab]["rows"].items()}

    def write(self, tab, cells, source):
        """cells: [(row_num, col, value)] applied atomically."""
        with self._lock:
            rows = self.tabs[tab]["rows"]
            for r, c, v in cells:
                row = rows.setdefault(r, [])
                row.extend([""] * (c + 1 - len(row)))
                self.changes.append((tab, r, c, row[c], v, source))
                row[c] = v

    def append(self, tab, start_col, values, source):
        with self._lock:
            rows = self.tabs[tab]["rows"]
            last = max((k for k, v in rows.items() if any(x not in (None, "") for x in v)), default=0)
            r = last + 1
            row = [""] * start_col + list(values)
            rows[r] = row
            for c, v in enumerate(row):
                if c >= start_col:
                    self.changes.append((tab, r, c, "", v, source))
            return r


SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_tabs (
  title     TEXT PRIMARY KEY,
  sheet_id  BIGINT NOT NULL,
  idx       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grid_rows (
  tab        TEXT NOT NULL REFERENCES grid_tabs(title),
  row_num    INTEGER NOT NULL CHECK (row_num >= 1),
  cells      JSONB NOT NULL DEFAULT '[]'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tab, row_num)
);
CREATE TABLE IF NOT EXISTS grid_changes (
  id       BIGSERIAL PRIMARY KEY,
  at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  tab      TEXT NOT NULL,
  row_num  INTEGER NOT NULL,
  col      INTEGER NOT NULL,
  old      JSONB,
  new      JSONB,
  source   TEXT
);
CREATE INDEX IF NOT EXISTS grid_changes_tab_row_idx ON grid_changes (tab, row_num);
-- Read-only typed view of the Customers tab (columns A..Y as of 2026-11-10).
-- Next Due / OK to Text? are NOT here on purpose: the app computes them (formulas.py).
CREATE OR REPLACE VIEW customer_list_v AS
SELECT row_num,
       cells->>0  AS account,          cells->>1  AS customer_name,
       cells->>2  AS phone,            cells->>3  AS address,
       cells->>4  AS city,             cells->>5  AS service_type,
       cells->>6  AS frequency,        cells->>7  AS last_service_serial,
       cells->>9  AS wave,             cells->>11 AS opt_in_status,
       cells->>12 AS opt_in_method,    cells->>13 AS opt_in_date_serial,
       cells->>14 AS proposed_appointment, cells->>15 AS reminder_status,
       cells->>16 AS last_reply,       cells->>17 AS pest_notes,
       cells->>18 AS termite_notes,    cells->>19 AS missed_count,
       cells->>20 AS office_list_month, cells->>21 AS source,
       cells->>22 AS import_notes,     cells->>23 AS status,
       cells->>24 AS cancelled_on_serial,
       updated_at
FROM grid_rows WHERE tab = 'Customers' AND row_num >= 2;
"""


class PgBackend:
    """Postgres backend (DATABASE_URL). Each call is one short transaction."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ready = False

    def _conn(self):
        import psycopg
        return psycopg.connect(self.dsn, connect_timeout=5)

    def ensure_schema(self):
        if self._ready:
            return
        with self._conn() as conn:
            conn.execute(SCHEMA)
        self._ready = True

    def list_tabs(self):
        self.ensure_schema()
        with self._conn() as conn:
            return [tuple(r) for r in conn.execute("SELECT title, sheet_id, idx FROM grid_tabs ORDER BY idx")]

    def add_tab(self, title, sheet_id=None):
        self.ensure_schema()
        with self._conn() as conn:
            n = conn.execute("SELECT count(*) FROM grid_tabs").fetchone()[0]
            conn.execute("INSERT INTO grid_tabs (title, sheet_id, idx) VALUES (%s, %s, %s)",
                         (title, sheet_id if sheet_id is not None else 1000 + n, n))

    def rows(self, tab):
        self.ensure_schema()
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM grid_tabs WHERE title=%s", (tab,)).fetchone():
                raise GridError(f"Unable to parse range: {tab}")
            return {r: list(c) for r, c in conn.execute(
                "SELECT row_num, cells FROM grid_rows WHERE tab=%s ORDER BY row_num", (tab,))}

    def write(self, tab, cells, source):
        from psycopg.types.json import Jsonb
        self.ensure_schema()
        by_row = {}
        for r, c, v in cells:
            by_row.setdefault(r, []).append((c, v))
        with self._conn() as conn, conn.transaction():
            for r, items in sorted(by_row.items()):
                got = conn.execute("SELECT cells FROM grid_rows WHERE tab=%s AND row_num=%s FOR UPDATE",
                                   (tab, r)).fetchone()
                row = list(got[0]) if got else []
                for c, v in items:
                    row.extend([""] * (c + 1 - len(row)))
                    conn.execute("INSERT INTO grid_changes (tab, row_num, col, old, new, source) "
                                 "VALUES (%s,%s,%s,%s,%s,%s)", (tab, r, c, Jsonb(row[c]), Jsonb(v), source))
                    row[c] = v
                conn.execute("INSERT INTO grid_rows (tab, row_num, cells) VALUES (%s,%s,%s) "
                             "ON CONFLICT (tab, row_num) DO UPDATE SET cells=EXCLUDED.cells, updated_at=now()",
                             (tab, r, Jsonb(row)))

    def append(self, tab, start_col, values, source):
        from psycopg.types.json import Jsonb
        self.ensure_schema()
        with self._conn() as conn, conn.transaction():
            # Lock the tab so two appends can't take the same row number.
            conn.execute("SELECT 1 FROM grid_tabs WHERE title=%s FOR UPDATE", (tab,))
            last = conn.execute(
                "SELECT coalesce(max(row_num),0) FROM grid_rows WHERE tab=%s AND EXISTS ("
                " SELECT 1 FROM jsonb_array_elements(cells) e WHERE e <> '\"\"'::jsonb AND e <> 'null'::jsonb)",
                (tab,)).fetchone()[0]
            r = last + 1
            row = [""] * start_col + list(values)
            conn.execute("INSERT INTO grid_rows (tab, row_num, cells) VALUES (%s,%s,%s) "
                         "ON CONFLICT (tab, row_num) DO UPDATE SET cells=EXCLUDED.cells, updated_at=now()",
                         (tab, r, Jsonb(row)))
            conn.execute("INSERT INTO grid_changes (tab, row_num, col, old, new, source) "
                         "VALUES (%s,%s,%s,NULL,%s,%s)", (tab, r, -1, Jsonb(row), source))
            return r


# ---------------------------------------------------------------- Sheets-compatible service
class _Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class GridService:
    """Duck-types the googleapiclient Sheets service: spreadsheets() / values() / execute()."""

    def __init__(self, backend, source="webhook"):
        self.b = backend
        self.source = source

    def spreadsheets(self):
        return self

    def values(self):
        return _Values(self)

    # spreadsheets().get / batchUpdate
    def get(self, spreadsheetId=None, fields=None, **_):
        def run():
            sheets = []
            for title, sid, idx in self.b.list_tabs():
                width = DEFAULT_COLS
                try:
                    width = max([DEFAULT_COLS] + [len(r) for r in self.b.rows(title).values()])
                except GridError:
                    pass
                sheets.append({"properties": {"sheetId": sid, "title": title, "index": idx,
                                              "gridProperties": {"columnCount": width}}})
            return {"sheets": sheets}
        return _Exec(run)

    def batchUpdate(self, spreadsheetId=None, body=None, **_):
        def run():
            replies = []
            for req in (body or {}).get("requests", []):
                if "addSheet" in req:
                    self.b.add_tab(req["addSheet"]["properties"]["title"])
                    replies.append({"addSheet": {}})
                elif "appendDimension" in req:
                    replies.append({})  # rows are unbounded here
                else:
                    raise GridError(f"unsupported request {sorted(req)}")
            return {"replies": replies}
        return _Exec(run)

    # ---- helpers used by _Values
    def read_grid(self, tab):
        rows = self.b.rows(tab)
        if tab == CUSTOMERS_TAB:
            for r, row in rows.items():
                if r >= 2:
                    row.extend([""] * (formulas.COL_K + 1 - len(row)))
                    for c, v in formulas.computed_cells(row).items():
                        row[c] = v
        return rows

    def check_writable(self, tab, cells):
        for _r, c, _v in cells:
            if (tab, c) in PROTECTED:
                raise GridError(f"refusing to write formula column {col_letter(c)} on {tab}")


class _Values:
    def __init__(self, svc: GridService):
        self.s = svc

    def get(self, spreadsheetId=None, range=None, valueRenderOption="FORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER", **_):
        def run():
            tab, c0, r0, c1, r1 = parse_range(range)
            grid = self.s.read_grid(tab)
            header = grid.get(1, [])
            c0, r0 = c0 or 0, r0 or 1
            last = max(grid, default=0)
            r1 = min(r1, last) if r1 else last
            out = []
            for r in builtins_range(r0, r1 + 1):
                row = grid.get(r, [])
                row = row[c0:(c1 + 1) if c1 is not None else None]
                if valueRenderOption == "FORMATTED_VALUE":
                    row = [formatted(v, str(header[c0 + j]) if c0 + j < len(header) else "")
                           for j, v in enumerate(row)]
                out.append(_trim(["" if v is None else v for v in row]))
            while out and not out[-1]:
                out.pop()
            res = {"range": range, "majorDimension": "ROWS"}
            if out:
                res["values"] = out
            return res
        return _Exec(run)

    def _cells(self, rng, values, option):
        tab, c0, r0, _c1, _r1 = parse_range(rng)
        c0, r0 = c0 or 0, r0 or 1
        cells = []
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                cells.append((r0 + i, c0 + j, user_entered(v) if option == "USER_ENTERED" else v))
        return tab, cells

    def update(self, spreadsheetId=None, range=None, valueInputOption="RAW", body=None, **_):
        def run():
            tab, cells = self._cells(range, (body or {}).get("values", []), valueInputOption)
            self.s.check_writable(tab, cells)
            self.s.b.write(tab, cells, self.s.source)
            return {"updatedRange": range, "updatedCells": len(cells)}
        return _Exec(run)

    def batchUpdate(self, spreadsheetId=None, body=None, **_):
        def run():
            body_ = body or {}
            opt = body_.get("valueInputOption", "RAW")
            per_tab = {}
            for d in body_.get("data", []):
                tab, cells = self._cells(d["range"], d["values"], opt)
                per_tab.setdefault(tab, []).extend(cells)
            for tab, cells in per_tab.items():   # check everything before writing anything
                self.s.check_writable(tab, cells)
            for tab, cells in per_tab.items():
                self.s.b.write(tab, cells, self.s.source)
            return {"totalUpdatedCells": sum(len(c) for c in per_tab.values())}
        return _Exec(run)

    def append(self, spreadsheetId=None, range=None, valueInputOption="RAW", insertDataOption=None,
               body=None, **_):
        def run():
            tab, c0, _r0, _c1, _r1 = parse_range(range)
            c0 = c0 or 0
            rows = (body or {}).get("values", [])
            first = None
            for row in rows:
                vals = [user_entered(v) if valueInputOption == "USER_ENTERED" else v for v in row]
                self.s.check_writable(tab, [(0, c0 + j, v) for j, v in enumerate(vals)])
                r = self.s.b.append(tab, c0, vals, self.s.source)
                first = first or r
            return {"updates": {"updatedRange": f"'{tab}'!{col_letter(c0)}{first}", "updatedRows": len(rows)}}
        return _Exec(run)


import builtins as _b  # noqa: E402
builtins_range = _b.range

_service = None
_service_lock = threading.Lock()


def service():
    """Shared GridService on DATABASE_URL, or None if DATABASE_URL is unset."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                dsn = os.environ.get("DATABASE_URL", "").strip()
                if not dsn:
                    log.error("CUSTOMER_STORE=postgres but DATABASE_URL is not set")
                    return None
                _service = GridService(PgBackend(dsn))
    return _service


# ---------------------------------------------------------------- import / verify / export
def import_from_sheet(sheets_svc, sheet_id, backend, replace=False, out=print):
    """Copy every tab of the real sheet into the grid (READ-ONLY on the sheet).

    Formula columns (Customers I and K) are not copied; they're computed. Refuses to
    overwrite existing grid data unless replace=True.
    """
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    have = {t for t, _, _ in backend.list_tabs()}
    if have and not replace:
        raise GridError("grid already has data; pass --replace to overwrite (it is backed up nightly)")
    counts = {}
    for s in meta.get("sheets", []):
        p = s["properties"]
        title = p["title"]
        vals = sheets_svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="'" + title.replace("'", "''") + "'",
            valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER",
        ).execute().get("values", [])
        if title not in have:
            if isinstance(backend, PgBackend):
                backend.add_tab(title, p.get("sheetId"))
            else:
                backend.add_tab(title)
        cells = []
        for r, row in enumerate(vals, start=1):
            for c, v in enumerate(row):
                if title == CUSTOMERS_TAB and r >= 2 and (title, c) in PROTECTED:
                    continue
                if v not in ("", None):
                    cells.append((r, c, v))
        if cells:
            backend.write(title, cells, "import")
        counts[title] = max(0, len(vals) - 1)
        out(f"  {title}: {counts[title]} data rows")
    return counts


def verify_against_sheet(sheets_svc, sheet_id, grid_svc, out=print):
    """Compare what the app would read from Postgres vs the real sheet. Prints counts only."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
    total = 0
    for s in meta.get("sheets", []):
        title = s["properties"]["title"]
        rng = "'" + title.replace("'", "''") + "'"
        for mode in ("FORMATTED_VALUE", "UNFORMATTED_VALUE"):
            a = sheets_svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng,
                                                       valueRenderOption=mode).execute().get("values", [])
            b = grid_svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng,
                                                     valueRenderOption=mode).execute().get("values", [])
            diffs = []
            for r in builtins_range(max(len(a), len(b))):
                ra = a[r] if r < len(a) else []
                rb = b[r] if r < len(b) else []
                for c in builtins_range(max(len(ra), len(rb))):
                    va = ra[c] if c < len(ra) else ""
                    vb = rb[c] if c < len(rb) else ""
                    if va != vb and not (isinstance(va, (int, float)) and isinstance(vb, (int, float))
                                         and abs(va - vb) < 1e-9):
                        diffs.append(f"{col_letter(c)}{r + 1}")
            total += len(diffs)
            out(f"  {title} [{mode}]: {len(diffs)} differing cells" + (f" e.g. {diffs[:8]}" if diffs else ""))
    return total


def export_grid(backend) -> dict:
    """{tab: [[cells...], ...]} with raw values (for a rollback write-back or a CSV)."""
    out = {}
    for title, _, _ in backend.list_tabs():
        rows = backend.rows(title)
        last = max(rows, default=0)
        out[title] = [rows.get(r, []) for r in builtins_range(1, last + 1)]
    return out


def write_back_plan(backend):
    """Ranges a rollback would write to the real sheet: everything except Customers I and K."""
    plan = []
    for title, grid in export_grid(backend).items():
        for r, row in enumerate(grid, start=1):
            for c, v in enumerate(row):
                if (title, c) in PROTECTED and r >= 2:
                    continue
                plan.append((title, r, c, v))
    return plan


def _cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="PestPod Postgres customer store tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="copy the Google Sheet into Postgres (read-only on the sheet)")
    imp.add_argument("--confirm", action="store_true")
    imp.add_argument("--replace", action="store_true")
    sub.add_parser("verify", help="compare Postgres vs the sheet (counts only)")
    exp = sub.add_parser("export", help="write the Postgres grid to a local JSON file")
    exp.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    import main as m  # real Sheets client; CUSTOMER_STORE is ignored here on purpose
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL not set")
    backend = PgBackend(dsn)
    if a.cmd == "export":
        with open(a.out, "w") as fh:
            json.dump(export_grid(backend), fh)
        print(f"wrote {a.out}")
        return 0
    sheets = m.get_real_sheets()
    if sheets is None:
        raise SystemExit("Google Sheets credentials unavailable")
    if a.cmd == "import":
        if not a.confirm:
            raise SystemExit("dry refusal: re-run with --confirm to copy the sheet into Postgres")
        import_from_sheet(sheets, m.SHEET_ID, backend, replace=a.replace)
        return 0
    n = verify_against_sheet(sheets, m.SHEET_ID, GridService(backend, "verify"))
    print("MATCH" if n == 0 else f"{n} differing cells")
    return 0 if n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
