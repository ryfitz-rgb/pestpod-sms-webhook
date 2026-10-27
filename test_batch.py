"""Offline tests for batch.py: Sheets and Twilio are mocked; no network, fictional numbers."""
from datetime import datetime, date
import pytest
import batch
from batch import TZ

HEADER = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Next Due", "Wave", "OK to Text?", "Opt-In Status",
          "Proposed Appointment", "Reminder Status", "Last Reply"]
NUDGE = "PestPod: You're due for pest service. Reply Y and we'll set a time. Reply STOP to opt out."


def row(acct, name, phone, due="2026-11-10", wave="", ok="TRUE", opt="Opted in", appt="",
        status="Not sent", reply=""):
    return [acct, name, phone, due, wave, ok, opt, appt, status, reply]


def log(phone, ts="2026-11-03T10:00:00-06:00", wave="1", status="sent"):
    return [ts, phone, "x", wave, "b", "SM1", status, 2]


def at(y, m, d, h=10, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class Exec:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()


class FakeSvc:
    def __init__(self, customers, outbound=None):
        self.tabs = {"Customers": [HEADER] + customers}
        if outbound is not None:
            self.tabs["Outbound Log"] = [batch.OUTBOUND_HEADERS] + outbound
        self.writes, self.appends, self.added, self.mutations = [], [], [], 0
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, spreadsheetId, range=None, fields=None):
        if fields:
            return Exec(lambda: {"sheets": [{"properties": {"title": t}} for t in self.tabs]})
        name = range.strip("'")
        return Exec(lambda: {"values": self.tabs.get(name, [])})
    def batchUpdate(self, spreadsheetId, body):
        def run():
            self.mutations += 1
            if "requests" in body:
                for r in body["requests"]:
                    t = r["addSheet"]["properties"]["title"]; self.tabs[t] = []; self.added.append(t)
            else:
                self.writes.append(body)
            return {}
        return Exec(run)
    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            self.mutations += 1; self.tabs["Outbound Log"] = list(body["values"]); return {}
        return Exec(run)
    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def run():
            self.mutations += 1; self.appends.append(body["values"][0])
            self.tabs.setdefault("Outbound Log", []).append(body["values"][0]); return {}
        return Exec(run)


class FakeSender:
    def __init__(self, fail=None): self.sent = []; self.fail = fail or {}
    def __call__(self, to, body):
        if to in self.fail: raise self.fail[to]
        self.sent.append((to, body)); return f"SM{len(self.sent):032d}"


def run(svc, now, dry_run=False, sender=None, **kw):
    sender = sender if sender is not None else FakeSender()
    sleeps = []
    s = batch.run(now=now, dry_run=dry_run, svc=svc, sender=sender, sleep=sleeps.append,
                  clock=lambda: now, out=lambda *_: None, **kw)
    return s, sender, sleeps


# ---------------------------------------------------------------- due window
@pytest.mark.parametrize("d,sends", [
    (2, False),   # 8 days before Next Due 11/10
    (3, True),    # 7 days before
    (9, True), (10, True),
    (17, True),   # 7 days overdue (grace)
    (18, False),  # 8 days overdue
])
def test_due_window(d, sends):
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-10")])
    _, snd, _ = run(svc, at(2026, 11, d))
    assert (len(snd.sent) == 1) is sends


def test_runs_any_day_of_month_no_fixed_windows():
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-12-29")])
    s, snd, _ = run(svc, at(2026, 12, 23))
    assert not s["abort"] and len(snd.sent) == 1 and s["wave"] == 1


def test_next_due_formats_and_blank():
    for due in ("2026-11-10", "11/10/2026", "Tue 2026-11-10"):
        assert len(run(FakeSvc([row("A", "Al", "+15555550101", due=due)]), at(2026, 11, 5))[1].sent) == 1
    plan = batch.build_plan(batch.parse_customers([HEADER, row("A", "Al", "+15555550101", due="")])[1], [], at(2026, 11, 5))
    assert plan.sends == [] and plan.skips[0][1] == "no Next Due"
    plan = batch.build_plan(batch.parse_customers([HEADER, row("A", "Al", "+15555550101", due="soon")])[1], [], at(2026, 11, 5))
    assert "unreadable" in plan.skips[0][1]


def test_future_appointment_blocks_nudge_past_one_does_not():
    svc = FakeSvc([row("A", "Al", "+15555550101", appt="Mon 2026-11-09 9:00 AM"),
                   row("B", "Bo", "+15555550102", appt="Tue 2026-08-11 9:00 AM"),
                   row("C", "Cy", "+15555550103", appt="next Tuesday")])
    _, snd, _ = run(svc, at(2026, 11, 5))
    assert [t for t, _ in snd.sent] == ["+15555550102"]


def test_excluded_rows_never_texted():
    for st in ("Excluded - no SMS", "Excluded - no SMS (anytime stop)"):
        svc = FakeSvc([row("A", "Will", "+15555550101", status=st)])
        assert run(svc, at(2026, 11, 5))[1].sent == []
        assert run(svc, at(2026, 11, 5), force_wave=1, only_phone="+15555550101")[1].sent == []


def test_wave_column_not_required_or_written():
    hdr = [h for h in HEADER if h != "Wave"]
    rows = [[v for k, v in zip(HEADER, row("A", "Al", "+15555550101")) if k != "Wave"]]
    svc = FakeSvc(rows); svc.tabs["Customers"] = [hdr] + rows
    _, snd, _ = run(svc, at(2026, 11, 5))
    assert len(snd.sent) == 1
    assert svc.writes[0]["data"] == [{"range": "'Customers'!H2", "values": [["Nudge 1 sent"]]}]


def test_next_due_column_required():
    hdr = [h for h in HEADER if h != "Next Due"]
    with pytest.raises(ValueError, match="next due"):
        batch.parse_customers([hdr])


# ---------------------------------------------------------------- once per due cycle
def test_same_day_rerun_sends_nothing():
    svc = FakeSvc([row("A", "Al", "+15555550101")])
    _, s1, _ = run(svc, at(2026, 11, 3))
    svc.tabs["Customers"][1][8] = "Nudge 1 sent"
    _, s2, _ = run(svc, at(2026, 11, 3, 13))
    assert len(s1.sent) == 1 and s2.sent == []


def test_one_nudge_per_cycle_across_days_log_only():
    svc = FakeSvc([row("A", "Al", "+15555550101", status="Not sent")], outbound=[log("+15555550101")])
    for d in (4, 8, 10, 15, 17):
        assert run(svc, at(2026, 11, d))[1].sent == []


def test_next_cycle_gets_a_new_nudge():
    # Last cycle's nudge (Aug) and Confirmed Y don't block the November cycle.
    svc = FakeSvc([row("A", "Al", "+15555550101", status="Confirmed Y", reply="Y")],
                  outbound=[log("+15555550101", ts="2026-08-03T10:00:00-05:00")])
    assert len(run(svc, at(2026, 11, 3))[1].sent) == 1
    svc = FakeSvc([row("A", "Al", "+15555550101", status="Nudge 1 sent")],
                  outbound=[log("+15555550101", ts="2026-08-03T10:00:00-05:00")])
    assert len(run(svc, at(2026, 11, 3))[1].sent) == 1


def test_unknown_status_counts_as_sent_error_does_not():
    unk = [log("+15555550101", status="unknown: Timeout")]
    err = [log("+15555550101", status="error: Twilio 21211")]
    assert run(FakeSvc([row("A", "Al", "+15555550101")], outbound=unk), at(2026, 11, 5))[1].sent == []
    assert len(run(FakeSvc([row("A", "Al", "+15555550101")], outbound=err), at(2026, 11, 5))[1].sent) == 1


def test_stamp_without_log_is_conservative():
    svc = FakeSvc([row("A", "Al", "+15555550101", status="Nudge 1 sent")])
    assert run(svc, at(2026, 11, 5))[1].sent == []


def test_duplicate_phone_rows_send_once():
    svc = FakeSvc([row("A", "Al", "+15555550101"), row("A2", "Al", "(555) 555-0101")])
    assert len(run(svc, at(2026, 11, 5))[1].sent) == 1


# ---------------------------------------------------------------- wave 2 (manual only)
def test_wave2_never_runs_on_schedule():
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-12-31", appt="Wed 2026-11-25 1:00 PM",
                       status="Nudge 1 sent")], outbound=[log("+15555550101", ts="2026-11-01T10:00:00-06:00")])
    for d in (15, 16, 17):
        s, snd, _ = run(svc, at(2026, 11, d))
        assert s["wave"] == 1 and snd.sent == []


def test_wave2_forced_appointment_reminder():
    svc = FakeSvc([row("A", "Al", "+15555550101", appt="Wed 2026-11-25 1:00 PM", status="Nudge 1 sent"),
                   row("B", "Bo", "+15555550102", appt="Wed 2026-11-25 1:00 PM", status="Confirmed Y")])
    with pytest.raises(SystemExit):
        run(svc, at(2026, 11, 15), force_wave=2)
    _, snd, _ = run(svc, at(2026, 11, 15), force_wave=2, only_phone="+15555550101")
    assert snd.sent[0][1].startswith("PestPod: Hi Al, your pest control service is scheduled for Wed 2026-11-25")
    assert svc.writes[-1]["data"][0]["values"] == [["Nudge 2 sent"]]
    assert run(svc, at(2026, 11, 15), force_wave=2, only_phone="+15555550102")[1].sent == []


# ---------------------------------------------------------------- eligibility
@pytest.mark.parametrize("kw,reason", [
    (dict(ok="FALSE"), "OK to Text"), (dict(ok=""), "OK to Text"), (dict(ok="#REF!"), "OK to Text"),
    (dict(opt="Opted out"), "Opt-In"), (dict(opt="opted in "), "Opt-In"), (dict(opt="Not asked"), "Opt-In"),
    (dict(due=""), "no Next Due"), (dict(due="2026-12-10"), "days away"), (dict(due="2026-10-01"), "grace"),
    (dict(appt="Thu 2026-11-12 9:00 AM"), "already set"),
])
def test_ineligible(kw, reason):
    svc = FakeSvc([row("A", "Al", "+15555550101", **kw)])
    plan = batch.build_plan(batch.parse_customers(svc.tabs["Customers"])[1], [], at(2026, 11, 5))
    assert plan.sends == [] and reason in plan.skips[0][1]


def test_no_phone_and_example_rows_skipped():
    svc = FakeSvc([row("A", "Al", ""), row("EX-001", "Example Customer 1 (EXAMPLE DATA)", "+15555550101")])
    plan = batch.build_plan(batch.parse_customers(svc.tabs["Customers"])[1], [], at(2026, 11, 5))
    assert plan.sends == [] and [r for _, r in plan.skips] == ["no valid phone", "example row"]


def test_bool_true_accepted():
    svc = FakeSvc([row("A", "Al", "5555550101", ok=True)])
    _, snd, _ = run(svc, at(2026, 11, 5))
    assert snd.sent[0][0] == "+15555550101"


def test_parse_appt_date():
    t = date(2026, 11, 1)
    assert batch.parse_appt_date("Tue 2026-10-27 9:00 AM", t) == date(2026, 10, 27)
    assert batch.parse_appt_date("Tue 10/27 at 9 AM", t) == date(2026, 10, 27)
    assert batch.parse_appt_date("1/5", date(2026, 12, 20)) == date(2027, 1, 5)
    assert batch.parse_appt_date("soon", t) is None


# ---------------------------------------------------------------- message + statuses
def test_exact_due_nudge_template_and_messaging_service():
    c = batch.parse_customers([HEADER, row("A", "Sam", "+15555550101")])[1][0]
    assert batch.render_message(c) == NUDGE == batch.TEMPLATE
    assert batch.TEMPLATE.isascii() and len(batch.TEMPLATE) <= 160   # GSM-7, one segment
    assert batch.MESSAGING_SERVICE_SID == "MG7aa08d66618db7ad78ad6af28b793bf2"
    assert batch.TEMPLATE.startswith("PestPod:") and batch.APPT_TEMPLATE.startswith("PestPod:")


def test_status_values_strict():
    assert set(batch.STATUS_FOR_WAVE.values()) <= set(batch.ALLOWED_STATUSES)
    assert batch.ALLOWED_STATUSES == ("Not sent", "Nudge 1 sent", "Nudge 2 sent", "Nudge 3 sent",
                                      "Confirmed Y", "Reschedule N", "No reply")


def test_live_send_writes_log_and_row_and_pauses():
    svc = FakeSvc([row("A", "Al", "+15555550101"), row("B", "Bo", "+15555550104")])
    s, snd, sleeps = run(svc, at(2026, 11, 3))
    assert s["sent"] == 2 and sleeps == [1.5]
    assert svc.added == ["Outbound Log"] and svc.tabs["Outbound Log"][0] == batch.OUTBOUND_HEADERS
    ts, phone, name, wave, body, sid, status, rownum = svc.appends[0]
    assert (phone, name, wave, status, rownum, body) == ("+15555550101", "Al", "1", "sent", 2, NUDGE)
    assert sid.startswith("SM") and ts == "2026-11-03T10:00:00-06:00"
    assert svc.writes[0]["data"] == [{"range": "'Customers'!I2", "values": [["Nudge 1 sent"]]}]
    for w in svc.writes:
        for d in w["data"]:
            assert d["values"][0][0] in batch.ALLOWED_STATUSES


def test_twilio_rejection_logged_row_not_stamped():
    svc = FakeSvc([row("A", "Al", "+15555550101")])
    snd = FakeSender(fail={"+15555550101": batch.SendError("Twilio 21610")})
    s, _, _ = run(svc, at(2026, 11, 5), sender=snd)
    assert s["errors"] == 1 and svc.appends[0][6] == "error: Twilio 21610" and svc.writes == []


def test_max_sends_cap():
    svc = FakeSvc([row(f"A{i}", "Al", f"+1555555{i:04d}") for i in range(100, 110)])
    s, snd, _ = run(svc, at(2026, 11, 5), max_sends=4)
    assert len(snd.sent) == 4


# ---------------------------------------------------------------- quiet hours
@pytest.mark.parametrize("h,mi,ok", [(8, 59, False), (9, 0, True), (19, 59, True), (20, 0, False), (23, 0, False)])
def test_quiet_hours(h, mi, ok):
    assert batch.in_quiet_window(at(2026, 11, 1, h, mi)) is ok
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-03")])
    _, snd, _ = run(svc, at(2026, 11, 1, h, mi))
    assert (len(snd.sent) == 1) is ok
    if not ok:
        assert svc.mutations == 0


def test_quiet_hours_stops_mid_run():
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-03"), row("B", "Bo", "+15555550104", due="2026-11-03")])
    times = iter([at(2026, 11, 1, 19, 59)] * 2 + [at(2026, 11, 1, 20, 0)] * 10)
    snd = FakeSender()
    batch.run(now=at(2026, 11, 1, 19, 59), dry_run=False, svc=svc, sender=snd, sleep=lambda _: None,
              clock=lambda: next(times), out=lambda *_: None)
    assert len(snd.sent) == 1


def test_quiet_hours_uses_chicago_time():
    from zoneinfo import ZoneInfo
    assert batch.in_quiet_window(datetime(2026, 11, 2, 15, 30, tzinfo=ZoneInfo("UTC")))       # 9:30 CST
    assert not batch.in_quiet_window(datetime(2026, 11, 2, 2, 30, tzinfo=ZoneInfo("UTC")))    # 8:30 PM CST


# ---------------------------------------------------------------- dry run
def test_dry_run_writes_and_sends_nothing():
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-03"), row("B", "Bo", "+15555550104", due="2026-11-03")])
    lines = []

    def boom(*a): raise AssertionError("sender called in dry run")
    s = batch.run(now=at(2026, 11, 3), dry_run=True, svc=svc, sender=boom, sleep=boom,
                  clock=lambda: at(2026, 11, 3), out=lines.append)
    assert svc.mutations == 0 and s["planned"] == 2
    assert any("WOULD SEND" in l for l in lines) and lines[-1].startswith("Dry run: nothing sent")


def test_dry_run_does_not_need_twilio_creds(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-03")])
    batch.run(now=at(2026, 11, 1), dry_run=True, svc=svc, out=lambda *_: None)
    assert svc.mutations == 0


def test_live_without_creds_fails_before_any_write(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2026-11-03")])
    with pytest.raises(RuntimeError, match="Twilio credentials missing"):
        batch.run(now=at(2026, 11, 1), dry_run=False, svc=svc, clock=lambda: at(2026, 11, 1), out=lambda *_: None)
    assert svc.mutations == 0


def test_force_wave_live_requires_only_phone_and_ignores_due_window():
    svc = FakeSvc([row("A", "Al", "+15555550101", due="2027-02-03")])
    assert run(svc, at(2026, 11, 10))[1].sent == []
    with pytest.raises(SystemExit):
        run(svc, at(2026, 11, 10), force_wave=1)
    _, snd, _ = run(svc, at(2026, 11, 10), force_wave=1, only_phone="5555550101")
    assert snd.sent == [("+15555550101", NUDGE)]


def test_cli_rejects_now_in_live_mode():
    with pytest.raises(SystemExit):
        batch.main_cli(["--now", "2026-11-01T10:00"])
