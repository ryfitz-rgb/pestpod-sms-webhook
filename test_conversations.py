"""Offline tests for the conversation view + reply box (conversations.py).

Twilio is mocked everywhere: the sender factory is replaced with a fake, and the one test of the
real make_sender() swaps twilio.rest.Client for a recorder. No network, no live texts, fictional
555 numbers, Postgres replaced by MemoryStore."""
import os
os.environ.setdefault("DASHBOARD_PASSWORD", "test-pass-123")
import html as htmlmod
import logging
import re
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import batch
import conversations as cv
import dashboard
import main

TZ = cv.TZ
PHONE = "+15555550142"
RYAN_FROM = "+15555550822"
HEADER = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "City", "OK to Text?", "Opt-In Status",
          "Proposed Appointment", "Reminder Status", "Last Reply", "Pest Notes", "Next Due"]


def cust(row=5, acct="A-100", name="Dana Test", phone="(555) 555-0142", ok=True, opt="Opted in",
         notes="", reply="Y", status="Confirmed Y"):
    return {"_row": row, "Account": acct, "Customer Name": name, "Phone (+1XXXXXXXXXX)": phone,
            "City": "Metairie", "OK to Text?": ok, "Opt-In Status": opt, "Proposed Appointment": "",
            "Reminder Status": status, "Last Reply": reply, "Pest Notes": notes, "Next Due": ""}


def at(h, mi=0, d=15):
    return datetime(2026, 11, d, h, mi, tzinfo=TZ)


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class LogSvc:
    """Read-only fake for the Inbound Log / Outbound Log tabs. Any write blows up the test."""
    def __init__(self, tabs=None): self.tabs = tabs or {}
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, spreadsheetId, range=None, **kw):
        name = range.strip("'")
        if name not in self.tabs:
            def boom(): raise RuntimeError("no such tab")
            return Exec(boom)
        return Exec(lambda: {"values": self.tabs[name]})
    def batchUpdate(self, *a, **k): raise AssertionError("sheet write")
    def update(self, *a, **k): raise AssertionError("sheet write")
    def append(self, *a, **k): raise AssertionError("sheet write")


class FakeSender:
    def __init__(self, fail=None):
        self.sent, self.fail, self.lock = [], fail, threading.Lock()
    def __call__(self, to, body):
        if self.fail:
            raise self.fail
        with self.lock:
            self.sent.append((to, body))
            return f"SM{len(self.sent):032d}"


@pytest.fixture
def env(monkeypatch):
    """Reply box on, memory store, fake sender, 10 AM CT, one customer, empty sheet logs."""
    store = cv.MemoryStore()
    cv.set_store(store)
    snd = FakeSender()
    cv.set_sender_factory(lambda: snd)
    monkeypatch.setenv("REPLY_BOX_ENABLED", "true")
    monkeypatch.setattr(cv, "_clock", lambda: at(10))
    svc = LogSvc()
    monkeypatch.setattr(main, "get_sheets", lambda: svc)
    rows = [cust()]
    monkeypatch.setattr(dashboard, "fetch_rows", lambda force=False: {"header": HEADER, "rows": rows, "undo": []})
    yield {"store": store, "sender": snd, "rows": rows, "svc": svc}
    cv.set_store(None)
    cv.set_sender_factory(None)


def client():
    c = TestClient(main.app)
    r = c.get("/dashboard/login")
    tok = re.search(r'name=csrf value="([^"]+)"', r.text).group(1)
    assert c.post("/dashboard/login", data={"username": "ryan", "password": "test-pass-123", "csrf": tok},
                  follow_redirects=False).status_code == 303
    return c


def page(c, row=5, acct="A-100"):
    return c.get(f"/dashboard/conversation?row={row}&acct={quote(acct)}")


def form_bits(text):
    csrf = re.search(r'name=csrf value="([^"]+)"', text).group(1)
    idem = re.search(r'name=idem value="([^"]+)"', text).group(1)
    return csrf, idem


def post(c, csrf, idem, body="PestPod: How about Tue 9 AM?", **extra):
    data = {"row": 5, "acct": "A-100", "body": body, "idem": idem, "csrf": csrf, **extra}
    return c.post("/dashboard/conversation/send", data=data, follow_redirects=False)


# ---------------------------------------------------------------- segment math
@pytest.mark.parametrize("text,enc,segs", [
    ("", "GSM-7", 0), ("a", "GSM-7", 1), ("a" * 160, "GSM-7", 1), ("a" * 161, "GSM-7", 2),
    ("a" * 306, "GSM-7", 2), ("a" * 307, "GSM-7", 3),
    ("[" * 80, "GSM-7", 1), ("[" * 81, "GSM-7", 2), ("€" * 80, "GSM-7", 1),
    ("é" * 160, "GSM-7", 1),
    ("😀", "UCS-2", 1), ("ñ" * 70 + "’", "UCS-2", 2), ("’" * 70, "UCS-2", 1), ("’" * 71, "UCS-2", 2),
    ("’" * 134, "UCS-2", 2), ("’" * 135, "UCS-2", 3), ("😀" * 35, "UCS-2", 1), ("😀" * 36, "UCS-2", 2),
])
def test_sms_segments(text, enc, segs):
    r = cv.sms_segments(text)
    assert (r["encoding"], r["segments"]) == (enc, segs)


def test_segment_units_and_chars():
    assert cv.sms_segments("a[b")["units"] == 4 and cv.sms_segments("a[b")["chars"] == 3
    assert cv.sms_segments("😀")["units"] == 2
    assert cv.sms_segments(batch.TEMPLATE) == {"chars": len(batch.TEMPLATE), "units": len(batch.TEMPLATE),
                                               "encoding": "GSM-7", "segments": 1}


def test_mask_shows_last4_only():
    assert cv.mask("+15555550142") == "***-0142" and cv.mask("") == "-" and cv.mask("12") == "-"


# ---------------------------------------------------------------- flag off
def test_enabled_parsing(monkeypatch):
    for v, want in (("", False), ("false", False), ("0", False), ("true", True), ("1", True), ("YES", True)):
        monkeypatch.setenv("REPLY_BOX_ENABLED", v)
        assert cv.enabled() is want
    monkeypatch.delenv("REPLY_BOX_ENABLED")
    assert cv.enabled() is False


def test_flag_off_routes_404_and_nothing_sends(env, monkeypatch):
    c = client()
    csrf, idem = form_bits(page(c).text)
    monkeypatch.setenv("REPLY_BOX_ENABLED", "false")
    assert page(c).status_code == 404
    assert post(c, csrf, idem).status_code == 404
    assert env["sender"].sent == [] and env["store"].rows == []


def test_flag_off_no_link_in_wants_time(monkeypatch):
    monkeypatch.delenv("REPLY_BOX_ENABLED", raising=False)
    entries = [{"row": 5, "acct": "A-100", "name": "Dana", "city": "", "phone_last4": "0142", "next_due": None,
                "note": ""}]
    assert "/dashboard/conversation" not in dashboard.render_wants_time(entries)
    monkeypatch.setenv("REPLY_BOX_ENABLED", "1")
    h = dashboard.render_wants_time(entries)
    assert 'href="/dashboard/conversation?row=5&amp;acct=A-100"' in h and "Open texts" in h


def test_wants_link_encodes_account(monkeypatch):
    monkeypatch.setenv("REPLY_BOX_ENABLED", "1")
    h = dashboard.render_wants_time([{"row": 7, "acct": "A&B 1", "name": "x", "city": "", "phone_last4": "",
                                      "next_due": None, "note": ""}])
    assert "row=7&amp;acct=A%26B+1" in h


def test_flag_off_by_default_in_repo():
    assert "REPLY_BOX_ENABLED" not in os.environ or not cv.enabled()


# ---------------------------------------------------------------- auth
def test_view_requires_login(env):
    c = TestClient(main.app)
    r = c.get("/dashboard/conversation?row=5&acct=A-100", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"


def test_send_requires_login_and_csrf(env):
    anon = TestClient(main.app)
    r = anon.post("/dashboard/conversation/send", data={"row": 5, "acct": "A-100", "body": "PestPod: hi",
                                                        "idem": "k" * 24, "csrf": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
    c = client()
    _, idem = form_bits(page(c).text)
    assert post(c, "bad-csrf", idem).status_code == 403
    assert post(c, "", idem).status_code == 403
    assert env["sender"].sent == []


def test_send_rejects_cross_origin(env):
    c = client()
    csrf, idem = form_bits(page(c).text)
    r = c.post("/dashboard/conversation/send", data={"row": 5, "acct": "A-100", "body": "PestPod: hi",
                                                     "idem": idem, "csrf": csrf},
               headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and env["sender"].sent == []


def test_view_wrong_account_is_404(env):
    c = client()
    assert page(c, acct="OTHER").status_code == 404
    assert page(c, row=99).status_code == 404


# ---------------------------------------------------------------- conversation view
def _seed(store):
    cv.record(PHONE, "out", "batch", batch.TEMPLATE, sid="SMb1", status="sent", when=at(10, 30, d=3))
    cv.record(PHONE, "in", "inbound", "Y", sid="SMi1", status="received", when=at(15, 2, d=3))
    cv.record(PHONE, "out", "auto_reply", main.CONFIRM_REPLY, status="twiml", when=at(15, 2, d=3))
    cv.record("+15555550999", "in", "inbound", "someone else", sid="SMx", when=at(9, d=4))


def test_view_shows_thread_in_order_with_ct_times_and_masked_phone(env):
    _seed(env["store"])
    t = page(client()).text
    body = htmlmod.unescape(t)
    i1, i2, i3 = body.index("You're due for pest service"), body.index(">Y<"), body.index("Got it!")
    assert i1 < i2 < i3
    assert "Nov 3, 10:30 AM CT" in body and "Nov 3, 3:02 PM CT" in body
    assert "someone else" not in body
    assert "Phone ending 0142" in body
    for full in ("5555550142", "555-0142", "(555) 555-0142", "555.555.0142"):
        assert full not in body
    assert "reminder" in body and "auto-reply" in body
    assert "id=theme-toggle" in t and "html[data-theme=dark]" in t  # same dark-mode styling
    assert "Reply from Ryan's number (ending 0822)" in body and 'placeholder="PestPod: "' in t
    assert 'usually start with "PestPod:"' in body


def test_view_merges_sheet_logs_and_dedupes_by_sid(env):
    env["svc"].tabs = {
        "Inbound Log": [main.INBOUND_HEADERS, ["2026-11-01T09:00:00-05:00", PHONE, "old hello", "SMold", "5"],
                        ["2026-11-03T15:02:00-06:00", PHONE, "Y", "SMi1", "5"],
                        ["2026-11-02T09:00:00-06:00", "+15555550999", "not them", "SMz", ""]],
        "Outbound Log": [batch.OUTBOUND_HEADERS, ["2026-11-03T10:30:00-06:00", PHONE, "Dana", "1",
                                                  batch.TEMPLATE, "SMb1", "sent", 5]],
    }
    _seed(env["store"])
    msgs, err = cv.thread(PHONE)
    assert err == ""
    bodies = [m["body"] for m in msgs]
    assert bodies[0] == "old hello"  # from before the messages table existed
    assert bodies.count("Y") == 1 and bodies.count(batch.TEMPLATE) == 1
    assert "not them" not in bodies
    assert all(m["created_at"].tzinfo is not None for m in msgs)
    assert [m["created_at"] for m in msgs] == sorted(m["created_at"] for m in msgs)
    # 9:00 -05:00 on Nov 1 is 8:00 AM CST (DST ended at 2 AM that day): converted, not parroted
    assert msgs[0]["created_at"].strftime("%-I:%M %p") == "8:00 AM" and msgs[0]["created_at"].utcoffset().total_seconds() == -6 * 3600


def test_thread_survives_store_and_sheet_failures(env, monkeypatch):
    def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(env["store"], "for_phone", boom)
    monkeypatch.setattr(main, "get_sheets", boom)
    msgs, err = cv.thread(PHONE)
    assert msgs == [] and "couldn't be loaded" in err
    assert page(client()).status_code == 200


def test_view_after_hours_shows_warning_and_confirm_box(env, monkeypatch):
    monkeypatch.setattr(cv, "_clock", lambda: at(21, 15))
    t = htmlmod.unescape(page(client()).text)
    assert "outside the normal texting hours" in t and "9:15 PM CT" in t and "name=confirm_quiet" in t
    monkeypatch.setattr(cv, "_clock", lambda: at(10))
    assert "name=confirm_quiet" not in page(client()).text


def test_view_has_double_submit_guard_and_counter(env):
    t = page(client()).text
    assert "b.disabled=true" in t and "f.dataset.sent" in t
    assert "id=ctr" in t and "153" in t and "67" in t
    assert "0 characters · 0 texts (GSM-7)" in t


# ---------------------------------------------------------------- sending
def test_send_once_from_ryan_and_logged(env):
    c = client()
    csrf, idem = form_bits(page(c).text)
    r = post(c, csrf, idem)
    assert r.status_code == 303 and "msg=sent" in r.headers["location"]
    assert env["sender"].sent == [(PHONE, "PestPod: How about Tue 9 AM?")]
    [m] = env["store"].rows
    assert (m["direction"], m["kind"], m["status"], m["tech"], m["account"], m["customer_row"]) == \
        ("out", "manual", "sent", "ryan", "A-100", 5)
    assert m["twilio_sid"].startswith("SM") and m["idempotency_key"] == idem
    t = htmlmod.unescape(c.get(r.headers["location"]).text)
    assert "Sent." in t and "sent by Ryan" in t and "How about Tue 9 AM?" in t


def test_same_idempotency_key_sends_only_once(env):
    c = client()
    csrf, idem = form_bits(page(c).text)
    assert "msg=sent" in post(c, csrf, idem).headers["location"]
    r = post(c, csrf, idem)
    assert "msg=duplicate" in r.headers["location"]
    assert len(env["sender"].sent) == 1 and len(env["store"].rows) == 1
    assert "Already sent" in c.get(r.headers["location"]).text


def test_concurrent_double_click_sends_once(env):
    gate = threading.Event()
    snd = env["sender"]
    def slow(to, body):
        gate.wait(2)
        return snd(to, body)
    cv.set_sender_factory(lambda: slow)
    out = []
    def go():
        out.append(cv.send_reply(cust(), PHONE, "PestPod: hi", "same-key-1234567890", False))
    ts = [threading.Thread(target=go) for _ in range(5)]
    for t in ts: t.start()
    gate.set()
    for t in ts: t.join()
    assert sorted(o[0] for o in out).count("sent") == 1
    assert len(snd.sent) == 1


def test_each_page_load_gets_a_fresh_key(env):
    c = client()
    assert form_bits(page(c).text)[1] != form_bits(page(c).text)[1]


@pytest.mark.parametrize("body,outcome", [("", "empty"), ("   ", "empty"), ("x" * (cv.MAX_BODY + 1), "too_long")])
def test_empty_and_too_long_not_sent(env, body, outcome):
    assert cv.send_reply(cust(), PHONE, body, "k" * 24, False)[0] == outcome
    assert env["sender"].sent == []


def test_endpoint_empty_body_400(env):
    c = client()
    csrf, idem = form_bits(page(c).text)
    assert post(c, csrf, idem, body="  ").status_code == 400 and env["sender"].sent == []


@pytest.mark.parametrize("key", ["", "short", "bad key with spaces!!", "x" * 81])
def test_bad_idempotency_key_refused(env, key):
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", key, False)[0] == "bad_key"
    assert env["sender"].sent == []


def test_no_database_refuses_to_send(env):
    cv.set_store(None)
    os.environ.pop("DATABASE_URL", None)
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "no_db"
    assert env["sender"].sent == []


def test_no_phone_blocked(env):
    assert cv.send_reply(cust(phone=""), "", "PestPod: hi", "k" * 24, False)[0] == "blocked"


# ---------------------------------------------------------------- opt-out / consent
def test_stop_blocks_server_side_and_hides_box(env):
    cv.record(PHONE, "in", "inbound", "STOP", sid="SMs", when=at(9, d=10))
    out, why = cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)
    assert out == "blocked" and "STOP" in why and env["sender"].sent == []
    c = client()
    t = page(c).text
    assert "Opted out (STOP)" in t and "id=reply" not in t and "name=idem" not in t
    # a hand-built POST is still refused
    csrf = re.search(r'name=csrf value="([^"]+)"', c.get("/dashboard/login", follow_redirects=True).text).group(1)
    r = post(c, csrf, "z" * 24)
    assert "msg=blocked" in r.headers["location"] and env["sender"].sent == []


@pytest.mark.parametrize("word", ["stop", "Stop.", "UNSUBSCRIBE", "cancel", "END", "quit", "STOPALL"])
def test_all_stop_words_block(env, word):
    cv.record(PHONE, "in", "inbound", word, sid="SMs", when=at(9))
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "blocked"


def test_start_after_stop_unblocks(env):
    cv.record(PHONE, "in", "inbound", "STOP", sid="SM1", when=at(9, d=10))
    cv.record(PHONE, "in", "inbound", "START", sid="SM2", when=at(9, d=11))
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "sent"


def test_stop_in_sheet_inbound_log_blocks(env):
    env["svc"].tabs = {"Inbound Log": [main.INBOUND_HEADERS, ["2026-11-10T09:00:00-06:00", PHONE, "Stop", "SMq", "5"]]}
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "blocked"


@pytest.mark.parametrize("opt", ["Opted out", "opted-out", "STOP"])
def test_sheet_opt_out_status_blocks(env, opt):
    assert cv.send_reply(cust(opt=opt, ok=True), PHONE, "PestPod: hi", "k" * 24, False)[0] == "blocked"


def test_twilio_21610_history_blocks(env):
    cv.record(PHONE, "out", "batch", "x", status="error: Twilio 21610", when=at(9))
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "blocked"


def test_no_consent_blocked(env):
    out, why = cv.send_reply(cust(ok=False, opt=""), PHONE, "PestPod: hi", "k" * 24, False)
    assert out == "blocked" and "consent" in why and env["sender"].sent == []


def test_consent_via_ok_to_text_string(env):
    assert cv.send_reply(cust(ok="TRUE", opt=""), PHONE, "PestPod: hi", "k" * 24, False)[0] == "sent"


def test_anytime_stop_blocked(env):
    env["rows"][0] = cust(notes="Anytime stop, gate code on file")
    c = client()
    t = page(c).text
    assert "Anytime stop" in t and "id=reply" not in t
    assert cv.send_reply(env["rows"][0], PHONE, "PestPod: hi", "k" * 24, False, anytime=True)[0] == "blocked"


# ---------------------------------------------------------------- quiet hours
@pytest.mark.parametrize("h,mi,inside", [(8, 59, False), (9, 0, True), (19, 59, True), (20, 0, False), (23, 0, False)])
def test_quiet_window(h, mi, inside):
    assert cv.in_quiet_window(at(h, mi)) is inside


def test_quiet_window_uses_central_time():
    assert cv.in_quiet_window(datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc)) is True   # 9 AM CT
    assert cv.in_quiet_window(datetime(2026, 11, 15, 14, 59, tzinfo=timezone.utc)) is False


def test_after_hours_needs_confirm_server_side(env):
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False, now=at(21))[0] == "quiet"
    assert env["sender"].sent == []
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, True, now=at(21))[0] == "sent"
    assert len(env["sender"].sent) == 1


def test_after_hours_endpoint_409_then_confirm(env, monkeypatch):
    monkeypatch.setattr(cv, "_clock", lambda: at(7, 30))
    c = client()
    csrf, idem = form_bits(page(c).text)
    r = post(c, csrf, idem, body="PestPod: running late")
    assert r.status_code == 409 and env["sender"].sent == []
    assert "Yes, send it anyway" in r.text and "PestPod: running late" in r.text
    csrf2, idem2 = form_bits(r.text)
    r2 = post(c, csrf2, idem2, body="PestPod: running late", confirm_quiet="1")
    assert "msg=sent" in r2.headers["location"] and len(env["sender"].sent) == 1


# ---------------------------------------------------------------- Twilio outcomes
def test_twilio_rejection_recorded_not_retried(env):
    cv.set_sender_factory(lambda: FakeSender(fail=cv.SendError("Twilio 21211")))
    out, status = cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)
    assert out == "error" and status == "error: Twilio 21211"
    assert env["store"].rows[0]["status"] == "error: Twilio 21211"
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "duplicate"


def test_network_error_is_unknown(env):
    cv.set_sender_factory(lambda: FakeSender(fail=TimeoutError()))
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "unknown"
    assert env["store"].rows[0]["status"] == "unknown: TimeoutError"


def test_config_error_sends_nothing(env):
    def bad(): raise cv.ConfigError("REPLY_FROM_NUMBER is not set")
    cv.set_sender_factory(bad)
    assert cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)[0] == "config"
    assert env["store"].rows == []


# ---------------------------------------------------------------- Ryan's number only
@pytest.mark.parametrize("num", ["", "+15555552533", "+15555550101", "12"])
def test_from_number_must_be_ryans_0822(monkeypatch, num):
    monkeypatch.setenv("REPLY_FROM_NUMBER", num)
    with pytest.raises(cv.ConfigError):
        cv.reply_from_number()


def test_from_number_ok_and_not_another_techs(monkeypatch):
    monkeypatch.setenv("REPLY_FROM_NUMBER", "(555) 555-0822")
    monkeypatch.delenv("TECH_NUMBERS", raising=False)
    assert cv.reply_from_number() == RYAN_FROM
    monkeypatch.setenv("TECH_NUMBERS", "blain=+15555550822")
    with pytest.raises(cv.ConfigError):
        cv.reply_from_number()
    monkeypatch.setenv("TECH_NUMBERS", "blain=+15555552533")
    assert cv.reply_from_number() == RYAN_FROM


def test_messaging_service_pinned_to_ryans(monkeypatch):
    assert cv.RYAN_MESSAGING_SERVICE_SID == "MG7aa08d66618db7ad78ad6af28b793bf2"
    assert "2533" in cv.BLOCKED_SENDER_LAST4 and cv.RYAN_LAST4 == "0822"


def test_make_sender_uses_ryan_service_and_from_never_real_twilio(monkeypatch):
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return type("M", (), {"sid": "SMfake"})()

    class FakeClient:
        def __init__(self, *a): self.messages = FakeMessages()

    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)
    monkeypatch.setenv("REPLY_FROM_NUMBER", RYAN_FROM)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_MESSAGING_SERVICE_SID", "MG" + "f" * 32)  # must be ignored
    monkeypatch.delenv("TECH_NUMBERS", raising=False)
    send = cv.make_sender()
    assert send(PHONE, "PestPod: hi") == "SMfake"
    assert calls == [{"messaging_service_sid": cv.RYAN_MESSAGING_SERVICE_SID, "from_": RYAN_FROM,
                      "to": PHONE, "body": "PestPod: hi"}]


def test_make_sender_rejection_maps_to_senderror(monkeypatch):
    from twilio.base.exceptions import TwilioRestException

    class FakeClient:
        def __init__(self, *a):
            class M:
                def create(self, **kw): raise TwilioRestException(400, "u", msg="x", code=21610)
            self.messages = M()
    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", FakeClient)
    monkeypatch.setenv("REPLY_FROM_NUMBER", RYAN_FROM)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    with pytest.raises(cv.SendError, match="21610"):
        cv.make_sender()(PHONE, "PestPod: hi")


def test_make_sender_needs_credentials(monkeypatch):
    monkeypatch.setenv("REPLY_FROM_NUMBER", RYAN_FROM)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(cv.ConfigError):
        cv.make_sender()


def test_sender_factory_bad_number_blocks_endpoint(env, monkeypatch):
    cv.set_sender_factory(None)  # the real factory, which checks the number before any client exists
    monkeypatch.setenv("REPLY_FROM_NUMBER", "+15555552533")
    import twilio.rest
    monkeypatch.setattr(twilio.rest, "Client", lambda *a: (_ for _ in ()).throw(AssertionError("no client")))
    c = client()
    csrf, idem = form_bits(page(c).text)
    r = post(c, csrf, idem)
    assert "msg=config" in r.headers["location"] and env["store"].rows == []


# ---------------------------------------------------------------- logging from existing flows
def _run_tasks(bt):
    import asyncio
    asyncio.run(bt())


def test_webhook_logs_inbound_and_auto_reply(env, monkeypatch):
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: {"row": 5, "name": "Dana", "account": "A-100",
                                                                   "proposed_appointment": "", "opted_in": True,
                                                                   "fields": {}})
    for fn in ("update_customer_reply", "log_inbound"):
        monkeypatch.setattr(main, fn, lambda *a, **k: None)
    monkeypatch.setattr(main.alert_email, "send_y_alert", lambda *a, **k: None)
    bt = BackgroundTasks()
    r = main.sms_webhook(background_tasks=bt, From="(555) 555-0142", To="", Body="Y", MessageSid="SMin1")
    assert "Got it!" in r.body.decode()
    assert env["store"].rows == []  # nothing before the reply goes out
    _run_tasks(bt)
    kinds = [(m["direction"], m["kind"], m["body"]) for m in env["store"].rows]
    assert kinds == [("in", "inbound", "Y"), ("out", "auto_reply", main.CONFIRM_REPLY)]
    assert env["store"].rows[0]["twilio_sid"] == "SMin1" and env["store"].rows[0]["account"] == "A-100"
    assert all(m["phone"] == PHONE and m["customer_row"] == 5 for m in env["store"].rows)


def test_webhook_retry_same_sid_logged_once(env, monkeypatch):
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: None)
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    for _ in range(2):
        bt = BackgroundTasks()
        main.sms_webhook(background_tasks=bt, From=PHONE, To="", Body="hello", MessageSid="SMdup")
        _run_tasks(bt)
    assert [m["kind"] for m in env["store"].rows].count("inbound") == 1


def test_webhook_stop_logged_no_auto_reply(env, monkeypatch):
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: None)
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    bt = BackgroundTasks()
    main.sms_webhook(background_tasks=bt, From=PHONE, To="", Body="STOP", MessageSid="SMstop")
    _run_tasks(bt)
    assert [(m["kind"], m["body"]) for m in env["store"].rows] == [("inbound", "STOP")]
    assert cv.opted_out(cust(), cv.thread(PHONE)[0])


def test_webhook_reply_survives_broken_message_log(env, monkeypatch, caplog):
    class Broken(cv.MemoryStore):
        def add(self, rec): raise RuntimeError("db down postgres://secret@host")
    cv.set_store(Broken())
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: None)
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    bt = BackgroundTasks()
    with caplog.at_level(logging.WARNING):
        r = main.sms_webhook(background_tasks=bt, From=PHONE, To="", Body="hello", MessageSid="SMx")
        _run_tasks(bt)
    assert "No matching appointment" in r.body.decode()
    assert "***-0142" in caplog.text and "5555550142" not in caplog.text and "secret" not in caplog.text


def test_webhook_logs_tech_for_mapped_number(env, monkeypatch):
    monkeypatch.setenv("TECH_NUMBERS", "blain=+15555552533")
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: None)
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    bt = BackgroundTasks()
    main.sms_webhook(background_tasks=bt, From=PHONE, To="+15555552533", Body="hi", MessageSid="SMt")
    _run_tasks(bt)
    assert env["store"].rows[0]["tech"] == "blain"
    t = page(client()).text
    assert "to blain&#x27;s number" in t or "to blain's number" in t


def test_webhook_end_to_end_via_http(env, monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: None)
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    r = TestClient(main.app).post("/api/v1/sms/webhook", data={"From": PHONE, "To": "", "Body": "hey", "MessageSid": "SMh"})
    assert r.status_code == 200
    assert [m["kind"] for m in env["store"].rows] == ["inbound", "auto_reply"]


def test_batch_send_logged(env):
    import test_batch as tb
    svc = tb.FakeSvc([tb.row("A", "Al", "+15555550101")])
    s, snd, _ = tb.run(svc, tb.at(2026, 11, 3))
    assert s["sent"] == 1
    [m] = env["store"].rows
    assert (m["direction"], m["kind"], m["body"], m["status"], m["account"], m["customer_row"]) == \
        ("out", "batch", tb.NUDGE, "sent", "A", 2)
    assert m["twilio_sid"].startswith("SM") and m["phone"] == "+15555550101"


def test_batch_unaffected_by_broken_message_log(env):
    import test_batch as tb
    class Broken(cv.MemoryStore):
        def add(self, rec): raise RuntimeError("down")
    cv.set_store(Broken())
    svc = tb.FakeSvc([tb.row("A", "Al", "+15555550101"), tb.row("B", "Bo", "+15555550104")])
    s, snd, _ = tb.run(svc, tb.at(2026, 11, 3))
    assert s["sent"] == 2 and len(svc.appends) == 2


def test_batch_dry_run_logs_nothing(env):
    import test_batch as tb
    svc = tb.FakeSvc([tb.row("A", "Al", "+15555550101")])
    tb.run(svc, tb.at(2026, 11, 3), dry_run=True)
    assert env["store"].rows == []


def test_record_without_database_is_noop(monkeypatch):
    cv.set_store(None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert cv.record(PHONE, "in", "inbound", "hi") is None


def test_record_rejects_bad_direction_quietly(env):
    assert cv.record(PHONE, "sideways", "inbound", "hi") is None and env["store"].rows == []


def test_record_twiml_parses_messages(env):
    cv.record_twiml(PHONE, '<?xml version="1.0" encoding="UTF-8"?><Response><Message>PestPod: a &amp; b</Message></Response>')
    cv.record_twiml(PHONE, "<Response/>")
    cv.record_twiml(PHONE, "not xml")
    assert [m["body"] for m in env["store"].rows] == ["PestPod: a & b"]


def test_send_log_lines_mask_phone(env, caplog):
    with caplog.at_level(logging.INFO, logger="pestpod.conversations"):
        cv.send_reply(cust(), PHONE, "PestPod: hi", "k" * 24, False)
    assert "***-0142" in caplog.text and "5555550142" not in caplog.text


# ---------------------------------------------------------------- schema + guards
def test_schema_has_messages_table():
    import pgsync
    s = pgsync.SCHEMA
    assert "CREATE TABLE IF NOT EXISTS messages" in s
    for col in ("phone", "account", "customer_row", "direction", "kind", "body", "created_at", "twilio_sid",
                "status", "idempotency_key TEXT UNIQUE"):
        assert col in s
    assert "messages_sid_dir_idx" in s


def test_pg_store_sql_shapes(monkeypatch):
    """PgStore against a recording fake connection (no database)."""
    seen = []

    class Cur:
        description = [("id",), ("created_at",), ("phone",), ("account",), ("customer_row",), ("direction",),
                       ("kind",), ("body",), ("twilio_sid",), ("status",), ("tech",)]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): seen.append((sql, params))
        def fetchone(self): return (7,)
        def fetchall(self): return [(2, at(10), PHONE, None, None, "in", "inbound", "b", None, None, None),
                                    (1, at(9), PHONE, None, None, "in", "inbound", "a", None, None, None)]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass

    import pgsync
    monkeypatch.setattr(pgsync, "connect", lambda: Conn())
    monkeypatch.setattr(cv.PgStore, "_ready", True)
    st = cv.PgStore()
    assert st.add({"phone": PHONE, "direction": "in", "kind": "inbound", "body": "x"}) == 7
    assert "ON CONFLICT DO NOTHING RETURNING id" in seen[-1][0]
    st.finish(7, "SMx", "sent")
    assert seen[-1][1] == ("SMx", "sent", 7)
    rows = st.for_phone(PHONE)
    assert [r["body"] for r in rows] == ["a", "b"]  # newest-first query reversed to oldest-first


def test_conversations_never_writes_the_sheet():
    import inspect
    src = inspect.getsource(cv)
    for w in ("batchUpdate", "values().update", "values().append", "values().clear", ".append(spreadsheetId"):
        assert w not in src


def test_no_column_i_or_k_writes_and_says_ryan():
    import inspect
    src = inspect.getsource(cv)
    assert "Clyde" not in src and "Ryan" in src
    assert "Blain" in src  # only in the 'never send from Blain's number' guard comments
