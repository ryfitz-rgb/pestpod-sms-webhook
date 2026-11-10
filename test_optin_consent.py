"""Web opt-in form: the SMS consent box is optional. Offline: Postgres, Sheets and Twilio are faked."""
import inspect
import pytest
from fastapi.testclient import TestClient
import main
import pgsync

client = TestClient(main.app)
TEXT = "I agree to receive recurring automated text messages from Blain via PestPod ..."


@pytest.fixture
def saved(monkeypatch):
    rows = []
    monkeypatch.setattr(pgsync, "enabled", lambda: True)
    monkeypatch.setattr(pgsync, "save_optin", lambda *a, **k: rows.append((a, k)) or len(rows))

    # Any Sheets access or outbound SMS from this endpoint is a failure.
    def no_sheets():
        raise AssertionError("opt-in form must not touch the sheet")
    monkeypatch.setattr(main, "get_sheets", no_sheets)
    import twilio.rest

    class NoSMS:
        def __init__(self, *a, **k):
            raise AssertionError("opt-in form must never send SMS")
    monkeypatch.setattr(twilio.rest, "Client", NoSMS)
    return rows


def post(body, json=True):
    if json:
        return client.post("/api/v1/optin", json=body, headers={"origin": "https://www.pestpod.ai"})
    return client.post("/api/v1/optin", data=body)


def base(**kw):
    d = {"name": "Pat Example", "phone": "(555) 555-0142", "consent_text": TEXT, "tech": "blain",
         "page_url": "https://www.pestpod.ai/optin/blain"}
    d.update(kw)
    return d


def test_ticked_saved_with_consent(saved):
    r = post(base(consent=True))
    assert r.status_code == 200 and r.json() == {"ok": True, "sms_consent": True}
    (args, kw), = saved
    assert args[0] == "Pat Example" and args[1] == "+15555550142" and args[2] == TEXT
    assert args[5] == "blain" and kw["sms_consent"] is True
    assert r.headers["access-control-allow-origin"] == "https://www.pestpod.ai"


def test_unticked_saved_as_contact_without_consent(saved):
    r = post(base(consent=False))
    assert r.status_code == 200 and r.json() == {"ok": True, "sms_consent": False}
    (args, kw), = saved
    assert args[1] == "+15555550142" and kw["sms_consent"] is False


def test_missing_consent_is_false(saved):
    d = base()
    r = post(d)
    assert r.status_code == 200 and r.json()["sms_consent"] is False
    assert saved[0][1]["sms_consent"] is False


def test_unticked_without_consent_text_still_saved(saved):
    r = post({"name": "Pat", "phone": "5555550142", "consent": False})
    assert r.status_code == 200 and saved[0][1]["sms_consent"] is False


def test_ticked_requires_consent_text(saved):
    r = post({"name": "Pat", "phone": "5555550142", "consent": True})
    assert r.status_code == 400 and saved == []


def test_phone_required(saved):
    assert post(base(phone="", consent=True)).status_code == 400
    assert post(base(phone="  ", consent=False)).status_code == 400
    assert saved == []


@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), ("TRUE", True), ("on", True), ("yes", True), ("1", True), (1, True),
    (False, False), ("false", False), ("off", False), ("no", False), ("0", False), (0, False),
    ("", False), (None, False), ("maybe", False), ([], False),
])
def test_consent_values(saved, value, expected):
    r = post(base(consent=value))
    assert r.status_code == 200 and r.json()["sms_consent"] is expected
    assert saved[0][1]["sms_consent"] is expected


@pytest.mark.parametrize("form,expected", [({"consent": "on"}, True), ({"consent": "false"}, False), ({}, False)])
def test_form_encoded(saved, form, expected):
    body = {"name": "Pat", "phone": "5555550142", "consent_text": TEXT, **form}
    r = post(body, json=False)
    assert r.status_code == 200 and saved[0][1]["sms_consent"] is expected


def test_bad_json_is_400(saved):
    r = client.post("/api/v1/optin", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400 and saved == []


def test_storage_down_is_503(monkeypatch):
    monkeypatch.setattr(pgsync, "enabled", lambda: False)
    assert post(base(consent=True)).status_code == 503


def test_endpoint_never_texts_or_marks_opted_in():
    src = inspect.getsource(main.optin_signup)
    for bad in ("messages.create", "mark_customer_opted_in", "log_optin", "get_sheets", "batchUpdate",
                "values().update", "values().append"):
        assert bad not in src


def test_batch_never_reads_web_signups():
    import batch
    assert "optin_signups" not in inspect.getsource(batch)


def test_schema_migration_is_safe_and_defaults_false():
    s = pgsync.SCHEMA
    assert "ADD COLUMN IF NOT EXISTS sms_consent BOOLEAN NOT NULL DEFAULT true" in s  # old rows all ticked
    assert "ALTER COLUMN sms_consent SET DEFAULT false" in s                          # new rows: no consent
    assert "DROP" not in s.upper()


class FakeCur:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.log.append((sql, params))
    def fetchone(self): return (7,)


class FakeConn(FakeCur):
    def cursor(self): return FakeCur(self.log)
    def commit(self): pass


@pytest.mark.parametrize("flag", [True, False, None, "true"])
def test_save_optin_writes_boolean(monkeypatch, flag):
    log = []
    monkeypatch.setattr(pgsync, "connect", lambda: FakeConn(log))
    assert pgsync.save_optin("n", "+15555550142", "t", "ip", "ua", None, "url", sms_consent=flag) == 7
    sql, params = log[-1]
    assert "sms_consent" in sql and params[-1] is bool(flag)
