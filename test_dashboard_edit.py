"""Dashboard add/edit customer (DASHBOARD_EDIT_ENABLED). Runs against the Postgres-shaped store
(gridstore.MemoryBackend via CUSTOMER_STORE=postgres), the store that's live. No network, no texts."""
import json
import os
os.environ["DASHBOARD_PASSWORD"] = "test-pass-123"
os.environ.pop("GOOGLE_SA_JSON_B64", None)
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

import conversations
import customer_edit as ce
import dashboard as dash
import formulas as f
import gridstore as gs
import main

HDR = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Address", "City", "Service Type", "Frequency",
       "Last Service Date", "Next Due", "Wave", "OK to Text?", "Opt-In Status", "Opt-In Method", "Opt-In Date",
       "Proposed Appointment", "Reminder Status", "Last Reply", "Pest Notes", "Termite Notes", "Missed Count",
       "Office List Month", "Source", "Import Notes", "Status", "Cancelled On"]
S = f.serial_from_date
TODAY = date(2026, 11, 19)
COL = {h: i for i, h in enumerate(HDR)}


def cust(acct, name, phone, opt="Not asked", method="", last=None, city="Metairie"):
    r = [""] * len(HDR)
    r[0], r[1], r[2], r[4], r[6], r[11], r[12] = acct, name, phone, city, "Quarterly", opt, method
    r[7] = last if last is not None else S(date(2026, 8, 10))
    r[15] = "Sent"
    return r


@pytest.fixture
def be(monkeypatch):
    rows = [list(HDR),
            cust(1001, "Alice Test", "+15045550101"),
            cust("1002", "Bob Opted", "+15045550102", opt="Opted in", method="Keyword JOIN"),
            cust("1003", "Cy Stopped", "+15045550103", opt="Opted out (STOP)"),
            cust("1004", "Di Text Date", "", last="call first")]
    b = gs.MemoryBackend({"Customers": rows, "Undo Log": [dash.UNDO_HEADERS],
                          "Inbound Log": [main.INBOUND_HEADERS]})
    monkeypatch.setenv("CUSTOMER_STORE", "postgres")
    monkeypatch.setenv("DASHBOARD_EDIT_ENABLED", "true")
    monkeypatch.setattr(gs, "_service", gs.GridService(b))
    monkeypatch.setattr(conversations, "get_store", lambda: None)
    dash._cache.update(t=0, rows=None)
    yield b
    dash._cache.update(t=0, rows=None)


def cell(b, row, col):
    r = b.tabs["Customers"]["rows"].get(row, [])
    i = COL[col]
    return r[i] if i < len(r) else ""


def undo_rows(b):
    rows = b.tabs["Undo Log"]["rows"]
    return [rows[k] for k in sorted(rows)][1:]


def computed(row):
    return gs.service().spreadsheets().values().get(
        range=f"Customers!A{row}:Y{row}", valueRenderOption="UNFORMATTED_VALUE").execute()["values"][0]


# ---------------------------------------------------------------- flag
def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("DASHBOARD_EDIT_ENABLED", raising=False)
    assert not ce.enabled()
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("DASHBOARD_EDIT_ENABLED", v)
        assert ce.enabled()


# ---------------------------------------------------------------- edit
def test_edit_fields_and_undo_log(be):
    form = {"name": "Alice  Tester", "phone": "504.555.0199", "address": "9 New Rd", "city": "Kenner",
            "service": "Pest", "freq": "monthly", "last": "2026-11-01", "pest_notes": "gate code 12",
            "optin_status": "Not asked", "optin_method": "", "optin_date": "", "termite_notes": ""}
    assert ce.save_edit(2, "1001", form, TODAY) == "saved"
    assert cell(be, 2, "Customer Name") == "Alice Tester"
    assert cell(be, 2, "Phone (+1XXXXXXXXXX)") == "+15045550199"
    assert cell(be, 2, "Frequency") == "Monthly"
    assert cell(be, 2, "Last Service Date") == S(date(2026, 11, 1))
    assert computed(2)[COL["Next Due"]] == S(date(2026, 12, 1))  # formula follows; never written
    log = undo_rows(be)[-1]
    assert log[7] == "edit" and log[1] == 2 and log[2] == "1001"
    old, new = json.loads(log[8]), json.loads(log[9])
    assert old["Customer Name"] == "Alice Test" and new["Customer Name"] == "Alice Tester"
    assert old["Last Service Date"] == "2026-08-10" and new["Last Service Date"] == "2026-11-01"
    assert "Account" not in new and "Opt-In Status" not in new  # only changed columns
    assert not any(c[2] in (f.COL_I, f.COL_K) for c in be.changes if c[0] == "Customers")


def test_edit_undo_restores(be):
    ce.save_edit(2, "1001", {"name": "Zed", "city": "Kenner", "last": "2026-11-01"}, TODAY)
    dash._cache.update(t=0, rows=None)
    assert dash.undo_candidate(dash.fetch_rows(force=True), 2)[1]
    assert dash.undo_last(2, "1001") == "undone"
    assert cell(be, 2, "Customer Name") == "Alice Test" and cell(be, 2, "City") == "Metairie"
    assert cell(be, 2, "Last Service Date") == S(date(2026, 8, 10))
    assert undo_rows(be)[-1][6]  # marked undone


def test_edit_undo_refused_after_later_change(be):
    ce.save_edit(2, "1001", {"city": "Kenner"}, TODAY)
    be.tabs["Customers"]["rows"][2][COL["City"]] = "Harahan"
    assert dash.undo_last(2, "1001") == "changed"


def test_edit_nochange_and_absent_fields_untouched(be):
    assert ce.save_edit(2, "1001", {"name": "Alice Test", "phone": "(504) 555-0101", "city": "Metairie"},
                        TODAY) == "nochange"
    assert ce.save_edit(2, "1001", {"city": "Kenner"}, TODAY) == "saved"
    assert cell(be, 2, "Customer Name") == "Alice Test"


def test_edit_validation(be):
    for form, code in (({"phone": "555-0123"}, "badphone"), ({"name": "  "}, "badname"),
                       ({"last": "2027-01-01"}, "baddate"), ({"last": "13/45/2026"}, "baddate"),
                       ({"optin_status": "Maybe"}, "badoptin"), ({"city": "=HYPERLINK(1)"}, "badtext")):
        with pytest.raises(ce.EditError) as ex:
            ce.save_edit(2, "1001", form, TODAY)
        assert ex.value.code == code
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(2, "9999", {"city": "x"}, TODAY)
    assert ex.value.code == "moved"
    assert undo_rows(be) == []


def test_blank_date_box_keeps_text_date(be):
    assert ce.save_edit(5, "1004", {"last": "", "city": "Kenner"}, TODAY) == "saved"
    assert cell(be, 5, "Last Service Date") == "call first"


# ---------------------------------------------------------------- consent
def test_opt_in_needs_consent_and_method(be):
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(2, "1001", {"optin_status": "Opted in", "optin_method": "Verbal (in person)"}, TODAY)
    assert ex.value.code == "needconsent"
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(2, "1001", {"optin_status": "Opted in", "consent": "yes"}, TODAY)
    assert ex.value.code == "needmethod"
    assert cell(be, 2, "Opt-In Status") == "Not asked" and undo_rows(be) == []
    r = ce.save_edit(2, "1001", {"optin_status": "opted in", "optin_method": "Verbal (in person)",
                                 "consent": "yes"}, TODAY)
    assert r == "consent"
    assert cell(be, 2, "Opt-In Status") == "Opted in"
    assert cell(be, 2, "Opt-In Date") == S(TODAY)  # dated today when left blank
    assert computed(2)[COL["OK to Text?"]] is True
    log = undo_rows(be)[-1]
    assert log[7] == "optin" and json.loads(log[9])["Opt-In Status"] == "Opted in"
    assert dash.undo_last(2, "1001") == "undone"
    assert cell(be, 2, "Opt-In Status") == "Not asked" and cell(be, 2, "Opt-In Date") == ""


def test_phone_change_while_opted_in_needs_consent(be):
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(3, "1002", {"phone": "5045550999"}, TODAY)
    assert ex.value.code == "needconsent"
    assert ce.save_edit(3, "1002", {"phone": "5045550999", "consent": "yes"}, TODAY) == "consent"
    # same number typed differently is not a change
    assert ce.save_edit(3, "1002", {"phone": "+1 (504) 555-0999"}, TODAY) == "nochange"
    # opting out or clearing the phone never needs consent
    assert ce.save_edit(3, "1002", {"optin_status": "Opted out"}, TODAY) == "saved"


def test_opted_out_cannot_be_opted_in_here(be):
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(4, "1003", {"optin_status": "Opted in", "optin_method": "Verbal", "consent": "yes"}, TODAY)
    assert ex.value.code == "optedout"


def test_stop_in_texts_blocks_opt_in(be, monkeypatch):
    monkeypatch.setattr(conversations, "thread", lambda phone, **k: (
        [{"direction": "in", "body": "STOP", "status": None}], ""))
    with pytest.raises(ce.EditError) as ex:
        ce.save_edit(2, "1001", {"optin_status": "Opted in", "optin_method": "Verbal", "consent": "yes"}, TODAY)
    assert ex.value.code == "optedout"


# ---------------------------------------------------------------- add
def test_add_customer_and_undo(be):
    row, r = ce.add_customer({"acct": "2001", "name": "New Person", "phone": "(504) 555-0144", "city": "Kenner",
                              "freq": "quarterly", "last": "11/02/2026", "optin_status": ""}, TODAY)
    assert (row, r) == (6, "added")
    assert cell(be, 6, "Account") == "2001" and cell(be, 6, "Customer Name") == "New Person"
    assert cell(be, 6, "Phone (+1XXXXXXXXXX)") == "+15045550144"
    assert cell(be, 6, "Opt-In Status") == "Not asked" and cell(be, 6, "Source") == "Dashboard"
    vals = computed(6)
    assert vals[COL["Next Due"]] == S(date(2027, 2, 2)) and vals[COL["OK to Text?"]] is False
    log = undo_rows(be)[-1]
    assert log[7] == "add" and log[1] == 6 and log[2] == "2001"
    assert json.loads(log[8]) == {k: "" for k in json.loads(log[9])}
    assert dash.undo_last(6, "2001") == "undone"
    assert cell(be, 6, "Account") == "" and cell(be, 6, "Customer Name") == ""
    assert all(dash.is_hidden_row(d) for d in dash.fetch_rows(force=True)["rows"] if d["_row"] == 6)


def test_add_rejects_duplicates_and_bad_input(be):
    for form, code in (({"acct": "1001", "name": "X"}, "dupacct"),       # stored as a number
                       ({"acct": " 1002 ", "name": "X"}, "dupacct"),
                       ({"acct": "", "name": "X"}, "badacct"),
                       ({"acct": "bad;acct", "name": "X"}, "badacct"),
                       ({"acct": "3001", "name": ""}, "badname"),
                       ({"acct": "3001", "name": "X", "phone": "12"}, "badphone"),
                       ({"acct": "3001", "name": "X", "optin_status": "Opted in", "optin_method": "Verbal",
                         "phone": "5045550177"}, "needconsent")):
        with pytest.raises(ce.EditError) as ex:
            ce.add_customer(form, TODAY)
        assert ex.value.code == code, form
    assert undo_rows(be) == [] and 6 not in be.tabs["Customers"]["rows"]


def test_add_with_consent(be):
    row, r = ce.add_customer({"acct": "2002", "name": "Opt Person", "phone": "5045550155", "optin_status": "Opted in",
                              "optin_method": "Paper form", "consent": "yes"}, TODAY)
    assert r == "consent" and cell(be, row, "Opt-In Date") == S(TODAY)
    assert computed(row)[COL["OK to Text?"]] is True
    assert undo_rows(be)[-1][7] == "optin"


def test_write_cells_refuses_formula_and_bookkeeping_columns(be):
    m = main
    svc = m.get_sheets()
    for col in ("Next Due", "OK to Text?", "Reminder Status", "Status", "Account"):
        with pytest.raises(RuntimeError):
            ce.write_cells(svc, m, HDR, 2, {col: "x"}, ce.EDITABLE_COLS)


# ---------------------------------------------------------------- routes / UI
def _login(c):
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    c.post("/dashboard/login", data={"username": "ryan", "password": "test-pass-123", "csrf": tok})
    return re.search(r"name=csrf value='([^']+)'", c.get("/dashboard").text).group(1)


def test_routes_need_flag_login_and_csrf(be, monkeypatch):
    c = TestClient(main.app)
    r = c.post("/dashboard/customer/add", data={"acct": "9", "name": "x", "csrf": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard/login"
    csrf = _login(c)
    assert c.post("/dashboard/customer/add", data={"acct": "9", "name": "x"},
                  follow_redirects=False).status_code == 403
    assert c.post("/dashboard/customer/edit", data={"row": 2, "acct": "1001", "city": "x", "csrf": "bad"},
                  follow_redirects=False).status_code == 403
    monkeypatch.setenv("DASHBOARD_EDIT_ENABLED", "")
    assert c.post("/dashboard/customer/add", data={"acct": "9", "name": "x", "csrf": csrf},
                  follow_redirects=False).status_code == 404
    page = c.get("/dashboard").text
    assert "/dashboard/customer/add" not in page and "/dashboard/customer/edit" not in page
    assert 'action="/dashboard/edit"' in page  # the older phone/address/city edit stays as it was
    assert 6 not in be.tabs["Customers"]["rows"]
    c.cookies.clear()


def test_routes_flow(be):
    c = TestClient(main.app)
    csrf = _login(c)
    page = c.get("/dashboard").text
    assert "+ Add customer" in page and 'action="/dashboard/customer/edit"' in page
    assert "Customer agreed to get texts at this number" in page
    r = c.post("/dashboard/customer/add", data={"acct": "2003", "name": "Route Add", "phone": "5045550166",
                                                "csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=added&row=6"
    page = c.get(r.headers["location"]).text
    assert "Added." in page and "Route Add" in page and 'action="/dashboard/undo"' in page
    # duplicate: form comes back filled in with the error
    r = c.post("/dashboard/customer/add", data={"acct": "2003", "name": "Again Person", "csrf": csrf},
               follow_redirects=False)
    assert r.status_code == 400 and "already on the Customers list" in r.text and "Again Person" in r.text
    r = c.post("/dashboard/customer/edit", data={"row": 2, "acct": "1001", "city": "Kenner", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=saved&row=2"
    r = c.post("/dashboard/customer/edit", data={"row": 2, "acct": "1001", "optin_status": "Opted in",
                                                 "optin_method": "Verbal", "csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=needconsent&row=2"
    assert "tick" in c.get(r.headers["location"]).text
    c.cookies.clear()


def test_lists_never_show_full_phone(be):
    c = TestClient(main.app)
    _login(c)
    page = c.get("/dashboard").text
    # full numbers only inside Edit form inputs (value="..."), never as visible list text
    for full in ("+15045550101", "+15045550102"):
        for m_ in re.finditer(re.escape(full), page):
            before = page[max(0, m_.start() - 60):m_.start()]
            assert 'value="' in before or 'data-was="' in before
    c.cookies.clear()


def test_no_texts_sent(be, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tried to send a text")
    monkeypatch.setattr(conversations, "make_sender", boom)
    ce.add_customer({"acct": "2004", "name": "Quiet", "phone": "5045550188", "optin_status": "Opted in",
                     "optin_method": "Verbal", "consent": "yes"}, TODAY)
    ce.save_edit(2, "1001", {"city": "Kenner"}, TODAY)


# ---------------------------------------------------------------- follow-ups (Nov 19)
def test_old_edit_refuses_new_phone_for_opted_in(be):
    with pytest.raises(ValueError) as ex:
        dash.write_edit(3, "1002", "5045550999", "", "Metairie")
    assert str(ex.value) == "opted-in phone"
    assert cell(be, 3, "Phone (+1XXXXXXXXXX)") == "+15045550102" and undo_rows(be) == []
    # same number typed differently, clearing the phone, or address/city only: all fine
    assert dash.write_edit(3, "1002", "(504) 555-0102", "5 Oak St", "Metairie") == "saved"
    assert dash.write_edit(3, "1002", "", "5 Oak St", "Metairie") == "saved"
    # not opted in: phone changes are allowed as before
    assert dash.write_edit(2, "1001", "5045550998", "", "Metairie") == "saved"


def test_old_edit_route_message(be):
    c = TestClient(main.app)
    csrf = _login(c)
    r = c.post("/dashboard/edit", data={"row": 3, "acct": "1002", "phone": "5045550999", "address": "",
                                        "city": "Metairie", "csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=optinphone&row=3"
    assert "needs their consent" in c.get(r.headers["location"]).text
    c.cookies.clear()


def test_shared_phone_heads_up(be):
    row, _ = ce.add_customer({"acct": "2010", "name": "Same Phone", "phone": "504-555-0102"}, TODAY)
    rows = dash.fetch_rows(force=True)["rows"]
    note = ce.shared_phone_note(rows, row)
    assert "ending 0102" in note and "Acct 1002 · Bob Opted" in note and "(Acct 1002)" in note
    assert "5550102" not in note and "+1504" not in note  # last 4 only
    assert ce.shared_phone_note(rows, 2) == ""  # Alice's phone is hers alone
    # cancelled / hidden customers don't count
    be.tabs["Customers"]["rows"][3][COL["Status"]] = "Cancelled"
    assert ce.shared_phone_note(dash.fetch_rows(force=True)["rows"], row) == ""


def test_shared_phone_flash_does_not_block(be):
    c = TestClient(main.app)
    csrf = _login(c)
    r = c.post("/dashboard/customer/add", data={"acct": "2011", "name": "Twin Phone", "phone": "5045550101",
                                                "csrf": csrf}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=added&row=6"
    assert cell(be, 6, "Customer Name") == "Twin Phone"
    page = c.get(r.headers["location"]).text
    assert "Heads-up: the phone ending 0101 is also on Acct 1001" in page
    r = c.post("/dashboard/customer/edit", data={"row": 5, "acct": "1004", "phone": "5045550101", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=saved&row=5"
    assert "ending 0101 is also on" in c.get(r.headers["location"]).text
    c.cookies.clear()
