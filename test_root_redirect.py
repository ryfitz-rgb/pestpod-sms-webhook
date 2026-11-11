import os
os.environ.setdefault("SHEET_ID", "test-sheet")
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)


def test_root_redirects_to_dashboard():
    for host in ("app.pestpod.ai", "pestpod-sms-webhook.onrender.com"):
        r = client.get("/", headers={"host": host}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/dashboard"  # relative: stays on the same host


def test_root_head_redirects():
    r = client.head("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"


def test_other_routes_unchanged():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/version").status_code == 200
    # Twilio webhook still requires a signature (POST / is not redirected anywhere either)
    assert client.post("/api/v1/sms/webhook", data={"From": "+15555550100", "Body": "Y"}).status_code in (403, 503)
    assert client.post("/", data={}).status_code == 405
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
