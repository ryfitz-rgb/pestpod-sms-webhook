import os
os.environ["DASHBOARD_PASSWORD"] = "test-pass-123"
os.environ.pop("GOOGLE_SA_JSON_B64", None)
import re
from datetime import date
from fastapi.testclient import TestClient
import main, dashboard

c = TestClient(main.app)


def _login(pw="test-pass-123"):
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    return c.post("/dashboard/login", data={"username": "ryan", "password": pw, "csrf": tok}, follow_redirects=False)


def test_requires_login():
    c.cookies.clear()
    assert c.get("/dashboard", follow_redirects=False).status_code == 303


def test_wrong_password():
    c.cookies.clear()
    assert _login("nope").status_code == 401


def test_login_and_csrf():
    c.cookies.clear()
    assert _login().status_code == 303
    r = c.post("/dashboard/serviced", data={"row": 9, "acct": "x", "when": "2026-10-01"}, follow_redirects=False)
    assert r.status_code == 403


def test_view_row_override_and_hidden():
    d = {"_row": 9, "Account": "1", "Customer Name": "A", "Next Due": 46323, "Office List Month": "2026-11",
         "Last Service Date": 46231, "Pest Notes": "anytime stop; Preferred time: mornings"}
    v = dashboard.view_row(d, date(2026, 10, 31))
    assert v["eff"] == (2026, 11) and v["pushed"] and v["anytime"] and v["prefs"]
    assert dashboard.is_hidden_row({"Account": "EX-001", "Customer Name": "x"})
    assert dashboard.is_hidden_row({"Account": "TEST-001", "Customer Name": "x"})


def test_version_unchanged():
    assert "commit" in c.get("/version").json()


def test_theme_toggle_on_login_page():
    import dashboard as d
    h = d._login_page("tok")
    assert 'id=theme-toggle' in h and "localStorage" in h
    assert h.index("prefers-color-scheme") < h.index("<style>")  # init runs before paint
    assert 'name=csrf value="tok"' in h


def test_theme_toggle_on_dashboard():
    import dashboard as d
    from datetime import date
    h = d.render_dashboard([], "c123", date(2026, 11, 1))
    assert 'id=theme-toggle' in h and "html[data-theme=dark]" in h
    assert "c123" in h and "/dashboard/logout" in h


# ---------------------------------------------------------------- Wants a time (Nov 14 2026)
def _r(row, acct, name, reply="", status="", appt="", due=46387, phone="+15555550101", cust_status="", city="Metairie"):
    return {"_row": row, "Account": acct, "Customer Name": name, "Phone (+1XXXXXXXXXX)": phone, "City": city,
            "Last Reply": reply, "Reminder Status": status, "Proposed Appointment": appt, "Next Due": due,
            "Status": cust_status}


WT_TODAY = date(2026, 11, 14)  # Next Due 46340 = 2026-11-14, 46387 = 2026-12-31 (future month)


def test_wants_time_rules():
    rows = [
        _r(2, "1", "Ann Y no appt", reply="Y", status="Confirmed Y", due=46340, phone="+15555550111"),
        _r(3, "2", "Bob future appt", reply="Y", status="Confirmed Y", appt=46350.375, due=46340),
        _r(4, "3", "Cy past appt", status="Confirmed Y", appt="Tue 10/27 at 9 AM", due=46300),
        _r(5, "4", "Di unreadable", reply="Y", appt="call first", due=46340),
        _r(6, "5", "Ed said N", reply="N", status="Reschedule N", due=46340),
        _r(7, "6", "Flo not due yet", reply="Y", status="Confirmed Y", due=46387),
        _r(8, "7", "Gus cancelled", reply="Y", status="Confirmed Y", due=46340, cust_status="Cancelled"),
        _r(9, "EX-001", "Example (EXAMPLE DATA)", reply="Y", status="Confirmed Y", due=46340),
        _r(10, "TEST-001", "Ryan", reply="Y", status="Confirmed Y", due=46340, phone="+15555550156"),
        _r(11, "8", "Hal string future", reply="Y", appt="Tue 2026-11-24 9:00 AM", due=46340),
        _r(12, "9", "Ivy no due date", reply="Y", due=""),
    ]
    got = dashboard.wants_time(rows, WT_TODAY)
    names = [x["name"] for x in got]
    assert set(names) == {"Ann Y no appt", "Cy past appt", "Di unreadable", "Ivy no due date"}
    ann = next(x for x in got if x["row"] == 2)
    assert ann["phone_last4"] == "0111" and ann["note"] == ""
    assert next(x for x in got if x["row"] == 4)["note"] == "Past appointment Oct 27"
    assert next(x for x in got if x["row"] == 5)["note"].startswith("Appointment unreadable")
    assert "TEST-001" in [x["acct"] for x in dashboard.wants_time(rows, WT_TODAY, include_test=True)]


def test_wants_time_render_last4_only_and_badge():
    rows = [_r(2, "1", "Ann", reply="Y", status="Confirmed Y", due=46340, phone="+15555550111")]
    wants = dashboard.wants_time(rows, WT_TODAY)
    h = dashboard.render_dashboard([], "c1", WT_TODAY, wants=wants)
    assert "id=wants-time" in h and "Wants a time" in h and "<span class=badge>1</span>" in h
    assert "href='#wants-time'" in h and "Phone ending 0111" in h
    assert "5555550111" not in h
    empty = dashboard.render_dashboard([], "c1", WT_TODAY, wants=[])
    assert "Nobody is waiting on a time." in empty and "#wants-time'" not in empty
    assert "Wants a time" not in dashboard.render_dashboard([], "c1", WT_TODAY)  # section only when passed


def test_wants_time_on_dashboard_behind_login(monkeypatch):
    rows = [_r(2, "1", "Ann Waiting", reply="Y", status="Confirmed Y", due=46340, phone="+15555550111")]
    monkeypatch.setattr(dashboard, "fetch_rows", lambda force=False: {"header": list(rows[0]), "rows": rows, "undo": []})
    monkeypatch.setattr(dashboard, "load_unmatched", lambda rows, force=False: ([], ""))
    c.cookies.clear()
    assert c.get("/dashboard", follow_redirects=False).status_code == 303   # still needs login
    assert _login().status_code == 303
    h = c.get("/dashboard").text
    sec = h[h.index("id=wants-time"):h.index("</details>", h.index("id=wants-time"))]
    assert "Ann Waiting" in sec and "Phone ending 0111" in sec and "5555550111" not in sec


def test_wants_time_works_with_postgres_store(monkeypatch):
    import gridstore, main
    HEAD = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "City", "Last Reply", "Reminder Status",
            "Proposed Appointment", "Status"]
    be = gridstore.MemoryBackend()
    be.add_tab("Customers")
    svc = gridstore.GridService(be)
    svc.spreadsheets().values().update(spreadsheetId="x", range="'Customers'!A1", valueInputOption="RAW",
                                       body={"values": [HEAD, ["1", "Ann PG", "+15555550111", "Kenner", "Y",
                                                               "Confirmed Y", "", ""]]}).execute()
    monkeypatch.setattr(main, "get_sheets", lambda: svc)
    data = dashboard.fetch_rows(force=True)
    got = dashboard.wants_time(data["rows"], WT_TODAY)
    assert [x["name"] for x in got] == ["Ann PG"] and got[0]["phone_last4"] == "0111"
