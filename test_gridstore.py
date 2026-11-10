"""CUSTOMER_STORE=postgres: the webhook, batch and dashboard run against gridstore (in-memory
backend here; no network, no Google, no Twilio, fake 555 numbers only)."""
import html
import os
from datetime import date, datetime

os.environ.setdefault("DASHBOARD_PASSWORD", "test-pass-123")
os.environ.setdefault("SHEET_ID", "test-sheet")

import pytest
from fastapi.testclient import TestClient

import batch
import dashboard as dash
import formulas as f
import gridstore as gs
import main

# Live Customers header, A..Y (2026-11-10).
HDR = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Address", "City", "Service Type", "Frequency",
       "Last Service Date", "Next Due", "Wave", "OK to Text?", "Opt-In Status", "Opt-In Method", "Opt-In Date",
       "Proposed Appointment", "Reminder Status", "Last Reply", "Pest Notes", "Termite Notes", "Missed Count",
       "Office List Month", "Source", "Import Notes", "Status", "Cancelled On"]
S = f.serial_from_date


def cust(acct, name, phone, freq="Quarterly", last=None, opt="Not asked", status="", notes=""):
    r = [""] * len(HDR)
    r[0], r[1], r[2], r[6], r[7], r[11], r[15], r[17] = acct, name, phone, freq, last if last is not None else "", opt, status, notes
    return r


@pytest.fixture
def grid(monkeypatch):
    rows = [list(HDR),
            cust("1001", "Alice Test", "+15555550101", last=S(date(2026, 8, 10)), opt="Opted in"),
            cust("1002", "Bob Test", "+15555550102", last=S(date(2026, 8, 12))),
            cust("1003", "Cara Anytime", "+15555550103", freq="Weekly", last=S(date(2026, 11, 2)), opt="Opted in",
                 status="Excluded - no SMS (anytime stop)", notes="Anytime stop, no texts"),
            cust("1004", "Dan NoPhone", "", last=S(date(2026, 8, 15)), opt="Opted in")]
    be = gs.MemoryBackend({"Customers": rows, "Outbound Log": [batch.OUTBOUND_HEADERS],
                           "Inbound Log": [main.INBOUND_HEADERS], "Undo Log": [dash.UNDO_HEADERS]})
    svc = gs.GridService(be)
    monkeypatch.setenv("CUSTOMER_STORE", "postgres")
    monkeypatch.setattr(gs, "_service", svc)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_PUBLIC_URL", raising=False)
    monkeypatch.setattr(main, "_inbound_tab_ready", False)
    monkeypatch.setattr(main, "_optins_tab_ready", False)
    main._cache.update(at=0.0, data=None)
    dash._cache.update(t=0, rows=None)
    return be


def cell(be, tab, r, letter):
    row = be.tabs[tab]["rows"].get(r, [])
    c = gs.col_index(letter)
    return row[c] if c < len(row) else ""


def read(svc, rng, mode="FORMATTED_VALUE"):
    return svc.spreadsheets().values().get(spreadsheetId="x", range=rng, valueRenderOption=mode).execute().get("values", [])


# ---------------------------------------------------------------- store + formulas
def test_get_sheets_switches_on_flag(grid, monkeypatch):
    assert isinstance(main.get_sheets(), gs.GridService)
    monkeypatch.setenv("CUSTOMER_STORE", "sheet")
    assert not isinstance(main.get_sheets(), gs.GridService)


def test_formula_columns_computed_on_read(grid):
    v = read(main.get_sheets(), "'Customers'")
    assert v[1][8] == "2026-11-10" and v[1][10] == "TRUE"      # Alice: Next Due + OK to Text
    assert v[2][10] == "FALSE"                                   # Bob not opted in
    assert v[4][10] == "FALSE"                                   # Dan: no phone
    u = read(main.get_sheets(), "'Customers'", "UNFORMATTED_VALUE")
    assert u[1][8] == S(date(2026, 11, 10)) and u[1][10] is True
    assert cell(grid, "Customers", 2, "I") == "" and cell(grid, "Customers", 2, "K") == ""  # never stored


@pytest.mark.parametrize("letter", ["I", "K"])
def test_writes_to_formula_columns_refused(grid, letter):
    svc = main.get_sheets()
    with pytest.raises(gs.GridError):
        svc.spreadsheets().values().update(spreadsheetId="x", range=f"'Customers'!{letter}2",
                                           valueInputOption="RAW", body={"values": [["x"]]}).execute()
    # a batch containing one bad cell writes nothing at all
    with pytest.raises(gs.GridError):
        svc.spreadsheets().values().batchUpdate(spreadsheetId="x", body={"valueInputOption": "RAW", "data": [
            {"range": "'Customers'!Q2", "values": [["Y"]]},
            {"range": f"'Customers'!{letter}2", "values": [["x"]]}]}).execute()
    assert cell(grid, "Customers", 2, "Q") == ""


def test_parse_range_and_user_entered():
    assert gs.parse_range("'Undo Log'!A:J") == ("Undo Log", 0, None, 9, None)
    assert gs.parse_range("'Customers'!G12") == ("Customers", 6, 12, 6, 12)
    assert gs.parse_range("'It''s'") == ("It's", None, None, None, None)
    assert gs.user_entered("2026-11-10") == S(date(2026, 11, 10))
    assert gs.user_entered("TRUE") is True and gs.user_entered("2026-12") == "2026-12"
    with pytest.raises(gs.GridError):
        gs.user_entered("=1+1")


def test_formatted_datetime():
    assert gs.formatted(46329.375, "Proposed Appointment") == "Tue 2026-11-03 9:00 AM"
    assert gs.formatted(46331.541666666664, "Proposed Appointment") == "Thu 2026-11-05 1:00 PM"


# ---------------------------------------------------------------- webhook
client = TestClient(main.app)


def post(body, frm):
    r = client.post("/api/v1/sms/webhook", data={"From": frm, "Body": body, "MessageSid": "SMtest"})
    assert r.status_code == 200
    return html.unescape(r.text)


def test_join_known_customer_writes_optin_not_k(grid):
    t = post("JOIN", "+15555550102")
    assert main.JOIN_REPLY in t
    assert cell(grid, "Customers", 3, "L") == "Opted in"
    assert cell(grid, "Customers", 3, "M") == main.JOIN_METHOD
    assert isinstance(cell(grid, "Customers", 3, "N"), int)      # USER_ENTERED date -> serial
    assert cell(grid, "Customers", 3, "K") == ""                 # K never written...
    assert read(main.get_sheets(), "'Customers'")[2][10] == "TRUE"   # ...but now computes TRUE
    assert grid.tabs["Inbound Log"]["rows"][2][1] == "+15555550102"


def test_y_from_opted_in_confirms(grid):
    t = post("Y", "+15555550101")
    assert main.CONFIRM_REPLY in t
    assert cell(grid, "Customers", 2, "Q") == "Y" and cell(grid, "Customers", 2, "P") == "Confirmed Y"


def test_n_from_opted_in_reschedules(grid):
    t = post("N", "+15555550101")
    assert "reach out to reschedule your appointment" in t and "Alice Test" in t
    assert cell(grid, "Customers", 2, "P") == "Reschedule N"


def test_stranger_join_goes_to_optins_tab(grid):
    post("JOIN", "+15555550999")
    assert "Opt-Ins" in grid.tabs
    rows = grid.tabs["Opt-Ins"]["rows"]
    assert rows[1] == main.OPTINS_HEADERS and rows[2][1:4] == ["+15555550999", "JOIN", "SMtest"]


def test_unmatched_and_stop(grid):
    assert "No matching appointment found" in post("hello", "+15555550999")
    assert "<Message>" not in post("STOP", "+15555550101")


# ---------------------------------------------------------------- batch
class Sender:
    def __init__(self): self.sent = []
    def __call__(self, to, body):
        self.sent.append((to, body)); return f"SM{len(self.sent)}"


def test_batch_dry_and_live_against_postgres(grid):
    now = datetime(2026, 11, 10, 10, 30, tzinfo=main.TZ)
    lines = []
    s = batch.run(now=now, dry_run=True, out=lines.append)
    assert s["planned"] == 1                                    # only Alice
    text = "\n".join(lines)
    assert "SKIP row 4 1003 Cara Anytime: Reminder Status is 'Excluded - no SMS (anytime stop)'" in text
    assert "SKIP row 5 1004 Dan NoPhone: OK to Text? not TRUE" in text     # computed K = FALSE (no phone)
    assert "SKIP row 3 1002 Bob Test: OK to Text? not TRUE" in text
    snd = Sender()
    s = batch.run(now=now, dry_run=False, sender=snd, sleep=lambda _s: None, clock=lambda: now, out=lambda *_: None)
    assert s["sent"] == 1 and snd.sent[0][0] == "+15555550101" and snd.sent[0][1] == batch.TEMPLATE
    assert cell(grid, "Customers", 2, "P") == "Nudge 1 sent"
    assert grid.tabs["Outbound Log"]["rows"][2][1] == "+15555550101"
    s = batch.run(now=now, dry_run=False, sender=snd, sleep=lambda _s: None, clock=lambda: now, out=lambda *_: None)
    assert s["sent"] == 0 and len(snd.sent) == 1                 # once per due cycle


# ---------------------------------------------------------------- dashboard writes
def test_dashboard_serviced_recomputes_next_due_and_undo(grid):
    dash.write_last_service(2, "1001", date(2026, 11, 9))
    assert cell(grid, "Customers", 2, "H") == S(date(2026, 11, 9))
    assert read(main.get_sheets(), "'Customers'")[1][8] == "2027-02-09"
    assert grid.tabs["Undo Log"]["rows"][2][7] == "serviced"
    assert dash.undo_last(2, "1001") == "undone"
    assert cell(grid, "Customers", 2, "H") == S(date(2026, 8, 10))


def test_dashboard_cancel_push_edit(grid):
    assert dash.write_status(3, "1002", True, date(2026, 11, 10)) is True
    assert cell(grid, "Customers", 3, "X") == "Cancelled"
    assert cell(grid, "Customers", 3, "Y") == S(date(2026, 11, 10))
    dash.write_push(2, "1001", date(2026, 11, 10))
    assert cell(grid, "Customers", 2, "U") == "2026-12"
    assert dash.write_edit(2, "1001", "(555) 555-0111", "1 Test St", "Testville") == "saved"
    assert cell(grid, "Customers", 2, "C") == "+15555550111"
    with pytest.raises(ValueError):
        dash.write_last_service(2, "WRONG", date(2026, 11, 9))   # row/account check still applies


# ---------------------------------------------------------------- import / verify / rollback export
class FakeGoogle:
    """A stand-in for the real sheet built from the same values (formulas pre-computed)."""
    def __init__(self, be):
        self.g = gs.GridService(be, "fake-google")
    def spreadsheets(self): return self
    def values(self): return self.g.values()
    def get(self, spreadsheetId=None, fields=None): return self.g.get()


def test_import_skips_formula_cols_and_verify_matches(grid):
    google = FakeGoogle(grid)
    target = gs.MemoryBackend()
    counts = gs.import_from_sheet(google, "x", target, out=lambda *_: None)
    assert counts["Customers"] == 4
    assert all(len(r) <= 8 or r[8] == "" for r in list(target.tabs["Customers"]["rows"].values())[1:])
    assert all(len(r) <= 10 or r[10] == "" for r in list(target.tabs["Customers"]["rows"].values())[1:])
    assert gs.verify_against_sheet(google, "x", gs.GridService(target), out=lambda *_: None) == 0
    with pytest.raises(gs.GridError):
        gs.import_from_sheet(google, "x", target, out=lambda *_: None)   # no silent overwrite


def test_verify_reports_differences(grid):
    google = FakeGoogle(grid)
    target = gs.MemoryBackend()
    gs.import_from_sheet(google, "x", target, out=lambda *_: None)
    target.write("Customers", [(2, 15, "Confirmed Y")], "test")
    assert gs.verify_against_sheet(google, "x", gs.GridService(target), out=lambda *_: None) == 2


def test_write_back_plan_never_includes_i_or_k(grid):
    plan = gs.write_back_plan(grid)
    assert plan and not [p for p in plan if p[0] == "Customers" and p[1] >= 2 and p[2] in (8, 10)]
