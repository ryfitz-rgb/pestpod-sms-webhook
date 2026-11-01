"""Offline tests for dashboard Undo / Cancel / Reactivate (fake Sheets, no network, no texts)."""
import os
os.environ["DASHBOARD_PASSWORD"] = "test-pass-123"
os.environ.setdefault("SHEET_ID", "test-sheet")
os.environ.pop("GOOGLE_SA_JSON_B64", None)
import re
import pytest
from fastapi.testclient import TestClient
import main
import dashboard as dash

# A..W like the live sheet: H = Last Service Date, I = Next Due, K = OK to Text?
HDR = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Address", "City", "Service Type", "Frequency",
       "Last Service Date", "Next Due", "Wave", "OK to Text?", "Opt-In Status", "Opt-In Method", "Opt-In Date",
       "Proposed Appointment", "Reminder Status", "Last Reply", "Pest Notes", "Termite Notes", "Missed Count",
       "Office List Month", "Source", "Import Notes"]


def _col(letter):
    n = 0
    for ch in letter:
        n = n * 26 + ord(ch) - 64
    return n - 1


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class FakeSheets:
    def __init__(self):
        r = [""] * len(HDR)
        r[0], r[1], r[7], r[8], r[10] = "1001", "Alice Test", 46231, 46323, True
        r2 = list(r); r2[0], r2[1], r2[7] = "1002", "Bob Blank", ""
        self.tabs = {"Customers": [list(HDR), r, r2]}
        self.cols = {"Customers": 23}
        self.written = []  # (tab, letter, row)
    def spreadsheets(self): return self
    def values(self): return self
    def _parse(self, rng):
        tab, _, cell = rng.partition("!")
        tab = tab.strip("'")
        m = re.match(r"([A-Z]+)(\d+)?", cell or "A")
        return tab, m.group(1), int(m.group(2)) if m.group(2) else None
    def _set(self, tab, letter, row, val):
        t = self.tabs.setdefault(tab, [])
        while len(t) < row:
            t.append([])
        r = t[row - 1]
        c = _col(letter)
        while len(r) <= c:
            r.append("")
        r[c] = val
        self.written.append((tab, letter, row))
    def get(self, spreadsheetId, range=None, fields=None, **kw):
        if fields:
            return Exec(lambda: {"sheets": [{"properties": {"title": t, "sheetId": i,
                                 "gridProperties": {"columnCount": self.cols.get(t, 26)}}}
                                for i, t in enumerate(self.tabs)]})
        tab = self._parse(range)[0]
        if tab not in self.tabs:
            def boom(): raise RuntimeError("no tab")
            return Exec(boom)
        return Exec(lambda: {"values": [list(r) for r in self.tabs[tab]]})
    def update(self, spreadsheetId, range, valueInputOption, body):
        tab, letter, row = self._parse(range)
        def go():
            for k, v in enumerate(body["values"][0]):
                self._set(tab, main.col_letter(_col(letter) + k), row, v)
            return {}
        return Exec(go)
    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        tab = self._parse(range)[0]
        def go():
            self.tabs[tab].append(list(body["values"][0])); return {}
        return Exec(go)
    def batchUpdate(self, spreadsheetId, body):
        def go():
            if "data" in body:
                for d in body["data"]:
                    tab, letter, row = self._parse(d["range"])
                    self._set(tab, letter, row, d["values"][0][0])
            for rq in body.get("requests", []):
                if "addSheet" in rq:
                    self.tabs[rq["addSheet"]["properties"]["title"]] = []
                if "appendDimension" in rq:
                    self.cols["Customers"] += rq["appendDimension"]["length"]
            return {}
        return Exec(go)


@pytest.fixture
def fs(monkeypatch):
    f = FakeSheets()
    monkeypatch.setattr(main, "get_sheets", lambda: f)
    monkeypatch.setattr(main, "SHEET_ID", "test-sheet")
    dash._cache.update(t=0, rows=None)
    yield f
    dash._cache.update(t=0, rows=None)


def H(f, row=2):
    return f.tabs["Customers"][row - 1][7]


def test_mark_logs_then_undo_restores(fs):
    dash.write_last_service(2, "1001", __import__("datetime").date(2026, 10, 30))
    log = fs.tabs["Undo Log"]
    assert log[0] == dash.UNDO_HEADERS
    assert log[1][1] == 2 and log[1][4] == "2026-07-28" and log[1][5] == "2026-10-30"
    assert H(fs) == "2026-10-30"
    assert dash.undo_last(2, "1001") == "undone"
    assert H(fs) == "2026-07-28" and log[1][6]
    assert dash.undo_last(2, "1001") == "none"
    assert not any(l in ("I", "K") for t, l, r in fs.written if t == "Customers")


def test_undo_blank_restores_blank(fs):
    dash.write_last_service(3, "1002", __import__("datetime").date(2026, 10, 30))
    assert dash.undo_last(3, "1002") == "undone"
    assert H(fs, 3) == ""


def test_undo_refused_if_changed_since(fs):
    dash.write_last_service(2, "1001", __import__("datetime").date(2026, 10, 30))
    fs.tabs["Customers"][1][7] = "2026-10-31"  # edited by hand afterwards
    dash._cache.update(t=0, rows=None)
    assert dash.undo_last(2, "1001") == "changed"
    assert H(fs) == "2026-10-31"


def test_undo_checks_account(fs):
    dash.write_last_service(2, "1001", __import__("datetime").date(2026, 10, 30))
    with pytest.raises(ValueError):
        dash.undo_last(2, "9999")


def test_cancel_reactivate_and_undo(fs):
    from datetime import date
    assert dash.write_status(2, "1001", True, date(2026, 11, 1))
    hdr = fs.tabs["Customers"][0]
    assert hdr[23:25] == ["Status", "Cancelled On"] and hdr[:23] == HDR  # added at the END
    assert fs.tabs["Customers"][1][23:25] == ["Cancelled", "2026-11-01"]
    assert fs.tabs["Undo Log"][1][7] == "cancel"
    assert dash.undo_last(2, "1001") == "undone"
    assert fs.tabs["Customers"][1][23:25] == ["Active", ""]
    assert dash.write_status(2, "1001", True, date(2026, 11, 1))
    assert dash.write_status(2, "1001", False, date(2026, 11, 1))
    assert fs.tabs["Customers"][1][23] == "Active"
    assert not any(l in ("H", "I", "K") for t, l, r in fs.written if t == "Customers")


def _login(c):
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    c.post("/dashboard/login", data={"username": "ryan", "password": "test-pass-123", "csrf": tok})
    return re.search(r"name=csrf value='([^']+)'", c.get("/dashboard").text).group(1)


def test_routes_flow(fs):
    c = TestClient(main.app)
    assert c.post("/dashboard/undo", data={"row": 2, "acct": "1001"}, follow_redirects=False).status_code == 303
    assert c.post("/dashboard/undo", data={"row": 2, "acct": "1001", "csrf": "x"},
                  follow_redirects=False).headers["location"] == "/dashboard/login"
    csrf = _login(c)
    r = c.post("/dashboard/serviced", data={"row": 2, "acct": "1001", "when": "2026-10-30", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=serviced&row=2"
    page = c.get(r.headers["location"]).text
    assert page.count('action="/dashboard/undo"') == 2  # flash + card
    assert "confirm('Undo" in page and 'class="btn sm ghost"' in page
    r = c.post("/dashboard/undo", data={"row": 2, "acct": "1001", "csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=undone" and H(fs) == "2026-07-28"
    r = c.post("/dashboard/cancel", data={"row": 2, "acct": "1001", "csrf": csrf}, follow_redirects=False)
    page = c.get(r.headers["location"]).text
    assert "<span>Cancelled</span>" in page and 'action="/dashboard/reactivate"' in page
    r = c.post("/dashboard/reactivate", data={"row": 2, "acct": "1001", "csrf": csrf}, follow_redirects=False)
    assert "<span>Cancelled</span>" not in c.get(r.headers["location"]).text
    c.cookies.clear()


def test_unauthenticated_cancel_redirects(fs):
    c = TestClient(main.app)
    for p in ("/dashboard/cancel", "/dashboard/reactivate", "/dashboard/undo"):
        r = c.post(p, data={"row": 2, "acct": "1001", "csrf": "x"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
    assert not fs.written
