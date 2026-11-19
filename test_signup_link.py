"""Link an unmatched web sign-up to a customer (signup_link.py, DASHBOARD_EDIT_ENABLED).
Offline: the customer store is gridstore.MemoryBackend, optin_signups is faked. No network, no texts."""
import json
import os
os.environ["DASHBOARD_PASSWORD"] = "test-pass-123"
os.environ.pop("GOOGLE_SA_JSON_B64", None)
import re
import urllib.request
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import alert_email
import batch
import conversations
import customer_edit as ce
import dashboard as dash
import formulas as f
import gridstore as gs
import main
import pgsync
import signup_link as sl

HDR = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Address", "City", "Service Type", "Frequency",
       "Last Service Date", "Next Due", "Wave", "OK to Text?", "Opt-In Status", "Opt-In Method", "Opt-In Date",
       "Proposed Appointment", "Reminder Status", "Last Reply", "Pest Notes", "Termite Notes", "Missed Count",
       "Office List Month", "Source", "Import Notes", "Status", "Cancelled On"]
COL = {h: i for i, h in enumerate(HDR)}
S = f.serial_from_date
TODAY = date(2026, 11, 19)
FULL = "5045550177"  # the sign-up's number: must never reach the browser


def cust(acct, name, phone, opt="Not asked", method="", city="Metairie"):
    r = [""] * len(HDR)
    r[0], r[1], r[2], r[4], r[6], r[11], r[12] = acct, name, phone, city, "Quarterly", opt, method
    r[7] = S(date(2026, 8, 10))
    return r


def su(i, name, phone, consent=True, when="2026-11-18T15:00:00+00:00"):
    return {"id": i, "created_at": datetime.fromisoformat(when), "name": name, "phone": phone,
            "sms_consent": consent, "tech": "blain", "page_url": "https://www.pestpod.ai/optin/blain"}


@pytest.fixture
def be(monkeypatch):
    rows = [list(HDR),
            cust(1001, "Alice Test", "+15045550101"),
            cust("1002", "Bob Opted", "+15045550102", opt="Opted in", method="Keyword JOIN"),
            cust("1003", "Cy Stopped", "", opt="Opted out (STOP)"),
            cust("1004", "Eddie Sample", ""),
            cust("TEST-1", "Test Row", "")]
    b = gs.MemoryBackend({"Customers": rows, "Undo Log": [dash.UNDO_HEADERS],
                          "Inbound Log": [main.INBOUND_HEADERS]})
    signups = [su(11, "Edward Sample", "(504) 555-0177"),
               su(12, "Edward Sample", "504.555.0177", when="2026-11-17T15:00:00+00:00"),
               su(13, "Contact Only", "5045550179", consent=False),
               su(14, "Already Bob", "5045550102", consent=True)]
    b.links = []
    monkeypatch.setenv("CUSTOMER_STORE", "postgres")
    monkeypatch.setenv("DASHBOARD_EDIT_ENABLED", "true")
    monkeypatch.setattr(gs, "_service", gs.GridService(b))
    monkeypatch.setattr(conversations, "get_store", lambda: None)
    monkeypatch.setattr(pgsync, "enabled", lambda: True)
    monkeypatch.setattr(pgsync, "list_optins", lambda: list(signups))
    monkeypatch.setattr(pgsync, "review_rows", lambda: [])
    monkeypatch.setattr(pgsync, "save_optin_link", lambda ids, acct, row: b.links.append((list(ids), acct, row)))
    dash._cache.update(t=0, rows=None)
    dash._unm_cache.update(t=0, val=None)
    yield b
    dash._cache.update(t=0, rows=None)
    dash._unm_cache.update(t=0, val=None)


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


def _login(c):
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    c.post("/dashboard/login", data={"username": "ryan", "password": "test-pass-123", "csrf": tok})
    return re.search(r"name=csrf value='([^']+)'", c.get("/dashboard").text).group(1)


def _unmatched_ids(c):
    return {i for x in c.get("/dashboard/unmatched.json").json()["entries"] for i in x["ids"]}


# ---------------------------------------------------------------- flag + auth
def test_flag_off_404_and_no_button(be, monkeypatch):
    c = TestClient(main.app)
    csrf = _login(c)
    assert "Link to customer" in c.get("/dashboard").text
    monkeypatch.setenv("DASHBOARD_EDIT_ENABLED", "")
    assert c.get("/dashboard/signup/link?id=11", follow_redirects=False).status_code == 404
    assert c.get("/dashboard/signup/link?id=11&row=5&acct=1004", follow_redirects=False).status_code == 404
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    assert r.status_code == 404
    assert "Link to customer" not in c.get("/dashboard").text
    assert cell(be, 5, "Phone (+1XXXXXXXXXX)") == "" and undo_rows(be) == []
    c.cookies.clear()


def test_needs_login_and_csrf(be):
    c = TestClient(main.app)
    assert c.get("/dashboard/signup/link?id=11", follow_redirects=False).headers["location"] == "/dashboard/login"
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": "x"},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard/login"
    _login(c)
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": "bad"},
               follow_redirects=False)
    assert r.status_code == 403
    assert cell(be, 5, "Phone (+1XXXXXXXXXX)") == "" and undo_rows(be) == []
    c.cookies.clear()


# ---------------------------------------------------------------- happy path
def test_link_flow_happy_path(be):
    c = TestClient(main.app)
    csrf = _login(c)
    page = c.get("/dashboard").text
    assert 'href="/dashboard/signup/link?id=11"' in page          # latest submission of that person
    assert "/dashboard/signup/link?id=13" not in page             # contact only: no button
    pick = c.get("/dashboard/signup/link?id=11")
    assert pick.status_code == 200 and "Eddie Sample" in pick.text and "Phone ending 0177" in pick.text
    assert "Test Row" not in pick.text                            # hidden rows can't be picked
    assert pick.text.index("Eddie Sample") < pick.text.index("Alice Test")  # likely match first
    conf = c.get("/dashboard/signup/link?id=11&row=5&acct=1004")
    assert conf.status_code == 200
    for want in ("Confirm link</button>", "Edward Sample", "ending 0177", "Acct 1004", "No phone on file", "2026-11-18",
                 "Web form", "No text is sent"):
        assert want in conf.text, want
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard?msg=linked&row=5"
    assert cell(be, 5, "Phone (+1XXXXXXXXXX)") == "+1" + FULL
    assert cell(be, 5, "Opt-In Status") == "Opted in" and cell(be, 5, "Opt-In Method") == "Web form"
    assert cell(be, 5, "Opt-In Date") == S(date(2026, 11, 18))    # the sign-up's day, not today
    assert computed(5)[COL["OK to Text?"]] is True                # formula turns on by itself
    assert be.links == [([11, 12], "1004", 5)] or be.links == [([12, 11], "1004", 5)]
    done = c.get(r.headers["location"]).text
    assert "Linked." in done and "No text was sent" in done
    assert not ({11, 12} & _unmatched_ids(c))                     # left the unmatched list
    assert 'href="/dashboard/signup/link?id=11"' not in done
    c.cookies.clear()


def test_undo_log_entry_and_undo(be):
    sl.link(11, 5, "1004", "yes")
    (u,) = undo_rows(be)
    assert u[1] == 5 and u[2] == "1004" and u[3] == "Eddie Sample" and u[7] == "link"
    old, new = json.loads(u[8]), json.loads(u[9])
    assert old == {"Opt-In Date": "", "Opt-In Method": "", "Opt-In Status": "Not asked", "Phone (+1XXXXXXXXXX)": ""}
    assert new == {"Opt-In Date": "2026-11-18", "Opt-In Method": "Web form", "Opt-In Status": "Opted in",
                   "Phone (+1XXXXXXXXXX)": "+1" + FULL}
    assert dash.undo_last(5, "1004") == "undone"
    assert cell(be, 5, "Phone (+1XXXXXXXXXX)") == "" and cell(be, 5, "Opt-In Status") == "Not asked"
    dash._unm_cache.update(t=0, val=None)
    entries, _ = dash.load_unmatched(dash.fetch_rows(force=True)["rows"], force=True)
    assert any(11 in x["ids"] for x in entries)                   # back on the list after Undo


# ---------------------------------------------------------------- refusals
def test_confirm_required(be):
    for confirm in (None, "", "no"):
        with pytest.raises(sl.LinkError) as ex:
            sl.link(11, 5, "1004", confirm)
        assert ex.value.code == "needconfirm"
    c = TestClient(main.app)
    csrf = _login(c)
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard/signup/link?id=11&row=5&acct=1004&err=needconfirm"
    assert "Nothing was saved" in c.get(r.headers["location"]).text
    assert cell(be, 5, "Phone (+1XXXXXXXXXX)") == "" and undo_rows(be) == [] and be.links == []
    c.cookies.clear()


def test_opted_out_refused(be):
    c = TestClient(main.app)
    csrf = _login(c)
    conf = c.get("/dashboard/signup/link?id=11&row=4&acct=1003").text
    assert "opted out (STOP)" in conf and "Confirm link</button>" not in conf
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 4, "acct": "1003", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"].endswith("&err=optedout")
    assert cell(be, 4, "Phone (+1XXXXXXXXXX)") == "" and cell(be, 4, "Opt-In Status") == "Opted out (STOP)"
    assert undo_rows(be) == [] and be.links == []
    c.cookies.clear()


def test_stop_in_texts_refused(be, monkeypatch):
    monkeypatch.setattr(ce, "_stop_in_texts", lambda phone: phone == "+1" + FULL)
    with pytest.raises(sl.LinkError) as ex:
        sl.link(11, 5, "1004", "yes")
    assert ex.value.code == "optedout"
    assert sl.preview(11, 5, "1004")["refused"] == "optedout"
    assert undo_rows(be) == []


def test_already_linked_refused(be):
    sl.link(11, 5, "1004", "yes")
    for sid in (11, 12):  # either submission of that person
        with pytest.raises(sl.LinkError) as ex:
            sl.link(sid, 2, "1001", "yes")
        assert ex.value.code == "linked"
    c = TestClient(main.app)
    csrf = _login(c)
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 2, "acct": "1001", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=link_linked#unmatched"
    assert "unmatched any more" in c.get("/dashboard?msg=link_linked").text
    assert cell(be, 2, "Phone (+1XXXXXXXXXX)") == "+15045550101" and len(undo_rows(be)) == 1
    c.cookies.clear()


def test_contact_only_and_unknown_refused(be):
    with pytest.raises(sl.LinkError) as ex:
        sl.link(13, 5, "1004", "yes")
    assert ex.value.code == "noconsent"
    with pytest.raises(sl.LinkError) as ex:
        sl.link(999, 5, "1004", "yes")
    assert ex.value.code == "nosignup"
    with pytest.raises(sl.LinkError) as ex:
        sl.link(11, 5, "9999", "yes")                             # row moved / wrong account
    assert ex.value.code == "moved"
    with pytest.raises(sl.LinkError) as ex:
        sl.link(11, 6, "TEST-1", "yes")
    assert ex.value.code == "hidden"
    assert undo_rows(be) == []


def test_warnings_for_different_phone_and_shared_phone(be):
    v = sl.preview(11, 2, "1001")
    assert v["refused"] == "" and v["cur_last4"] == "0101"
    assert any("different phone (ending 0101)" in w for w in v["warnings"])
    v = sl.preview(14, 2, "1001")                                 # Bob's number onto Alice
    assert any("also on Acct 1002 · Bob Opted" in w for w in v["warnings"])
    assert "5550101" not in json.dumps(v) and FULL not in json.dumps(v)


# ---------------------------------------------------------------- no texts, masking
def test_no_texts_sent(be, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tried to send a text or email")
    monkeypatch.setattr(conversations, "make_sender", boom)
    monkeypatch.setattr(batch, "make_twilio_sender", boom)
    monkeypatch.setattr(alert_email, "_post_resend", boom)
    monkeypatch.setattr(alert_email, "send_y_alert", boom)
    monkeypatch.setattr(alert_email, "send_message_alert", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    c = TestClient(main.app)
    csrf = _login(c)
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    assert r.headers["location"] == "/dashboard?msg=linked&row=5"
    assert cell(be, 5, "Opt-In Status") == "Opted in"
    c.cookies.clear()


def test_full_number_never_in_link_responses(be):
    c = TestClient(main.app)
    csrf = _login(c)
    bodies = [c.get("/dashboard").text,
              c.get("/dashboard/unmatched.json").text,
              c.get("/api/v1/optin/unmatched", auth=("ryan", "test-pass-123")).text,
              c.get("/dashboard/signup/link?id=11").text,
              c.get("/dashboard/signup/link?id=11&row=5&acct=1004").text,
              c.get("/dashboard/signup/link?id=11&row=2&acct=1001").text,
              c.get("/dashboard/signup/link?id=11&row=4&acct=1003").text]
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "csrf": csrf},
               follow_redirects=False)
    bodies += [r.text, r.headers["location"], c.get(r.headers["location"]).text]
    r = c.post("/dashboard/signup/link", data={"id": 11, "row": 5, "acct": "1004", "confirm": "yes", "csrf": csrf},
               follow_redirects=False)
    bodies += [r.text, r.headers["location"], c.get("/dashboard/unmatched.json").text,
               c.get("/dashboard/signup/link?id=11", follow_redirects=False).headers.get("location", "")]
    for b in bodies:
        assert FULL not in b and "555-0177" not in b and "555.0177" not in b
    # After the link, the main dashboard holds the number only where every customer's number already
    # lives: inside that customer's Edit form inputs (value= / data-was=), never as visible text.
    page = c.get("/dashboard?msg=linked&row=5").text
    for m_ in re.finditer(re.escape("+1" + FULL), page):
        before = page[max(0, m_.start() - 60):m_.start()]
        assert 'value="' in before or 'data-was="' in before
    c.cookies.clear()
