"""Offline tests for free-text inbound handling, message alert emails and unread badges.

Everything is mocked: Postgres is a MemoryStore (or a recording fake connection), the alert email
poster is a recorder, Twilio is never called (the webhook only returns TwiML). Fictional 555 numbers.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks

import alert_email
import conversations as cv
import dashboard
import main
import pgsync
from test_conversations import (PHONE, _run_tasks, at, client, cust, env, page)  # noqa: F401

ACK = main.FREE_TEXT_ACK


def customer(opted_in=True, opt="Opted in", notes="", row=5, account="A-100", name="Dana"):
    return {"row": row, "name": name, "account": account, "proposed_appointment": "", "opted_in": opted_in,
            "fields": {"Opt-In Status": opt, "Pest Notes": notes, "Termite Notes": ""}}


@pytest.fixture
def hook(env, monkeypatch):
    """Webhook with a known customer, Sheets writes stubbed, alert emails recorded."""
    state = {"cust": customer(), "alerts": [], "y_alerts": [], "updates": []}
    monkeypatch.setattr(main, "find_customer_by_phone", lambda p: state["cust"])
    monkeypatch.setattr(main, "log_inbound", lambda *a, **k: None)
    monkeypatch.setattr(main, "mark_customer_opted_in", lambda *a, **k: None)
    monkeypatch.setattr(main, "log_optin", lambda *a, **k: None)
    monkeypatch.setattr(main, "update_customer_reply", lambda row, r: state["updates"].append((row, r)))
    monkeypatch.setattr(main.alert_email, "send_message_alert", lambda *a, **k: state["alerts"].append(a))
    monkeypatch.setattr(main.alert_email, "send_y_alert", lambda *a, **k: state["y_alerts"].append(a))
    state.update(env)
    return state


def text_in(body, frm="(555) 555-0142", sid="SMin", to=""):
    bt = BackgroundTasks()
    r = main.sms_webhook(background_tasks=bt, From=frm, To=to, Body=body, MessageSid=sid)
    twiml = r.body.decode()
    _run_tasks(bt)
    return twiml


def kinds(store):
    return [(m["direction"], m["kind"], m["body"]) for m in store.rows]


# ---------------------------------------------------------------- the acknowledgement text
def test_ack_text_rules():
    assert ACK.startswith("PestPod:") and "STOP" in ACK and "?" not in ACK  # tells, never asks
    assert "Ryan" in ACK and "Clyde" not in ACK
    seg = cv.sms_segments(ACK)
    assert seg["encoding"] == "GSM-7" and seg["segments"] == 1


# ---------------------------------------------------------------- webhook: free text
def test_free_text_gets_ack_not_menu(hook):
    t = text_in("When you coming out?", sid="SMq1")
    assert ACK in t and "Reply Y to confirm" not in t and t.count("<Message>") == 1
    assert kinds(hook["store"]) == [("out", "auto_reply", ACK), ("in", "inbound", "When you coming out?")] or \
        kinds(hook["store"]) == [("in", "inbound", "When you coming out?"), ("out", "auto_reply", ACK)]
    msgs, _ = cv.thread(PHONE, include_sheet=False)
    assert [m["kind"] for m in msgs] == ["inbound", "auto_reply"]  # question first, then the ack
    ack_row = [m for m in hook["store"].rows if m["kind"] == "auto_reply"][0]
    assert ack_row["status"] == "twiml" and ack_row["customer_row"] == 5 and ack_row["account"] == "A-100"
    assert ack_row["idempotency_key"].startswith("ack-") and "5555550142" not in ack_row["idempotency_key"]


def test_free_text_emails_ryan(hook):
    text_in("When you coming out?")
    [(name, last4, acct, body, row, when)] = hook["alerts"]
    assert (name, last4, acct, body, row) == ("Dana", "0142", "A-100", "When you coming out?", 5)
    assert when.tzinfo is not None
    assert hook["y_alerts"] == [] and hook["updates"] == []  # nothing marked in the sheet


def test_second_free_text_same_day_no_second_ack(hook):
    assert ACK in text_in("When you coming out?", sid="SMq1")
    t = text_in("Hello??", sid="SMq2")
    assert "<Message>" not in t
    assert [k for _, k, _ in kinds(hook["store"])].count("auto_reply") == 1
    assert [k for _, k, _ in kinds(hook["store"])].count("inbound") == 2
    assert len(hook["alerts"]) == 2  # Ryan still hears about every message


def test_claim_is_once_per_central_day(env):
    late = datetime(2026, 11, 15, 23, 59, tzinfo=cv.TZ)
    assert cv.claim_daily_ack(PHONE, ACK, late) is True
    assert cv.claim_daily_ack(PHONE, ACK, late + timedelta(seconds=30)) is False
    # 00:00 CT is still Nov 15 in UTC (06:00Z) but a new Central day.
    assert cv.claim_daily_ack(PHONE, ACK, late + timedelta(minutes=1)) is True
    assert cv.claim_daily_ack("+15555550143", ACK, late) is True  # per phone


def test_claim_race_only_one_wins(env):
    wins, go = [], threading.Barrier(8)

    def run():
        go.wait()
        wins.append(cv.claim_daily_ack(PHONE, ACK, at(10)))
    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_no_database_means_no_ack(hook, monkeypatch):
    cv.set_store(None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    t = text_in("When you coming out?")
    assert "<Message>" not in t and len(hook["alerts"]) == 1


def test_database_error_means_no_ack_and_masked_log(hook, caplog):
    class Broken(cv.MemoryStore):
        def add(self, rec): raise RuntimeError("down postgres://user:secret@db/x")
    cv.set_store(Broken())
    with caplog.at_level(logging.WARNING):
        t = text_in("When you coming out?")
    assert "<Message>" not in t and len(hook["alerts"]) == 1
    assert "***-0142" in caplog.text and "5555550142" not in caplog.text and "secret" not in caplog.text


def test_slow_database_means_no_ack_and_row_marked_not_sent(hook, monkeypatch):
    class Slow(cv.MemoryStore):
        def add(self, rec):
            time.sleep(0.3)
            return super().add(rec)
    store = Slow()
    cv.set_store(store)
    monkeypatch.setattr(cv, "ACK_DB_TIMEOUT", 0.05)
    start = time.monotonic()
    t = text_in("When you coming out?")
    assert "<Message>" not in t
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        late = [m for m in store.rows if m["kind"] == "auto_reply"]
        if late and late[0]["status"].startswith("error"):
            break
        time.sleep(0.02)
    [late] = [m for m in store.rows if m["kind"] == "auto_reply"]
    assert late["status"] == "error: not sent (database slow)"
    assert time.monotonic() - start < 3


def test_opted_out_row_gets_no_ack_but_ryan_is_told(hook):
    hook["cust"] = customer(opted_in=False, opt="Opted out (STOP)")
    t = text_in("why do I keep getting texts")
    assert "<Message>" not in t and len(hook["alerts"]) == 1
    assert [k for _, k, _ in kinds(hook["store"])] == ["inbound"]


def test_anytime_stop_gets_no_ack(hook):
    hook["cust"] = customer(notes="Anytime stop, no texts")
    t = text_in("Did you come today")
    assert "<Message>" not in t and len(hook["alerts"]) == 1


def test_not_opted_in_customer_free_text_gets_ack(hook):
    hook["cust"] = customer(opted_in=False, opt="")
    assert ACK in text_in("Is this PestPod?")


def test_empty_body_mms_is_free_text(hook):
    t = text_in("")
    assert ACK in t and hook["alerts"][0][3] == ""


# ---------------------------------------------------------------- keywords unchanged
def test_y_unchanged_no_message_alert(hook):
    t = text_in("Y")
    assert "Got it!" in t and ACK not in t
    assert hook["alerts"] == [] and len(hook["y_alerts"]) == 1 and hook["updates"] == [(5, "Y")]


@pytest.mark.parametrize("word", ["N", "no", "No."])
def test_n_unchanged(hook, word):
    t = text_in(word)
    assert "we&apos;ll reach out to reschedule" in t or "we'll reach out to reschedule" in t
    assert hook["updates"] == [(5, "N")] and hook["alerts"] == [] and hook["y_alerts"] == []


def test_n_from_not_opted_in_still_gets_help(hook):
    hook["cust"] = customer(opted_in=False, opt="")
    t = text_in("N")
    assert main.HELP_FALLBACK in t and hook["updates"] == [] and hook["alerts"] == []


@pytest.mark.parametrize("word", ["STOP", "help", "Cancel", "START", "REVOKE", "optout", "INFO"])
def test_opt_words_silent_and_not_alerted(hook, word):
    t = text_in(word)
    assert "<Message>" not in t and hook["alerts"] == []


def test_join_unchanged(hook):
    import html
    assert main.JOIN_REPLY in html.unescape(text_in("JOIN")) and hook["alerts"] == []


def test_unknown_number_unchanged(hook):
    hook["cust"] = None
    t = text_in("When you coming out?", frm="+15555550000")
    assert "No matching appointment found" in t and hook["alerts"] == []
    assert [k for _, k, _ in kinds(hook["store"])] == ["inbound", "auto_reply"]


def test_alert_failure_never_breaks_reply(hook, monkeypatch):
    def boom(*a, **k): raise RuntimeError("resend down")
    monkeypatch.setattr(main.alert_email, "send_message_alert", boom)
    assert ACK in text_in("When you coming out?")


def test_webhook_http_end_to_end(hook, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    r = TestClient(main.app).post("/api/v1/sms/webhook", data={"From": PHONE, "Body": "running late?",
                                                                "MessageSid": "SMh1"})
    assert r.status_code == 200 and ACK in r.text
    assert sorted(k for _, k, _ in kinds(hook["store"])) == ["auto_reply", "inbound"]


# ---------------------------------------------------------------- alert email
@pytest.fixture
def mail_on(monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", "true")
    monkeypatch.setenv("ALERT_EMAIL", "alerts@example.com")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@send.example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRET")


def test_message_alert_payload(mail_on):
    sent = []
    ok = alert_email.send_message_alert("Dana", "0142", "A-100", "When you coming out?", 5,
                                        datetime(2026, 11, 15, 15, 56, tzinfo=timezone.utc),
                                        poster=lambda p, k: sent.append((p, k)))
    assert ok
    [(p, key)] = sent
    assert key == "re_test_SECRET" and p["to"] == ["alerts@example.com"]
    assert p["subject"] == "PestPod: Message from Dana (***-0142)"
    b = p["text"]
    for want in ("Name: Dana", "Phone: ***-0142", "Account: A-100", "Message: When you coming out?",
                 "Time: 2026-11-15 9:56 AM CT",
                 "https://app.pestpod.ai/dashboard/conversation?row=5&acct=A-100"):
        assert want in b
    assert "re_test_SECRET" not in b


def test_message_alert_test_prefix_and_link_encoding(mail_on):
    sent = []
    alert_email.send_message_alert("Ryan", "0156", "TEST-001", "hi", 8, poster=lambda p, k: sent.append(p))
    assert sent[0]["subject"] == "[TEST] PestPod: Message from Ryan (***-0156)"
    assert "conversation?row=8&acct=TEST-001" in sent[0]["text"]
    assert alert_email.conversation_link(9, "A 1&2") == \
        "https://app.pestpod.ai/dashboard/conversation?row=9&acct=A+1%262"
    assert alert_email.conversation_link(None, "X") == "https://app.pestpod.ai/dashboard"


def test_message_alert_off_or_unconfigured(monkeypatch, mail_on):
    called = []
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", "false")
    assert alert_email.send_message_alert("D", "0142", poster=lambda p, k: called.append(1)) is False
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", "yes")
    monkeypatch.delenv("RESEND_API_KEY")
    assert alert_email.send_message_alert("D", "0142", poster=lambda p, k: called.append(1)) is False
    assert called == []


def test_message_alert_failure_returns_false_and_redacts(mail_on, caplog):
    def poster(p, k): raise RuntimeError(f"401 bad key {k}")
    with caplog.at_level(logging.WARNING):
        assert alert_email.send_message_alert("D", "0142", "A", "x", 5, poster=poster) is False
    assert "re_test_SECRET" not in caplog.text and "[redacted]" in caplog.text


def test_message_body_long_and_empty():
    b = alert_email.message_body("D", "0142", "A", "x" * 1500, 5)
    assert "x" * 1000 + "…" in b and "x" * 1001 not in b
    assert "(no text, maybe a picture)" in alert_email.message_body("D", "0142", "A", "  ", 5)
    assert alert_email.message_subject("", "") == "PestPod: Message from customer (***-----)"


# ---------------------------------------------------------------- unread state
def _add(store, kind, direction, when, body="hi", status=None, phone=PHONE):
    store.add({"created_at": when.astimezone(timezone.utc), "phone": phone, "account": "A-100",
               "customer_row": 5, "direction": direction, "kind": kind, "body": body, "twilio_sid": None,
               "status": status, "tech": None, "idempotency_key": None})


def test_unread_counts_and_mark_read(env):
    st = env["store"]
    _add(st, "inbound", "in", at(9), "first")
    _add(st, "inbound", "in", at(9, 30), "second")
    u = cv.unread_counts(now=at(10))
    assert u[PHONE]["count"] == 2 and u[PHONE]["body"] == "second"
    cv.mark_read(PHONE, at(9, 45))
    assert cv.unread_counts(now=at(10)) == {}
    _add(st, "inbound", "in", at(9, 50), "third")
    assert cv.unread_counts(now=at(10))[PHONE]["count"] == 1
    cv.mark_read(PHONE, at(9, 0))  # an older read time never moves the marker back
    assert cv.unread_counts(now=at(10))[PHONE]["count"] == 1


def test_manual_reply_clears_unread_failed_one_does_not(env):
    st = env["store"]
    _add(st, "inbound", "in", at(9), "q")
    _add(st, "manual", "out", at(9, 10), "a", status="error: Twilio 21610")
    assert cv.unread_counts(now=at(10))[PHONE]["count"] == 1
    _add(st, "manual", "out", at(9, 20), "a", status="sent")
    assert cv.unread_counts(now=at(10)) == {}
    _add(st, "auto_reply", "out", at(9, 30), ACK)  # an auto-reply doesn't count as Ryan answering
    _add(st, "inbound", "in", at(9, 40), "q2")
    assert cv.unread_counts(now=at(10))[PHONE]["count"] == 1


def test_unread_window(env):
    _add(env["store"], "inbound", "in", at(9) - timedelta(days=31))
    assert cv.unread_counts(now=at(10)) == {}


def test_unread_and_mark_read_never_raise(env, caplog):
    class Broken(cv.MemoryStore):
        def unread(self, since): raise RuntimeError("postgres://secret")
        def mark_read(self, phone, at): raise RuntimeError("postgres://secret")
    cv.set_store(Broken())
    with caplog.at_level(logging.WARNING):
        assert cv.unread_counts() == {}
        cv.mark_read(PHONE)
    assert "secret" not in caplog.text and "5555550142" not in caplog.text


def test_pgstore_unread_sql(monkeypatch):
    seen = []

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): seen.append((sql, params))
        def fetchall(self): return [(PHONE, 3, at(9), "latest")]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return Cur()
        def commit(self): pass
    monkeypatch.setattr(pgsync, "connect", lambda: Conn())
    monkeypatch.setattr(cv.PgStore, "_ready", True)
    st = cv.PgStore()
    assert st.unread(at(1)) == {PHONE: {"count": 3, "latest": at(9), "body": "latest"}}
    sql = seen[-1][0]
    assert "conversation_reads" in sql and "direction = 'in'" in sql and "kind = 'manual'" in sql
    assert "NOT LIKE 'error%%'" in sql  # %% because psycopg treats % as a placeholder
    st.mark_read(PHONE, at(9))
    assert "ON CONFLICT (phone) DO UPDATE" in seen[-1][0] and "GREATEST" in seen[-1][0]
    assert seen[-1][1] == (PHONE, at(9))


def test_schema_has_conversation_reads():
    assert "CREATE TABLE IF NOT EXISTS conversation_reads" in pgsync.SCHEMA
    assert "phone        TEXT PRIMARY KEY" in pgsync.SCHEMA


# ---------------------------------------------------------------- dashboard badges
def test_dashboard_shows_unread_and_opening_marks_read(env):
    _add(env["store"], "inbound", "in", datetime.now(cv.TZ) - timedelta(minutes=5), "When you coming out?")
    c = client()
    t = c.get("/dashboard").text
    assert "id=unread-texts" in t and "Unread texts" in t and "href='#unread-texts'" in t
    assert "When you coming out?" in t and "Phone ending 0142" in t and "5555550142" not in t
    assert "/dashboard/conversation?row=5&amp;acct=A-100" in t
    assert "title='Unread texts'" in t  # badge on the Wants a time card too
    assert page(c).status_code == 200  # opening the conversation marks it read
    t = c.get("/dashboard").text
    assert "id=unread-texts" not in t and "title='Unread texts'" not in t


def test_dashboard_no_unread_when_reply_box_off(env, monkeypatch):
    _add(env["store"], "inbound", "in", datetime.now(cv.TZ), "q")
    monkeypatch.setenv("REPLY_BOX_ENABLED", "false")
    called = []
    monkeypatch.setattr(cv, "unread_counts", lambda **k: called.append(1) or {})
    t = client().get("/dashboard").text
    assert "Unread texts" not in t and called == []


def test_dashboard_survives_unread_failure(env):
    class Broken(cv.MemoryStore):
        def unread(self, since): raise RuntimeError("down")
    cv.set_store(Broken())
    r = client().get("/dashboard")
    assert r.status_code == 200 and "Unread texts" not in r.text


def test_test_rows_only_with_test_flag(env):
    env["rows"][:] = [cust(row=8, acct="TEST-001", name="Ryan", phone="(555) 555-0156")]
    _add(env["store"], "inbound", "in", datetime.now(cv.TZ), "hey", phone="+15555550156")
    rows = env["rows"]
    assert dashboard.unread_texts(rows) == []
    [x] = dashboard.unread_texts(rows, include_test=True)
    assert (x["row"], x["acct"], x["phone_last4"], x["count"]) == (8, "TEST-001", "0156", 1)


def test_unread_unknown_number_not_listed(env):
    _add(env["store"], "inbound", "in", datetime.now(cv.TZ), "who is this", phone="+15555550999")
    assert dashboard.unread_texts(env["rows"]) == []


def test_unread_snippet_trimmed_and_escaped(env):
    _add(env["store"], "inbound", "in", datetime.now(cv.TZ), "<b>" + "y" * 100)
    [x] = dashboard.unread_texts(env["rows"])
    assert x["snippet"].endswith("…") and len(x["snippet"]) == 81
    assert "<b>" not in dashboard.render_unread([x]) and "&lt;b&gt;" in dashboard.render_unread([x])


# ---------------------------------------------------------------- logging hygiene
def test_twilio_client_request_logging_quieted():
    assert logging.getLogger("twilio.http_client").getEffectiveLevel() >= logging.WARNING


def test_inbound_print_masks_phone(monkeypatch, capsys):
    monkeypatch.setattr(main, "get_sheets", lambda: None)
    main.log_inbound("+15555550142", "hi", "SM1")
    out = capsys.readouterr().out
    assert "***-0142" in out and "5555550142" not in out


def test_changed_code_says_ryan_and_never_writes_formula_columns():
    import inspect
    for mod in (main, cv, alert_email, dashboard):
        assert "Clyde" not in inspect.getsource(mod)
    src = inspect.getsource(main._free_text) + inspect.getsource(cv.claim_daily_ack)
    assert "batchUpdate" not in src and "append(" not in src.replace("alerts.append", "")
