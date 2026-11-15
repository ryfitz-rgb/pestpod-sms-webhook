"""Offline tests for the Y-alert email. No network; Resend is mocked."""
from datetime import datetime
import logging
import pytest
import alert_email
from alert_email import TZ


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("ALERT_EMAIL_ENABLED", "ALERT_EMAIL", "ALERT_EMAIL_FROM", "RESEND_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def _enable(monkeypatch, enabled="true"):
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", enabled)
    monkeypatch.setenv("ALERT_EMAIL", "alerts@pestpod.ai")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@send.pestpod.ai")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_not_a_real_key")


def test_subject_last4_only_and_test_prefix():
    assert alert_email.subject("Sam", "0199", "T-1") == "PestPod: Y from Sam (***-0199)"
    assert "555" not in alert_email.subject("Sam", "0199", "T-1")
    assert alert_email.subject("Ryan", "0156", "TEST-001") == \
        "[TEST] PestPod: Y from Ryan (***-0156)"
    assert alert_email.subject("Alice", "0101", "EX-1") == \
        "[TEST] PestPod: Y from Alice (***-0101)"
    assert alert_email.is_test_account("test-001") and alert_email.is_test_account("ex-9")
    assert not alert_email.is_test_account("T-1")


def test_body_wants_a_time_vs_confirmed_appt():
    now = datetime(2026, 11, 14, 15, 37, tzinfo=TZ)
    wants = alert_email.body("Sam", "0199", "T-1", "Y", "", now)
    assert "Name: Sam" in wants and "Phone: ***-0199" in wants and "Account: T-1" in wants
    assert "Reply: Y" in wants and "Time: 2026-11-14 3:37 PM CT" in wants
    assert "Appointment: wants a time" in wants
    assert "5555550199" not in wants
    conf = alert_email.body("Sam", "0199", "T-1", "Y", "Thu 11/19 between 9-11 AM", now)
    assert "Appointment: Thu 11/19 between 9-11 AM (confirmed appt)" in conf
    assert "wants a time" not in conf


def test_send_when_enabled(monkeypatch):
    _enable(monkeypatch)
    calls = []
    now = datetime(2026, 11, 14, 15, 37, tzinfo=TZ)
    assert alert_email.send_y_alert("Sam", "0199", "T-1", "Y", "", now,
                                    poster=lambda p, k: calls.append((p, k))) is True
    payload, key = calls[0]
    assert key == "re_test_not_a_real_key"
    assert payload["to"] == ["alerts@pestpod.ai"]
    assert payload["from"] == "alerts@send.pestpod.ai"
    assert payload["subject"] == "PestPod: Y from Sam (***-0199)"
    assert "wants a time" in payload["text"]
    assert "5555550199" not in payload["text"]


@pytest.mark.parametrize("enabled", ["false", "0", "no", "", None])
def test_noop_when_disabled(monkeypatch, enabled):
    if enabled is not None:
        monkeypatch.setenv("ALERT_EMAIL_ENABLED", enabled)
    monkeypatch.setenv("ALERT_EMAIL", "alerts@pestpod.ai")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@send.pestpod.ai")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_not_a_real_key")
    calls = []
    assert alert_email.send_y_alert("Sam", "0199", poster=lambda *a: calls.append(a)) is False
    assert calls == []


@pytest.mark.parametrize("missing", ["ALERT_EMAIL", "ALERT_EMAIL_FROM", "RESEND_API_KEY"])
def test_noop_when_missing_config(monkeypatch, missing):
    _enable(monkeypatch)
    monkeypatch.delenv(missing)
    calls = []
    assert alert_email.send_y_alert("Sam", "0199", poster=lambda *a: calls.append(a)) is False
    assert calls == []


def test_error_is_swallowed_and_key_not_logged(monkeypatch, caplog):
    _enable(monkeypatch)

    def boom(payload, api_key):
        raise RuntimeError(f"401 unauthorized key={api_key}")

    with caplog.at_level(logging.WARNING, logger="pestpod.alert_email"):
        assert alert_email.send_y_alert("Sam", "0199", "T-1", poster=boom) is False
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Y alert email failed" in text
    assert "re_test_not_a_real_key" not in text
    assert "[redacted]" in text


def test_post_resend_sends_custom_user_agent(monkeypatch):
    """Cloudflare blocks the default Python-urllib UA; we must send our own."""
    import alert_email
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(alert_email.urllib.request, "urlopen", fake_urlopen)
    alert_email._post_resend({"to": "x"}, "re_test")
    assert seen["ua"] == alert_email.USER_AGENT
    assert "urllib" not in seen["ua"].lower()
