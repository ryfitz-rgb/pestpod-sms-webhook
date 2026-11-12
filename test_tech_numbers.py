"""Inbound SMS tech tagging from TECH_NUMBERS (Twilio To number -> tech)."""
import html
import pytest
from fastapi.testclient import TestClient
import main
from test_main import PHONE, FakeSvc


# Blain's number is not assigned yet; this is a stand-in used only in tests.
BLAIN = "+15045550111"
RYAN = "+15047320822"  # Ryan's live PestPod number: must stay untagged
STRANGER = "+15555550000"


@pytest.fixture
def svc(monkeypatch):
    s = FakeSvc()
    monkeypatch.setattr(main, "_service", s)
    monkeypatch.setattr(main, "_service_failed", False)
    monkeypatch.setattr(main, "_inbound_tab_ready", False)
    monkeypatch.setattr(main, "_optins_tab_ready", False)
    monkeypatch.setattr(main, "_tech_col_ready", set())
    main._cache.update(at=0.0, data=None)
    return s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("WEBHOOK_PUBLIC_URL", raising=False)
    monkeypatch.delenv("TECH_NUMBERS", raising=False)


client = TestClient(main.app)


def post(body, frm=PHONE, sid="SM123", to=""):
    data = {"From": frm, "Body": body, "MessageSid": sid}
    if to:
        data["To"] = to
    r = client.post("/api/v1/sms/webhook", data=data)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    return html.unescape(r.text)


def set_map(monkeypatch, raw="blain=+15045550111"):
    monkeypatch.setenv("TECH_NUMBERS", raw)


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize("raw,expect", [
    (None, {}),
    ("", {}),
    ("   ", {}),
    ("blain=+15045550111", {"5045550111": "blain"}),
    ("blain=5045550111", {"5045550111": "blain"}),
    ("blain=15045550111", {"5045550111": "blain"}),
    ("blain=(504) 555-0111", {"5045550111": "blain"}),
    ("blain=504-555-0111", {"5045550111": "blain"}),
    (" BLAIN = +1 504 555 0111 ", {"5045550111": "blain"}),
    ("blain=+15045550111,pat=+15045550222",
     {"5045550111": "blain", "5045550222": "pat"}),
    ("blain=+15045550111, not-a-pair, pat=+15045550222",
     {"5045550111": "blain", "5045550222": "pat"}),
    ("=+15045550111", {}),
    ("blain=", {}),
    ("blain=123", {}),  # fewer than 10 digits
])
def test_parse_tech_numbers(raw, expect):
    assert main.parse_tech_numbers(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("+15045550111", "5045550111"),
    ("5045550111", "5045550111"),
    ("15045550111", "5045550111"),
    ("(504) 555-0111", "5045550111"),
    ("504-555-0111", "5045550111"),
    ("+1 504 555 0111", "5045550111"),
    ("", ""),
    (None, ""),
    ("555", ""),
])
def test_last10_digits(raw, expect):
    assert main.last10_digits(raw) == expect


def test_tech_for_to_unset(monkeypatch):
    assert main.tech_for_to(BLAIN) is None
    assert main.tech_for_to(RYAN) is None
    assert main.tech_for_to("") is None


def test_tech_for_to_mapped(monkeypatch):
    set_map(monkeypatch)
    assert main.tech_for_to(BLAIN) == "blain"
    assert main.tech_for_to("5045550111") == "blain"
    assert main.tech_for_to("(504) 555-0111") == "blain"
    assert main.tech_for_to("15045550111") == "blain"
    assert main.tech_for_to("504-555-0111") == "blain"
    assert main.tech_for_to(RYAN) is None
    assert main.tech_for_to("+15555550000") is None
    assert main.tech_for_to("") is None


# ---------------------------------------------------------------- webhook: To=Blain -> tech=blain


def test_to_blain_yes_from_customer_tags_inbound(svc, monkeypatch):
    set_map(monkeypatch)
    t = post("Y", to=BLAIN)
    assert "text you a day and time soon" in t  # reply text unchanged
    row = svc.appends[0]
    assert row[1:5] == [PHONE, "Y", "SM123", 3]
    assert row[5] == "blain"
    assert {"range": "'Inbound Log'!F1", "values": [[main.TECH_HEADER]]} in svc.writes


@pytest.mark.parametrize("to", [
    "+15045550111",
    "5045550111",
    "15045550111",
    "(504) 555-0111",
    "504-555-0111",
    "+1 504 555 0111",
])
def test_to_blain_formatting_variants(svc, monkeypatch, to):
    set_map(monkeypatch)
    t = post("HELP", frm=STRANGER, to=to)
    assert "<Message>" not in t and "Response" in t
    row = svc.appends[0]
    assert row[2] == "HELP" and row[5] == "blain"


def test_to_blain_stop_and_help_silent_but_tagged(svc, monkeypatch):
    set_map(monkeypatch)
    for word, sid in (("STOP", "SMstop"), ("HELP", "SMhelp")):
        t = post(word, frm=STRANGER, sid=sid, to=BLAIN)
        assert "<Message>" not in t and "Response" in t
    assert [r[2:] for r in svc.appends] == [
        ["STOP", "SMstop", "", "blain"],
        ["HELP", "SMhelp", "", "blain"],
    ]
    assert not [w for w in svc.writes if str(w.get("range", "")).startswith("'Customers'")]


def test_to_blain_stranger_yes_tags_optins(svc, monkeypatch):
    set_map(monkeypatch)
    t = post("YES", frm=STRANGER, sid="SMyes", to=BLAIN)
    assert "<Message>" not in t and "Response" in t  # Twilio already replied
    inbound, optin = svc.appends
    assert inbound[1:] == [STRANGER, "YES", "SMyes", "", "blain"]
    assert optin[1:] == [STRANGER, "YES", "SMyes", "", "blain"]
    assert {"range": "'Opt-Ins'!F1", "values": [[main.TECH_HEADER]]} in svc.writes
    assert not [w for w in svc.writes if str(w.get("range", "")).startswith("'Customers'")]


def test_to_blain_stranger_join_tags_optins(svc, monkeypatch):
    set_map(monkeypatch)
    t = post("JOIN", frm=STRANGER, sid="SMjoin", to=BLAIN)
    assert main.JOIN_REPLY in t
    inbound, optin = svc.appends
    assert inbound[5] == "blain" and optin[5] == "blain"


# ---------------------------------------------------------------- webhook: To=Ryan or unset -> blank (5 columns, same as today)


def test_to_ryan_stays_blank(svc, monkeypatch):
    set_map(monkeypatch)
    t = post("Y", to=RYAN)
    assert "text you a day and time soon" in t
    row = svc.appends[0]
    assert row == [row[0], PHONE, "Y", "SM123", 3]
    assert len(row) == 5
    assert not [w for w in svc.writes if w.get("range") == "'Inbound Log'!F1"]


def test_env_unset_stays_blank_even_to_blain(svc):
    t = post("Y", to=BLAIN)
    assert "text you a day and time soon" in t
    row = svc.appends[0]
    assert row == [row[0], PHONE, "Y", "SM123", 3]
    assert len(row) == 5
    assert not [w for w in svc.writes if w.get("range") == "'Inbound Log'!F1"]


def test_env_unset_stranger_yes_optins_untagged(svc):
    post("YES", frm=STRANGER, sid="SMy", to=BLAIN)
    inbound, optin = svc.appends
    assert inbound[1:] == [STRANGER, "YES", "SMy", ""]
    assert optin[1:] == [STRANGER, "YES", "SMy", ""]
    assert not [w for w in svc.writes if w.get("range") in ("'Inbound Log'!F1", "'Opt-Ins'!F1")]


def test_missing_to_stays_blank(svc, monkeypatch):
    set_map(monkeypatch)
    post("Y")  # no To field at all
    assert len(svc.appends[0]) == 5
