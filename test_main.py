"""Offline tests: Sheets is mocked, no network, no real phone numbers."""
import base64, json, re
import pytest
from fastapi.testclient import TestClient
import main

PHONE = "+15555550199"
HEADER = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Opt-In Status",
          "Proposed Appointment", "Reminder Status", "Last Reply"]
ROWS = [HEADER,
        ["EX-1", "Alice", "(555) 555-0101", "Opted in", "Mon 11/2 at 8 AM", "Pending", ""],
        ["T-1", "Sam", "555.555.0199", "Opted in", "Tue 2026-10-27 9:00 AM", "Pending", ""]]


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class FakeSvc:
    def __init__(self, tabs=("Customers",), fail_writes=False):
        self.tabs = set(tabs); self.writes = []; self.appends = []; self.added = []
        self.fail_writes = fail_writes
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, spreadsheetId, range=None, fields=None):
        if fields:
            return Exec(lambda: {"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        return Exec(lambda: {"values": ROWS})
    def batchUpdate(self, spreadsheetId, body):
        def run():
            if self.fail_writes: raise RuntimeError("boom")
            if "requests" in body:
                for r in body["requests"]:
                    t = r["addSheet"]["properties"]["title"]; self.tabs.add(t); self.added.append(t)
            else:
                self.writes.extend(body["data"])
            return {}
        return Exec(run)
    def update(self, spreadsheetId, range, valueInputOption, body):
        return Exec(lambda: self.writes.append({"range": range, "values": body["values"]}) or {})
    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def run():
            if self.fail_writes: raise RuntimeError("boom")
            self.appends.append(body["values"][0]); return {}
        return Exec(run)


@pytest.fixture
def svc(monkeypatch):
    s = FakeSvc()
    monkeypatch.setattr(main, "_service", s)
    monkeypatch.setattr(main, "_service_failed", False)
    monkeypatch.setattr(main, "_inbound_tab_ready", False)
    main._cache.update(at=0.0, data=None)
    return s


client = TestClient(main.app)


def post(body, frm=PHONE, sid="SM123"):
    r = client.post("/api/v1/sms/webhook", data={"From": frm, "Body": body, "MessageSid": sid})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    return r.text


def test_yes_confirms_and_writes(svc):
    t = post("y")
    assert "Thanks Sam, your pest control service is confirmed for Tue 2026-10-27 9:00 AM." in t
    assert {"range": "'Customers'!G3", "values": [["Y"]]} in svc.writes
    assert {"range": "'Customers'!F3", "values": [["Confirmed Y"]]} in svc.writes
    assert svc.added == ["Inbound Log"]
    assert {"range": "'Inbound Log'!A1", "values": [main.INBOUND_HEADERS]} in svc.writes
    ts, phone, body, sid, row = svc.appends[0]
    assert (phone, body, sid, row) == (PHONE, "y", "SM123", 3)
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d-0[56]:00$", ts)


def test_yes_word(svc):
    assert "confirmed for" in post("YES")


def test_no_reschedules(svc):
    t = post(" N ")
    assert "Got it Sam, we'll reschedule" in t
    assert {"range": "'Customers'!G3", "values": [["N"]]} in svc.writes
    assert {"range": "'Customers'!F3", "values": [["Reschedule N"]]} in svc.writes


def test_stop_silent_but_logged(svc):
    t = post("STOP")
    assert "<Message>" not in t and "Response" in t
    assert svc.appends[0][2] == "STOP" and svc.appends[0][4] == 3
    assert not [w for w in svc.writes if w["range"].startswith("'Customers'")]


def test_garbage(svc):
    assert "Reply Y to confirm or N to reschedule" in post("maybe?")
    assert not [w for w in svc.writes if w["range"].startswith("'Customers'")]


def test_unknown_number(svc):
    t = post("Y", frm="+15555550000")
    assert "No matching appointment found" in t
    assert svc.appends[0][4] == "" and not [w for w in svc.writes if w["range"].startswith("'Customers'")]


def test_existing_tab_not_recreated(svc):
    svc.tabs.add("Inbound Log"); post("Y")
    assert svc.added == []


def test_combined_failure_retries_last_reply(svc, monkeypatch):
    calls = []
    orig = svc.batchUpdate
    def flaky(spreadsheetId, body):
        calls.append(body)
        if "data" in body and len(body["data"]) > 1:
            return Exec(lambda: (_ for _ in ()).throw(RuntimeError("validation")))
        return orig(spreadsheetId, body)
    monkeypatch.setattr(svc, "batchUpdate", flaky)
    assert "confirmed for" in post("Y")
    data_calls = [c["data"] for c in calls if "data" in c]
    assert len(data_calls) == 2 and len(data_calls[1]) == 1
    assert {"range": "'Customers'!G3", "values": [["Y"]]} in svc.writes
    assert not [w for w in svc.writes if w["range"] == "'Customers'!F3"]


def test_write_failure_still_replies(svc):
    svc.fail_writes = True
    assert "confirmed for" in post("Y")


def test_missing_creds_falls_back(monkeypatch):
    monkeypatch.setattr(main, "_service", None)
    monkeypatch.setattr(main, "_service_failed", False)
    monkeypatch.delenv("GOOGLE_SA_JSON_B64", raising=False)
    monkeypatch.setattr(main, "TEST_PHONE", PHONE)
    assert "Thanks Ryan, your pest control service is confirmed for Tue 10/27 at 9 AM." in post("Y")
    assert "Got it Ryan" in post("n")
    assert "No matching appointment found" in post("Y", frm="+15555550000")
    assert "<Message>" not in post("HELP")


def test_invalid_creds_falls_back(monkeypatch, capsys, caplog):
    monkeypatch.setattr(main, "_service", None)
    monkeypatch.setattr(main, "_service_failed", False)
    monkeypatch.setenv("GOOGLE_SA_JSON_B64", base64.b64encode(b'{"private_key":"SECRETKEY"}').decode())
    monkeypatch.setattr(main, "TEST_PHONE", PHONE)
    assert "Thanks Ryan" in post("Y")
    assert "SECRETKEY" not in caplog.text + capsys.readouterr().out


def test_read_failure_falls_back(svc, monkeypatch):
    monkeypatch.setattr(svc, "get", lambda *a, **k: Exec(lambda: (_ for _ in ()).throw(RuntimeError())))
    monkeypatch.setattr(main, "TEST_PHONE", PHONE)
    assert "Thanks Ryan" in post("Y")


def test_normalize():
    assert main.normalize_phone("(555) 555-0199") == PHONE
    assert main.normalize_phone("15555550199") == PHONE
    assert main.normalize_phone(5555550199) == PHONE
    assert main.col_letter(0) == "A" and main.col_letter(26) == "AA"


def test_health():
    assert client.get("/health").json() == {"status": "ok"}
