"""Unmatched web sign-ups: matching, last-4 masking, consent flag, dashboard section and JSON endpoint.
Offline: Postgres and Sheets are faked; nothing is written anywhere."""
import base64
import json
import os
import re
from datetime import datetime, timezone

os.environ.setdefault("DASHBOARD_PASSWORD", "test-pass-123")
os.environ.pop("GOOGLE_SA_JSON_B64", None)
import pytest
from fastapi.testclient import TestClient
import main
import dashboard
import pgsync
import unmatched as u

PW = os.environ["DASHBOARD_PASSWORD"]


def cust(row, acct, name, phone, status="", method="", date=""):
    return {"_row": row, "Account": acct, "Customer Name": name, "Phone (+1XXXXXXXXXX)": phone,
            "Opt-In Status": status, "Opt-In Method": method, "Opt-In Date": date}


def su(i, name, phone, consent=True, when="2026-11-11T13:25:00+00:00", tech="blain",
       page="https://www.pestpod.ai/optin/blain?utm=x"):
    return {"id": i, "created_at": datetime.fromisoformat(when), "name": name, "phone": phone,
            "sms_consent": consent, "tech": tech, "page_url": page}


CUSTOMERS = [
    cust(5, "1001", "Sydney Faucheaux", "+15045550101", "Opted in", "web form", "2026-11-05"),
    cust(6, "1002", "Marcus Boudreaux", "504-555-0102"),                      # not marked
    cust(7, "1003", "Ana Thibodeaux", "(504) 555-0103", "Opted in", "", ""),   # partly marked
    cust(8, "1004", "Gail Hebert", ""),
]


# ---------------------------------------------------------------- pure logic
def test_phone_normalization_and_last4():
    assert u.digits10("+1 (504) 555-0101") == u.digits10("504.555.0101") == u.digits10("15045550101") == "5045550101"
    assert u.digits10("555-01") == ""
    assert u.last4("+1 (504) 555-0101") == "0101" and u.last4("") == ""


def test_is_marked_needs_all_three():
    assert u.is_marked(CUSTOMERS[0])
    assert not u.is_marked(CUSTOMERS[1]) and not u.is_marked(CUSTOMERS[2])
    assert not u.is_marked(cust(1, "x", "y", "z", "Opted out", "web", "2026-11-01"))


def test_matched_and_marked_is_not_flagged():
    assert u.find_unmatched([su(1, "Sydney", "(504) 555-0101")], CUSTOMERS) == []


def test_no_phone_match_with_likely_name_match():
    (x,) = u.find_unmatched([su(2, "Gail Hebert", "504 555 9999")], CUSTOMERS)
    assert x["reasons"] == ["no phone match"] and x["actionable"] and x["consent"]
    assert x["likely_match"][0]["account"] == "1004" and x["likely_match"][0]["name"] == "Gail Hebert"
    assert x["phone_last4"] == "9999"


def test_first_name_only_fuzzy_match():
    (x,) = u.find_unmatched([su(2, "sydney", "5045559998")], CUSTOMERS)
    assert any(m["account"] == "1001" for m in x["likely_match"])
    assert u.name_score("Marcus Boudreau", "Marcus Boudreaux") >= u.NAME_RATIO
    assert u.name_score("Bob Smith", "Marcus Boudreaux") < u.NAME_RATIO


def test_matched_but_not_marked():
    got = u.find_unmatched([su(3, "Marcus", "5045550102"), su(4, "Ana T", "+15045550103")], CUSTOMERS)
    assert {x["phone_last4"]: x["reasons"] for x in got} == {"0102": ["matched but not marked"],
                                                              "0103": ["matched but not marked"]}
    assert all(x["likely_match"] == [] and x["matched_rows"] for x in got)


def test_no_consent_flagged_as_contact_only_even_when_matched():
    got = u.find_unmatched([su(5, "Sydney", "5045550101", consent=False), su(6, "New Person", "5045557777", consent=False)],
                           CUSTOMERS)
    assert len(got) == 2 and all(x["reasons"] == ["no consent"] and not x["consent"] and not x["actionable"] for x in got)
    assert u.summary(got) == {"actionable": 0, "no_consent": 2, "total": 2}


def test_repeat_submissions_grouped_any_consent_counts():
    got = u.find_unmatched([su(7, "Kim", "5045554444", consent=False, when="2026-11-01T15:00:00+00:00"),
                            su(8, "Kim Lee", "504-555-4444", consent=True, when="2026-11-02T15:00:00+00:00")], CUSTOMERS)
    (x,) = got
    assert x["ids"] == [7, 8] and x["consent"] and x["name"] == "Kim Lee" and x["submissions"] == 2


def test_time_in_central_page_path_only():
    (x,) = u.find_unmatched([su(9, "Zed", "5045553333", when="2026-11-11T13:25:00+00:00")], CUSTOMERS)
    assert x["submitted_ct"] == "2026-11-11 07:25" and x["submitted_label"].endswith("7:25 AM CT")
    assert x["page"] == "blain · /optin/blain"   # no host, no query string


def test_import_review_is_hint_only():
    review = [{"source_tab": "Import Review - Oct", "sheet_row": 12, "name": "Zed Z", "phone": "+15045553333"}]
    (x,) = u.find_unmatched([su(9, "Zed", "5045553333")], CUSTOMERS, review)
    assert x["reasons"] == ["no phone match"] and x["import_review_hint"][0]["row"] == 12


def test_no_full_phone_anywhere_in_output():
    got = u.find_unmatched([su(2, "Gail Hebert", "+15045559999"), su(3, "Marcus", "5045550102")], CUSTOMERS,
                           [{"source_tab": "Import Review", "sheet_row": 3, "name": "G", "phone": "5045559999"}])
    blob = json.dumps(got)
    for full in ("5045559999", "5045550102", "555-0102", "555 9999"):
        assert full not in blob


# ---------------------------------------------------------------- dashboard + endpoint
SIGNUPS = [su(1, "Sydney", "5045550101"), su(2, "Gail Hebert", "5045559999"),
           su(3, "Walk In", "5045558888", consent=False)]


@pytest.fixture
def faked(monkeypatch):
    monkeypatch.setattr(dashboard, "fetch_rows", lambda force=False: {"header": [], "rows": CUSTOMERS, "undo": []})
    monkeypatch.setattr(pgsync, "enabled", lambda: True)
    monkeypatch.setattr(pgsync, "list_optins", lambda: list(SIGNUPS))
    monkeypatch.setattr(pgsync, "review_rows", lambda: [])
    dashboard._unm_cache.update(t=0, val=None)

    def no_write(*a, **k):
        raise AssertionError("unmatched report must not write")
    monkeypatch.setattr(pgsync, "save_optin", no_write)
    monkeypatch.setattr(dashboard, "_write_edit_cells", no_write)
    monkeypatch.setattr(dashboard, "_write_status", no_write)
    yield
    dashboard._unm_cache.update(t=0, val=None)


def _client_logged_in():
    c = TestClient(main.app)
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    assert c.post("/dashboard/login", data={"username": "ryan", "password": PW, "csrf": tok},
                  follow_redirects=False).status_code == 303
    return c


def test_render_section_and_badge():
    entries = u.find_unmatched(SIGNUPS, CUSTOMERS)
    h = dashboard.render_dashboard([], "c1", datetime(2026, 11, 11).date(), signups=(entries, ""))
    assert "Unmatched sign-ups" in h and "id=unmatched" in h
    assert "<a class=badge href='#unmatched'" in h and ">1</a>" in h      # 1 actionable (Gail)
    assert "Likely match: Acct 1004 · Gail Hebert" in h
    assert "No consent · contact only" in h and "1 contact only" in h
    assert "Phone ending 9999" in h and "5045559999" not in h and "Sydney" not in h


def test_render_empty_and_error():
    d = datetime(2026, 11, 11).date()
    assert "All web sign-ups match" in dashboard.render_dashboard([], "c", d, signups=([], ""))
    h = dashboard.render_dashboard([], "c", d, signups=([], "Couldn't read web sign-ups right now."))
    assert "read web sign-ups right now" in h and "class=badge" not in h
    assert "Unmatched sign-ups" not in dashboard.render_dashboard([], "c", d)  # old call unchanged


def test_dashboard_page_shows_section(faked):
    c = _client_logged_in()
    r = c.get("/dashboard")
    assert r.status_code == 200 and "Unmatched sign-ups" in r.text and "Gail Hebert" in r.text
    assert "5045559999" not in r.text


def test_dashboard_still_loads_when_db_down(faked, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(pgsync, "list_optins", boom)
    r = _client_logged_in().get("/dashboard")
    assert r.status_code == 200 and "read web sign-ups right now" in r.text


def test_json_requires_auth(faked):
    c = TestClient(main.app)
    assert c.get("/api/v1/optin/unmatched").status_code == 401
    assert c.get("/dashboard/unmatched.json").status_code == 401
    bad = base64.b64encode(b"ryan:wrong").decode()
    assert c.get("/api/v1/optin/unmatched", headers={"authorization": "Basic " + bad}).status_code == 401
    # Basic auth is only for the /api path, not the cookie-only dashboard path
    ok = base64.b64encode(f"ryan:{PW}".encode()).decode()
    assert c.get("/dashboard/unmatched.json", headers={"authorization": "Basic " + ok}).status_code == 401
    dashboard._fail.clear()


def test_json_basic_auth_and_session(faked):
    ok = base64.b64encode(f"ryan:{PW}".encode()).decode()
    r = TestClient(main.app).get("/api/v1/optin/unmatched", headers={"authorization": "Basic " + ok})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert (j["actionable"], j["no_consent"], j["total"]) == (1, 1, 2)
    gail = next(x for x in j["entries"] if x["name"] == "Gail Hebert")
    assert gail["phone_last4"] == "9999" and gail["reasons"] == ["no phone match"] and gail["consent"] is True
    assert "phone" not in gail and "5045559999" not in r.text and "5045558888" not in r.text
    assert _client_logged_in().get("/dashboard/unmatched.json").json()["total"] == 2


def test_json_throttles_bad_basic(faked):
    dashboard._fail.clear()
    c = TestClient(main.app)
    bad = {"authorization": "Basic " + base64.b64encode(b"ryan:nope").decode()}
    for _ in range(10):
        c.get("/api/v1/optin/unmatched", headers=bad)
    ok = {"authorization": "Basic " + base64.b64encode(f"ryan:{PW}".encode()).decode()}
    assert c.get("/api/v1/optin/unmatched", headers=ok).status_code == 401   # locked out for 15 min
    dashboard._fail.clear()
