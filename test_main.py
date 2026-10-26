"""Offline tests: Sheets is mocked, no network, no real phone numbers."""
import base64, html, json, re
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
    return html.unescape(r.text)


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
    t = post("maybe", frm="+15555550000")
    assert "No matching appointment found" in t
    assert svc.appends[0][4] == "" and not [w for w in svc.writes if w["range"].startswith("'Customers'")]


def test_unknown_number_n_not_marked(svc):
    t = post("N", frm="+15555550000")
    assert "No matching appointment found" in t
    assert len(svc.appends) == 1 and not [w for w in svc.writes if w["range"].startswith("'Customers'")]


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
    assert main.JOIN_REPLY in post("Y", frm="+15555550000")  # stranger Y = text-to-join
    assert "<Message>" not in post("YES", frm="+15555550000")  # Twilio auto-replies to YES
    assert "No matching appointment found" in post("N", frm="+15555550000")
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


# ---------------------------------------------------------------- text-to-join
import sys
from datetime import datetime as _dt

FULL_HEADER = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "OK to Text?", "Opt-In Status",
               "Opt-In Method", "Opt-In Date", "Proposed Appointment", "Reminder Status", "Last Reply"]
FULL_ROWS = [FULL_HEADER,
             ["T-1", "Sam", "555.555.0199", "FALSE", "Not asked", "", "", "", "Not sent", ""]]


@pytest.fixture
def full(svc, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROWS", FULL_ROWS)
    monkeypatch.setattr(main, "_optins_tab_ready", False)
    return svc


@pytest.mark.parametrize("word", ["JOIN", " join ", "Join", "REMIND", "remind"])
def test_join_known_customer_opts_in(full, word):
    t = post(word)
    assert main.JOIN_REPLY in t and t.count("<Message>") == 1
    today = _dt.now(main.TZ).date().isoformat()
    assert {"range": "'Customers'!D2", "values": [[True]]} in full.writes
    assert {"range": "'Customers'!E2", "values": [["Opted in"]]} in full.writes
    assert {"range": "'Customers'!F2", "values": [["Keyword JOIN"]]} in full.writes
    assert {"range": "'Customers'!G2", "values": [[today]]} in full.writes
    # No Last Reply / Reminder Status change, and no Opt-Ins row for a known customer.
    assert not [w for w in full.writes if w["range"] in ("'Customers'!I2", "'Customers'!J2")]
    assert "Opt-Ins" not in full.added
    assert len(full.appends) == 1 and full.appends[0][2] == word  # Inbound Log only


def test_join_stranger_logged_not_added(full):
    t = post("JOIN", frm="+15555550000", sid="SMjoin")
    assert main.JOIN_REPLY in t
    assert not [w for w in full.writes if w["range"].startswith("'Customers'")]
    assert "Opt-Ins" in full.added
    assert {"range": "'Opt-Ins'!A1", "values": [main.OPTINS_HEADERS]} in full.writes
    inbound, optin = full.appends
    ts, phone, kw, sid, row = optin
    assert (phone, kw, sid, row) == ("+15555550000", "JOIN", "SMjoin", "")
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d-0[56]:00$", ts)


def test_join_reply_is_compliant():
    r = main.JOIN_REPLY
    assert r.startswith("PestPod:") and "STOP" in r and "HELP" in r and "Msg & data rates" in r
    assert len(r) <= 160


def test_join_write_failure_still_replies(full):
    full.fail_writes = True
    assert main.JOIN_REPLY in post("JOIN")
    assert main.JOIN_REPLY in post("JOIN", frm="+15555550000")


def test_join_without_sheets(monkeypatch):
    monkeypatch.setattr(main, "_service", None)
    monkeypatch.setattr(main, "_service_failed", True)
    assert main.JOIN_REPLY in post("JOIN", frm="+15555550000")


def test_stop_start_help_still_silent(full):
    for w in ("STOP", "START", "HELP", "unstop"):
        assert "<Message>" not in post(w)


def test_version():
    assert "commit" in client.get("/version").json()


# ---------------------------------------------------------------- Y/YES as text-to-join
OPTED_ROWS = [FULL_HEADER,
              ["T-1", "Sam", "555.555.0199", "TRUE", "Opted in", "Signed ticket", "2026-10-01",
               "Tue 10/27 at 9 AM", "Sent", ""]]
OK_ONLY_ROWS = [FULL_HEADER,
                ["T-1", "Sam", "555.555.0199", "TRUE", "", "", "", "Tue 10/27 at 9 AM", "Sent", ""]]


@pytest.mark.parametrize("word", ["Y", "y", " y ", "y.", "Y!", "YES", "yes", "Yes.", "Yes!", " YES "])
def test_y_from_not_opted_in_customer_opts_in(full, word):
    t = post(word)
    if word.strip().rstrip(".!").upper() == "YES":
        # Twilio Advanced Opt-Out already auto-replied to YES: empty TwiML from us.
        assert "<Message>" not in t and "Response" in t
    else:
        assert main.JOIN_REPLY in t and t.count("<Message>") == 1
    assert "confirmed for" not in t
    today = _dt.now(main.TZ).date().isoformat()
    assert {"range": "'Customers'!D2", "values": [[True]]} in full.writes
    assert {"range": "'Customers'!E2", "values": [["Opted in"]]} in full.writes
    assert {"range": "'Customers'!F2", "values": [["Keyword JOIN"]]} in full.writes
    assert {"range": "'Customers'!G2", "values": [[today]]} in full.writes
    # Not treated as a confirm: Reminder Status / Last Reply untouched; no Opt-Ins row.
    assert not [w for w in full.writes if w["range"] in ("'Customers'!I2", "'Customers'!J2")]
    assert "Opt-Ins" not in full.added
    assert len(full.appends) == 1 and full.appends[0][2] == word and full.appends[0][4] == 2


@pytest.mark.parametrize("word", ["Y", "y.", "YES", "yes."])
def test_y_from_stranger_goes_to_optins(full, word):
    t = post(word, frm="+15555550000", sid="SMy")
    if word.rstrip(".").upper() == "YES":
        assert "<Message>" not in t and "Response" in t  # Twilio sent the welcome
    else:
        assert main.JOIN_REPLY in t and t.count("<Message>") == 1
    assert not [w for w in full.writes if w["range"].startswith("'Customers'")]
    assert "Opt-Ins" in full.added
    inbound, optin = full.appends
    assert inbound[2] == word
    assert optin[1:] == ["+15555550000", word.rstrip(".").upper(), "SMy", ""]


@pytest.mark.parametrize("word", ["y.", "YES", "yes!"])
@pytest.mark.parametrize("rows", [OPTED_ROWS, OK_ONLY_ROWS])
def test_y_from_opted_in_customer_confirms(full, monkeypatch, rows, word):
    monkeypatch.setattr(sys.modules[__name__], "ROWS", rows)
    t = post(word)
    assert t.count("<Message>") == 1  # confirm reply is unchanged for Y and YES
    assert "Thanks Sam, your pest control service is confirmed for Tue 10/27 at 9 AM." in t
    assert main.JOIN_REPLY not in t
    assert {"range": "'Customers'!J2", "values": [["Y"]]} in full.writes
    assert {"range": "'Customers'!I2", "values": [["Confirmed Y"]]} in full.writes
    assert not [w for w in full.writes if w["range"] in ("'Customers'!D2", "'Customers'!E2",
                                                          "'Customers'!F2", "'Customers'!G2")]
    assert "Opt-Ins" not in full.added


def test_n_from_not_opted_in_customer_not_marked(full):
    t = post("N")
    assert "Reply Y to confirm or N to reschedule" in t and "reschedule your" not in t
    assert not [w for w in full.writes if w["range"].startswith("'Customers'")]
    assert len(full.appends) == 1  # Inbound Log only


def test_n_from_opted_in_customer_reschedules(full, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROWS", OPTED_ROWS)
    assert "Got it Sam, we'll reschedule" in post("n")
    assert {"range": "'Customers'!I2", "values": [["Reschedule N"]]} in full.writes


def test_stop_start_help_silent_for_non_opted_in(full):
    for w in ("STOP", "START", "HELP", "stop."):
        assert "<Message>" not in post(w)
    assert not [w for w in full.writes if w["range"].startswith("'Customers'")]


def test_yes_join_write_failure_still_silent(full):
    full.fail_writes = True
    assert "<Message>" not in post("YES")
    assert "<Message>" not in post("YES", frm="+15555550000")
    assert main.JOIN_REPLY in post("Y", frm="+15555550000")


@pytest.mark.parametrize("word", ["START", "start", "UNSTOP", "Unstop."])
@pytest.mark.parametrize("frm", [PHONE, "+15555550000"])
def test_start_unstop_send_nothing(full, word, frm):
    t = post(word, frm=frm)
    assert "<Message>" not in t and "Response" in t
    assert not [w for w in full.writes if w["range"].startswith("'Customers'")]
    assert "Opt-Ins" not in full.added
    assert len(full.appends) == 1 and full.appends[0][2] == word  # Inbound Log only


# ---------------------------------------------------------------- fast reply / background writes
import asyncio, logging, threading, time
from fastapi import BackgroundTasks


def call_direct(body, frm=PHONE, sid="SMbg"):
    """Call the endpoint without TestClient so we can see state before background tasks run."""
    bt = BackgroundTasks()
    r = main.sms_webhook(background_tasks=bt, From=frm, Body=body, MessageSid=sid)
    return html.unescape(r.body.decode()), bt


@pytest.mark.parametrize("body,expect,row_writes", [
    ("Y", "Thanks Sam, your pest control service is confirmed for Tue 2026-10-27 9:00 AM.",
     [{"range": "'Customers'!G3", "values": [["Y"]]}, {"range": "'Customers'!F3", "values": [["Confirmed Y"]]}]),
    ("N", "Got it Sam, we'll reschedule",
     [{"range": "'Customers'!G3", "values": [["N"]]}, {"range": "'Customers'!F3", "values": [["Reschedule N"]]}]),
])
def test_reply_returned_before_writes_then_writes_run(svc, body, expect, row_writes):
    t, bt = call_direct(body)
    assert expect in t
    assert svc.writes == [] and svc.appends == []  # nothing written before the reply
    asyncio.run(bt())
    for w in row_writes:
        assert w in svc.writes
    assert svc.appends[0][1:] == [PHONE, body, "SMbg", 3]


def test_join_reply_returned_before_optin_writes(full):
    t, bt = call_direct("Y", frm="+15555550000", sid="SMj")
    assert main.JOIN_REPLY in t and full.appends == [] and full.added == []
    asyncio.run(bt())
    inbound, optin = full.appends
    assert inbound[2] == "Y" and optin[1:] == ["+15555550000", "Y", "SMj", ""]
    t, bt = call_direct("JOIN")
    assert main.JOIN_REPLY in t and not [w for w in full.writes if w["range"].startswith("'Customers'")]
    asyncio.run(bt())
    assert {"range": "'Customers'!E2", "values": [["Opted in"]]} in full.writes


def test_only_one_sheets_read_before_reply(svc, monkeypatch):
    reads = []
    orig = svc.get
    monkeypatch.setattr(svc, "get", lambda *a, **k: (reads.append(k or a), orig(*a, **k))[1])
    _, bt = call_direct("Y")
    assert len(reads) == 1  # just the Customers read
    reads.clear()
    _, bt2 = call_direct("maybe")
    assert reads == []  # second reply served from cache


def test_background_failure_logged_not_raised(svc, monkeypatch, caplog):
    def boom(*a, **k): raise RuntimeError("sheet down")
    monkeypatch.setattr(main, "update_customer_reply", boom)
    monkeypatch.setattr(main, "log_inbound", boom)
    caplog.set_level(logging.WARNING, logger="pestpod")
    assert "confirmed for" in post("Y")
    assert "background boom failed (RuntimeError)" in caplog.text


def test_stale_cache_served_and_refreshed_in_background(svc, monkeypatch):
    main.read_customers(svc)
    main._cache["at"] = time.time() - main.CACHE_TTL - 5  # stale but usable
    gate, done = threading.Event(), threading.Event()
    orig = svc.get
    def slow_get(*a, **k):
        gate.wait(5); r = orig(*a, **k); done.set(); return r
    monkeypatch.setattr(svc, "get", slow_get)
    t, _ = call_direct("Y")  # must not block on the slow read
    assert "confirmed for" in t
    gate.set(); assert done.wait(5)
    for _ in range(50):
        if not main._refreshing.is_set(): break
        time.sleep(0.01)
    assert time.time() - main._cache["at"] < main.CACHE_TTL


def test_very_old_cache_reads_synchronously(svc, monkeypatch):
    main.read_customers(svc)
    main._cache["at"] = time.time() - main.CACHE_MAX_STALE - 5
    reads = []
    orig = svc.get
    monkeypatch.setattr(svc, "get", lambda *a, **k: (reads.append(1), orig(*a, **k))[1])
    assert "confirmed for" in call_direct("Y")[0]
    assert reads == [1]


def test_write_refreshes_cache_for_next_reply(full, monkeypatch):
    # Not opted in -> Y opts in (background); the cache is refreshed so the next Y confirms.
    rows = [FULL_HEADER, list(FULL_ROWS[1])]
    monkeypatch.setattr(sys.modules[__name__], "ROWS", rows)
    orig_bu = full.batchUpdate
    def bu(spreadsheetId, body):
        e = orig_bu(spreadsheetId, body)
        def run():
            r = e.execute()
            for d in body.get("data", []):
                if d["range"] == "'Customers'!E2": rows[1][4] = "Opted in"
            return r
        return Exec(run)
    monkeypatch.setattr(full, "batchUpdate", bu)
    assert main.JOIN_REPLY in post("Y")
    assert "Thanks Sam" in post("Y")


def test_lifespan_warms_cache(svc):
    main._cache.update(at=0.0, data=None)
    with TestClient(main.app):
        for _ in range(100):
            if main._cache["data"] is not None: break
            time.sleep(0.01)
    assert main._cache["data"] is not None
