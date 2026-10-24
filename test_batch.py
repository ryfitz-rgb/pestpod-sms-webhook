"""Offline tests for batch.py: Sheets and Twilio are mocked; no network, fictional numbers."""
from datetime import datetime, date
import pytest
import batch
from batch import TZ

HEADER = ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Wave", "OK to Text?", "Opt-In Status",
          "Proposed Appointment", "Reminder Status", "Last Reply"]


def row(acct, name, phone, wave="", ok="TRUE", opt="Opted in", appt="Wed 2026-11-25 1:00 PM",
        status="Not sent", reply=""):
    return [acct, name, phone, wave, ok, opt, appt, status, reply]


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


def phone_in_group(g, start=100):
    """Find fictional 555-01xx numbers that hash to day-group g."""
    out = []
    for n in range(start, 200):
        p = f"+1555555{n:04d}"
        if batch.stable_group(p) == g:
            out.append(p)
    return out


def run(svc, now, dry_run=False, sender=None, **kw):
    sender = sender if sender is not None else FakeSender()
    sleeps = []
    s = batch.run(now=now, dry_run=dry_run, svc=svc, sender=sender, sleep=sleeps.append,
                  clock=lambda: now, out=lambda *_: None, **kw)
    return s, sender, sleeps


# ---------------------------------------------------------------- window / day logic
def test_wave_for_day():
    assert batch.wave_for_day(1) == (1, 1) and batch.wave_for_day(3) == (1, 3)
    assert batch.wave_for_day(15) == (2, 1) and batch.wave_for_day(17) == (2, 3)
    for d in (4, 14, 18, 23, 31):
        assert batch.wave_for_day(d) == (None, None)


def test_no_batch_outside_window():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    s, snd, _ = run(svc, at(2026, 11, 4))
    assert s["abort"] and snd.sent == [] and svc.mutations == 0


# ---------------------------------------------------------------- thirds
def test_stable_group_deterministic_and_spread():
    assert batch.stable_group("(555) 555-0101") == batch.stable_group("+15555550101")
    groups = {batch.stable_group(f"+1555555{n:04d}") for n in range(100, 200)}
    assert groups == {1, 2, 3}


def test_wave_column_used_as_group():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("B", "Bo", "+15555550102", wave="2"),
                   row("C", "Cy", "+15555550103", wave="3")])
    _, snd, _ = run(svc, at(2026, 11, 2))
    assert [t for t, _ in snd.sent] == ["+15555550102"]


def test_blank_wave_uses_hash_and_stamps_group():
    p = phone_in_group(2)[0]
    svc = FakeSvc([row("A", "Al", p, wave="")])
    _, snd, _ = run(svc, at(2026, 11, 1))
    assert snd.sent == []
    _, snd, _ = run(svc, at(2026, 11, 2))
    assert [t for t, _ in snd.sent] == [p]
    data = svc.writes[-1]["data"]
    assert {"range": "'Customers'!D2", "values": [["2"]]} in data
    assert svc.writes[-1]["valueInputOption"] == "USER_ENTERED"


def test_catch_up_on_last_day():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("B", "Bo", "+15555550102", wave="2"),
                   row("C", "Cy", "+15555550103", wave="3")])
    _, snd, _ = run(svc, at(2026, 11, 3))   # missed days 1 and 2
    assert len(snd.sent) == 3
    # group 3 only on day 3, not earlier
    svc2 = FakeSvc([row("C", "Cy", "+15555550103", wave="3")])
    assert run(svc2, at(2026, 11, 1))[1].sent == [] and run(svc2, at(2026, 11, 2))[1].sent == []


def test_catch_up_skips_already_sent():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("B", "Bo", "+15555550102", wave="3")])
    run(svc, at(2026, 11, 1))
    _, snd, _ = run(svc, at(2026, 11, 3))
    assert [t for t, _ in snd.sent] == ["+15555550102"]


# ---------------------------------------------------------------- idempotency
def test_same_day_rerun_sends_nothing():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    _, s1, _ = run(svc, at(2026, 11, 1))
    # Simulate the stamp landing in the sheet
    svc.tabs["Customers"][1][7] = "Nudge 1 sent"
    _, s2, _ = run(svc, at(2026, 11, 1, 13))
    assert len(s1.sent) == 1 and s2.sent == []


def test_log_alone_prevents_resend_even_if_stamp_failed():
    log = [["2026-11-01T10:00:00-05:00", "+15555550101", "Al", "1", "b", "SM1", "sent", 2]]
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1", status="Not sent")], outbound=log)
    assert run(svc, at(2026, 11, 3))[1].sent == []


def test_unknown_status_counts_as_sent_error_does_not():
    base = [row("A", "Al", "+15555550101", wave="1")]
    unk = [["2026-11-01T10:00:00-05:00", "+15555550101", "Al", "1", "b", "", "unknown: Timeout", 2]]
    err = [["2026-11-01T10:00:00-05:00", "+15555550101", "Al", "1", "b", "", "error: Twilio 21211", 2]]
    assert run(FakeSvc(base, outbound=unk), at(2026, 11, 3))[1].sent == []
    assert len(run(FakeSvc([list(base[0])], outbound=err), at(2026, 11, 3))[1].sent) == 1


def test_stale_stamp_from_last_month_does_not_block():
    log = [["2026-10-01T10:00:00-05:00", "+15555550101", "Al", "1", "b", "SM1", "sent", 2]]
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1", status="Nudge 1 sent")], outbound=log)
    assert len(run(svc, at(2026, 11, 1))[1].sent) == 1


def test_stamp_without_log_is_conservative():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1", status="Nudge 1 sent")])
    assert run(svc, at(2026, 11, 1))[1].sent == []


def test_duplicate_phone_rows_send_once():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("A2", "Al", "(555) 555-0101", wave="1")])
    assert len(run(svc, at(2026, 11, 1))[1].sent) == 1


# ---------------------------------------------------------------- wave 2
def test_wave2_nonresponder_and_missed_wave1_get_it_repliers_do_not():
    log = [["2026-11-01T10:00:00-05:00", "+15555550101", "Al", "1", "b", "SM1", "sent", 2],
           ["2026-11-01T10:00:02-05:00", "+15555550102", "Bo", "1", "b", "SM2", "sent", 3],
           ["2026-11-01T10:00:04-05:00", "+15555550103", "Cy", "1", "b", "SM3", "sent", 4]]
    svc = FakeSvc([
        row("A", "Al", "+15555550101", wave="1", status="Nudge 1 sent"),        # no reply
        row("B", "Bo", "+15555550102", wave="1", status="Confirmed Y", reply="Y"),
        row("C", "Cy", "+15555550103", wave="1", status="Reschedule N", reply="N"),
        row("D", "Di", "+15555550104", wave="1", status="Not sent"),            # missed wave 1
        row("E", "Ed", "+15555550105", wave="1", status="No reply"),
    ], outbound=log)
    _, snd, _ = run(svc, at(2026, 11, 15))
    assert [t for t, _ in snd.sent] == ["+15555550101", "+15555550104", "+15555550105"]
    statuses = [w["data"][0]["values"][0][0] for w in svc.writes]
    assert statuses == ["Nudge 2 sent"] * 3


def test_no_third_wave():
    log = [["2026-11-15T10:00:00-06:00", "+15555550101", "Al", "2", "b", "SM1", "sent", 2]]
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1", status="Nudge 2 sent")], outbound=log)
    for d in (16, 17, 18, 21, 22, 23, 28):
        assert run(svc, at(2026, 11, d))[1].sent == []


# ---------------------------------------------------------------- eligibility
@pytest.mark.parametrize("kw,reason", [
    (dict(ok="FALSE"), "OK to Text"), (dict(ok=""), "OK to Text"), (dict(ok="#REF!"), "OK to Text"),
    (dict(opt="Opted out"), "Opt-In"), (dict(opt="opted in "), "Opt-In"), (dict(opt="Not asked"), "Opt-In"),
    (dict(appt=""), "no Proposed Appointment"), (dict(status="Confirmed Y"), "Confirmed Y"),
    (dict(appt="Fri 2026-10-30 9:00 AM"), "in the past"),
])
def test_ineligible(kw, reason):
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1", **kw)])
    plan = batch.build_plan(batch.parse_customers(svc.tabs["Customers"])[1], [], at(2026, 11, 1))
    assert plan.sends == [] and reason in plan.skips[0][1]


def test_no_phone_and_example_rows_skipped():
    svc = FakeSvc([row("A", "Al", "", wave="1"), row("EX-001", "Example Customer 1 (EXAMPLE DATA)", "+15555550101", wave="1")])
    plan = batch.build_plan(batch.parse_customers(svc.tabs["Customers"])[1], [], at(2026, 11, 1))
    assert plan.sends == [] and [r for _, r in plan.skips] == ["no valid phone", "example row"]


def test_bool_true_accepted_and_unparseable_appt_allowed():
    svc = FakeSvc([row("A", "Al", "5555550101", wave="1", ok=True, appt="next Tuesday morning")])
    _, snd, _ = run(svc, at(2026, 11, 1))
    assert snd.sent[0][0] == "+15555550101"


def test_parse_appt_date():
    t = date(2026, 11, 1)
    assert batch.parse_appt_date("Tue 2026-10-27 9:00 AM", t) == date(2026, 10, 27)
    assert batch.parse_appt_date("Tue 10/27 at 9 AM", t) == date(2026, 10, 27)
    assert batch.parse_appt_date("1/5", date(2026, 12, 20)) == date(2027, 1, 5)
    assert batch.parse_appt_date("soon", t) is None


# ---------------------------------------------------------------- message + statuses
def test_exact_template_and_messaging_service():
    msg = batch.render_message("Sam", "Thu 2026-11-05 1:00 PM")
    assert msg == ("PestPod: Hi Sam, your pest control service is scheduled for Thu 2026-11-05 1:00 PM. "
                   "Reply Y to confirm or N to reschedule. Reply STOP to opt out. Msg & data rates may apply.")
    assert batch.MESSAGING_SERVICE_SID == "MG7aa08d66618db7ad78ad6af28b793bf2"
    assert "DA " not in batch.TEMPLATE and "due" not in batch.TEMPLATE.lower()


def test_status_values_strict():
    assert set(batch.STATUS_FOR_WAVE.values()) <= set(batch.ALLOWED_STATUSES)
    assert batch.ALLOWED_STATUSES == ("Not sent", "Nudge 1 sent", "Nudge 2 sent", "Nudge 3 sent",
                                      "Confirmed Y", "Reschedule N", "No reply")


def test_live_send_writes_log_and_row_and_pauses():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("B", "Bo", "+15555550104", wave="1")])
    s, snd, sleeps = run(svc, at(2026, 11, 1))
    assert s["sent"] == 2 and sleeps == [1.5]
    assert svc.added == ["Outbound Log"] and svc.tabs["Outbound Log"][0] == batch.OUTBOUND_HEADERS
    ts, phone, name, wave, body, sid, status, rownum = svc.appends[0]
    assert (phone, name, wave, status, rownum) == ("+15555550101", "Al", "1", "sent", 2)
    assert body.startswith("PestPod: Hi Al,") and sid.startswith("SM")
    assert ts == "2026-11-01T10:00:00-06:00"  # CST (DST ended Nov 1)
    assert svc.writes[0]["data"] == [{"range": "'Customers'!H2", "values": [["Nudge 1 sent"]]}]
    for w in svc.writes:
        for d in w["data"]:
            v = d["values"][0][0]
            assert v in batch.ALLOWED_STATUSES or v in batch.ALLOWED_WAVE_VALUES


def test_twilio_rejection_logged_row_not_stamped():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    snd = FakeSender(fail={"+15555550101": batch.SendError("Twilio 21610")})
    s, _, _ = run(svc, at(2026, 11, 1), sender=snd)
    assert s["errors"] == 1 and svc.appends[0][6] == "error: Twilio 21610" and svc.writes == []


def test_max_sends_cap():
    svc = FakeSvc([row(f"A{i}", "Al", f"+1555555{i:04d}", wave="1") for i in range(100, 110)])
    s, snd, _ = run(svc, at(2026, 11, 1), max_sends=4)
    assert len(snd.sent) == 4


# ---------------------------------------------------------------- quiet hours
@pytest.mark.parametrize("h,mi,ok", [(8, 59, False), (9, 0, True), (19, 59, True), (20, 0, False), (23, 0, False)])
def test_quiet_hours(h, mi, ok):
    assert batch.in_quiet_window(at(2026, 11, 1, h, mi)) is ok
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    _, snd, _ = run(svc, at(2026, 11, 1, h, mi))
    assert (len(snd.sent) == 1) is ok
    if not ok:
        assert svc.mutations == 0


def test_quiet_hours_stops_mid_run():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1"), row("B", "Bo", "+15555550104", wave="1")])
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
    svc = FakeSvc([row("A", "Al", "+15555550101", wave=""), row("B", "Bo", "+15555550104", wave="1")])
    lines = []

    def boom(*a): raise AssertionError("sender called in dry run")
    s = batch.run(now=at(2026, 11, 3), dry_run=True, svc=svc, sender=boom, sleep=boom,
                  clock=lambda: at(2026, 11, 3), out=lines.append)
    assert svc.mutations == 0 and s["planned"] == 2
    assert any("WOULD SEND" in l for l in lines) and lines[-1].startswith("Dry run: nothing sent")


def test_dry_run_does_not_need_twilio_creds(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    batch.run(now=at(2026, 11, 1), dry_run=True, svc=svc, out=lambda *_: None)
    assert svc.mutations == 0


def test_live_without_creds_fails_before_any_write(monkeypatch):
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET"):
        monkeypatch.delenv(k, raising=False)
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    with pytest.raises(RuntimeError, match="Twilio credentials missing"):
        batch.run(now=at(2026, 11, 1), dry_run=False, svc=svc, clock=lambda: at(2026, 11, 1), out=lambda *_: None)
    assert svc.mutations == 0


def test_force_wave_live_requires_only_phone():
    svc = FakeSvc([row("A", "Al", "+15555550101", wave="1")])
    with pytest.raises(SystemExit):
        run(svc, at(2026, 11, 10), force_wave=1)
    _, snd, _ = run(svc, at(2026, 11, 10), force_wave=1, only_phone="5555550101")
    assert len(snd.sent) == 1


def test_cli_rejects_now_in_live_mode():
    with pytest.raises(SystemExit):
        batch.main_cli(["--now", "2026-11-01T10:00"])
