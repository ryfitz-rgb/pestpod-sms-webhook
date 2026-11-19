"""Offline tests for POST /api/v1/batch. No network, fictional numbers, phones last-4 only."""
import os

import pytest
from fastapi.testclient import TestClient

import batch
import batch_api
import main
from test_batch import FakeSvc, FakeSender, row, at

TOKEN = "batch-test-token-xxxxxxxxxxxxxxxx"
PHONE = "+15555550199"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BATCH_RUN_TOKEN", TOKEN)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY_SID", raising=False)
    monkeypatch.delenv("TWILIO_API_KEY_SECRET", raising=False)
    svc = FakeSvc([row("T-1", "Sam", PHONE, due="2026-11-10")])
    monkeypatch.setattr(main, "get_sheets", lambda: svc)
    return TestClient(main.app), svc


def post(client, body=None, token=TOKEN, header="authorization"):
    h = {}
    if token is not None and header == "authorization":
        h["Authorization"] = f"Bearer {token}"
    elif token is not None and header == "x":
        h["X-PestPod-Batch-Token"] = token
    return client.post("/api/v1/batch", json=body if body is not None else {}, headers=h)


def test_disabled_when_token_unset(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("BATCH_RUN_TOKEN", raising=False)
    r = post(c, {"dry_run": True})
    assert r.status_code == 503
    assert TOKEN not in r.text


def test_401_missing_and_wrong_token(client):
    c, _ = client
    assert post(c, {"dry_run": True}, token=None).status_code == 401
    r = post(c, {"dry_run": True}, token="wrong-token-xxxxxxxxxxxxxxxxx")
    assert r.status_code == 401
    assert TOKEN not in r.text and "wrong-token" not in r.text


def test_dry_run_plans_and_writes_nothing(client):
    c, svc = client
    r = post(c, {"dry_run": True, "now": "2026-11-01T10:00"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["mode"] == "DRY RUN"
    assert j["planned"] == 1 and j["sent"] == 0 and j["errors"] == 0
    log = "\n".join(j["log"])
    assert "0199" in log and PHONE not in log and "5555550199" not in log
    assert "WOULD SEND" in log and "nothing sent, nothing written" in log.lower()
    assert svc.mutations == 0 and svc.appends == []


def test_default_body_is_dry_run(client, monkeypatch):
    c, svc = client

    def boom(*_a, **_k):
        raise AssertionError("Twilio sender must not be called on a dry run")

    monkeypatch.setattr(batch, "make_twilio_sender", boom)
    r = post(c, {})
    assert r.status_code == 200
    assert r.json()["mode"] == "DRY RUN"
    assert svc.mutations == 0


def test_alt_header_and_masked_only_phone(client):
    c, svc = client
    r = post(c, {"dry_run": True, "now": "2026-11-01T10:00", "only_phone": PHONE}, header="x")
    assert r.status_code == 200
    assert r.json()["planned"] == 1
    assert PHONE not in r.text
    assert svc.mutations == 0


def test_now_requires_dry_run(client):
    c, _ = client
    r = post(c, {"dry_run": False, "now": "2026-11-01T10:00"})
    assert r.status_code == 400
    assert "dry_run" in r.text


def test_bad_now_and_force_wave(client):
    c, _ = client
    assert post(c, {"dry_run": True, "now": "not-a-date"}).status_code == 400
    r = post(c, {"dry_run": True, "force_wave": 9})
    assert r.status_code == 422


def test_live_force_wave_without_only_phone_is_400(client):
    c, _ = client
    r = post(c, {"dry_run": False, "force_wave": 1})
    assert r.status_code == 400
    assert "only-phone" in r.text.lower() or "only_phone" in r.text.lower()


def test_live_send_uses_store_and_masks_phone(client, monkeypatch):
    c, svc = client
    sender = FakeSender()
    monkeypatch.setattr(batch, "make_twilio_sender", lambda: sender)
    # Pin the clock: a live run aborts outside 9 AM-8 PM Chicago.
    noon = at(2026, 11, 15, 10, 0)
    real_run = batch.run
    monkeypatch.setattr(
        batch, "run",
        lambda **kw: real_run(**{**kw, "now": kw.get("now") or noon, "clock": lambda: noon}),
    )
    r = post(c, {"dry_run": False, "force_wave": 1, "only_phone": PHONE})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["mode"] == "LIVE" and j["sent"] == 1
    assert len(sender.sent) == 1 and sender.sent[0][0] == PHONE
    assert sender.sent[0][1].startswith("PestPod:")
    assert PHONE not in r.text and "0199" in "\n".join(j["log"])
    assert svc.appends and svc.mutations > 0


def test_version_reports_batch_run_without_token(client, monkeypatch):
    c, _ = client
    j = c.get("/version").json()
    assert j["batch_run"] is True
    assert TOKEN not in str(j)
    monkeypatch.delenv("BATCH_RUN_TOKEN", raising=False)
    assert c.get("/version").json()["batch_run"] is False


def test_busy_returns_409(client, monkeypatch):
    c, _ = client
    started = []

    def blocked(**kwargs):
        started.append(1)
        # Re-enter the endpoint while the first call holds the lock.
        inner = post(c, {"dry_run": True, "now": "2026-11-01T10:00"})
        assert inner.status_code == 409
        return {"mode": "DRY RUN", "wave": 1, "planned": 0, "sent": 0, "errors": 0, "abort": None}

    monkeypatch.setattr(batch, "run", blocked)
    r = post(c, {"dry_run": True})
    assert r.status_code == 200 and started == [1]
