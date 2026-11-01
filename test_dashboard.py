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
