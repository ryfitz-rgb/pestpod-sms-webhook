"""
PestPod batch reminder sender.

Wave 1 = the due nudge. It runs every day (America/Chicago) and texts each eligible customer
once per due cycle, starting NUDGE_LEAD_DAYS (7) before their Next Due date (the Customers
tab's Next Due formula: Last Service Date + Frequency). Customers have no fixed appointments;
Proposed Appointment only gets filled after Ryan proposes a day/time and the customer says Y.
Anyone whose Next Due passed more than OVERDUE_GRACE_DAYS ago is not nudged automatically.

Wave 2 (the old appointment reminder) is manual only: --force-wave 2 (live needs --only-phone).

Usage:
  python batch.py --dry-run                         # plan only, writes nothing, sends nothing
  python batch.py --dry-run --now 2026-11-01T10:00  # simulate a date/time (Chicago)
  python batch.py                                   # live (needs Twilio env vars)
  python batch.py --only-phone +1504... --force-wave 1   # live single test send

Env vars:
  GOOGLE_SA_JSON_B64   service account key (same as the webhook)
  SHEET_ID             optional; defaults to PestPod Reminders v1
  TWILIO_ACCOUNT_SID   plus TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET, or TWILIO_AUTH_TOKEN
  TWILIO_MESSAGING_SERVICE_SID  optional; defaults to the PestPod Appointment Alerts service
  BATCH_MAX_SENDS      optional safety cap per run (default 200)
"""
import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import main
from main import TZ, col_letter, normalize_phone, quote_tab

log = logging.getLogger("pestpod.batch")

CUSTOMERS_TAB = main.CUSTOMERS_TAB
OUTBOUND_TAB = "Outbound Log"
OUTBOUND_HEADERS = ["Timestamp", "Phone", "Name", "Wave", "Body", "MessageSid", "Status", "Row"]
MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID") or "MG7aa08d66618db7ad78ad6af28b793bf2"

NUDGE_LEAD_DAYS = 7      # first text goes out this many days before Next Due
OVERDUE_GRACE_DAYS = 7   # still nudge up to this many days after Next Due (missed runs)
# Must match the Reminder Status column's strict ONE_OF_LIST validation.
ALLOWED_STATUSES = ("Not sent", "Nudge 1 sent", "Nudge 2 sent", "Nudge 3 sent",
                    "Confirmed Y", "Reschedule N", "No reply")
STATUS_FOR_WAVE = {1: "Nudge 1 sent", 2: "Nudge 2 sent"}
REPLIED_STATUSES = ("Confirmed Y", "Reschedule N")
QUIET_START_HOUR, QUIET_END_HOUR = 9, 20  # send only 9:00 AM - 8:00 PM Chicago

# Wave 1 due nudge, exact wording approved by Ryan (plain ASCII apostrophes keep it GSM-7 / 1 segment).
TEMPLATE = "PestPod: You're due for pest service. Reply Y and we'll set a time. Reply STOP to opt out."
# Wave 2 (manual only): appointment reminder, used once a Proposed Appointment exists.
APPT_TEMPLATE = ("PestPod: Hi {name}, your pest control service is scheduled for {appt}. "
                 "Reply Y to confirm or N to reschedule. Reply STOP to opt out. Msg & data rates may apply.")

H_ACCOUNT, H_NAME, H_PHONE = "account", ("customer name", "name"), main.PHONE_HEADERS
H_WAVE, H_OK, H_OPTIN = "wave", "ok to text?", "opt-in status"
H_APPT, H_STATUS, H_NEXT_DUE = "proposed appointment", "reminder status", "next due"


# ---------------------------------------------------------------- pure logic
def in_quiet_window(now: datetime) -> bool:
    now = now.astimezone(TZ)
    return QUIET_START_HOUR <= now.hour < QUIET_END_HOUR


def render_message(c, wave: int = 1) -> str:
    msg = TEMPLATE if wave == 1 else APPT_TEMPLATE.format(name=c.name.strip(), appt=c.appt.strip())
    assert msg.startswith("PestPod:")
    return msg


def is_true(v) -> bool:
    return v is True or str(v).strip().upper() == "TRUE"


_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_MDY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def parse_appt_date(text: str, today: date):
    """Best-effort date from a Proposed Appointment / Next Due string; None if unparseable."""
    s = str(text or "")
    try:
        m = _ISO.search(s)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = _MDY.search(s)
        if m:
            mo, d = int(m[1]), int(m[2])
            if m[3]:
                y = int(m[3]); y = y + 2000 if y < 100 else y
                return date(y, mo, d)
            cand = date(today.year, mo, d)
            if (today - cand).days > 180:  # e.g. "1/5" read in December
                cand = date(today.year + 1, mo, d)
            return cand
    except ValueError:
        return None
    return None


@dataclass
class Customer:
    row: int
    account: str
    name: str
    phone: str
    ok_to_text: bool
    opt_in: str
    appt: str
    status: str
    next_due: str = ""

    @property
    def is_example(self) -> bool:
        return "(EXAMPLE DATA)" in self.name.upper() or self.account.upper().startswith("EX-")


@dataclass
class Plan:
    now: datetime
    wave: int = None
    forced: bool = False
    sends: list = field(default_factory=list)   # [Customer]
    skips: list = field(default_factory=list)   # [(Customer, reason)]
    abort: str = ""


def parse_customers(values):
    """values: Customers tab FORMATTED values (list of rows). Returns (cols, [Customer])."""
    header_idx, cols = None, {}
    for i, row in enumerate(values[:20]):
        lowered = [str(c).strip().lower() for c in row]
        if any(h in lowered for h in H_PHONE):
            header_idx = i
            cols = {h: j for j, h in enumerate(lowered) if h}
            break
    if header_idx is None:
        raise ValueError("Customers header row not found")
    for need in (H_OK, H_OPTIN, H_APPT, H_STATUS, H_NEXT_DUE):
        if need not in cols:
            raise ValueError(f"Customers column missing: {need!r}")
    pcol = main._find_col(cols, *H_PHONE)
    ncol = main._find_col(cols, *H_NAME)

    out = []
    for k, r in enumerate(values[header_idx + 1:]):
        def cell(j):
            return str(r[j]).strip() if j is not None and j < len(r) and r[j] is not None else ""
        if not any(str(c).strip() for c in r):
            continue
        out.append(Customer(
            row=header_idx + 2 + k, account=cell(cols.get(H_ACCOUNT)), name=cell(ncol),
            phone=normalize_phone(cell(pcol)),
            ok_to_text=is_true(cell(cols[H_OK])), opt_in=cell(cols[H_OPTIN]),
            appt=cell(cols[H_APPT]), status=cell(cols[H_STATUS]), next_due=cell(cols[H_NEXT_DUE]),
        ))
    return cols, out


def parse_outbound(values):
    """Outbound Log rows -> list of dicts (header row skipped)."""
    out = []
    for r in values[1:] if values else []:
        r = list(r) + [""] * (len(OUTBOUND_HEADERS) - len(r))
        d = dict(zip(OUTBOUND_HEADERS, [str(x).strip() for x in r]))
        try:
            d["_ts"] = datetime.fromisoformat(d["Timestamp"]).astimezone(TZ)
        except ValueError:
            continue
        out.append(d)
    return out


def _counts_as_sent(entry) -> bool:
    st = entry.get("Status", "").lower()
    return st == "sent" or st.startswith("unknown")  # unknown = may have gone out; don't resend


def wave_sends(outbound, phone, wave):
    return [e for e in outbound
            if normalize_phone(e["Phone"]) == phone and e["Wave"] == str(wave) and _counts_as_sent(e)]


def cycle_start(c: Customer, today: date):
    """First day of this customer's current due cycle (sends on/after it count), or None."""
    nd = parse_appt_date(c.next_due, today)
    return nd - timedelta(days=NUDGE_LEAD_DAYS + 1) if nd else None


def already_sent(c: Customer, wave: int, outbound, now: datetime):
    """Wave 1: True if a due nudge was already sent this due cycle (Outbound Log), or the row
    stamp says so and the log doesn't prove the stamp is from an earlier cycle.
    Wave 2: same idea, per calendar month."""
    sends = wave_sends(outbound, c.phone, wave)
    if wave == 1:
        start = cycle_start(c, now.date())
        current = (lambda e: e["_ts"].date() >= start) if start else (lambda e: True)
    else:
        current = lambda e: (e["_ts"].year, e["_ts"].month) == (now.year, now.month)
    if any(current(e) for e in sends):
        return True
    if c.status == STATUS_FOR_WAVE[wave]:
        if sends:  # latest logged send of this wave is from an earlier cycle -> stale stamp
            return False
        return True  # stamped, no log evidence either way: be conservative
    return False


def eligibility(c: Customer, wave: int, today: date, include_examples=False, forced=False):
    """Return '' if eligible, else the skip reason."""
    if c.is_example and not include_examples:
        return "example row"
    if c.status.lower().startswith("excluded"):
        return f"Reminder Status is {c.status!r}"
    if not c.ok_to_text:
        return "OK to Text? not TRUE"
    if c.opt_in != "Opted in":
        return f"Opt-In Status is {c.opt_in or 'blank'!r}"
    if not c.phone or not re.fullmatch(r"\+1\d{10}", c.phone):
        return "no valid phone"
    if wave == 1:
        if c.appt:
            d = parse_appt_date(c.appt, today)
            if d is None or d >= today:
                return f"Proposed Appointment already set ({c.appt!r})"
        if forced:
            return ""
        if not c.next_due:
            return "no Next Due"
        nd = parse_appt_date(c.next_due, today)
        if nd is None:
            return f"Next Due {c.next_due!r} unreadable"
        days = (nd - today).days
        if days > NUDGE_LEAD_DAYS:
            return f"Next Due {nd.isoformat()} is {days} days away (nudge starts {NUDGE_LEAD_DAYS} days before)"
        if days < -OVERDUE_GRACE_DAYS:
            return f"Next Due {nd.isoformat()} was {-days} days ago (past the {OVERDUE_GRACE_DAYS}-day grace)"
        return ""
    # wave 2: appointment reminder
    if not c.appt:
        return "no Proposed Appointment"
    d = parse_appt_date(c.appt, today)
    if d is not None and d < today:
        return f"Proposed Appointment {c.appt!r} is in the past"
    if c.status in REPLIED_STATUSES:
        return f"replied ({c.status})"
    return ""


def build_plan(customers, outbound, now: datetime, force_wave=None, only_phone=None,
               include_examples=False) -> Plan:
    now = now.astimezone(TZ)
    plan = Plan(now=now, wave=force_wave or 1, forced=bool(force_wave))
    seen = set()
    target = normalize_phone(only_phone) if only_phone else None
    for c in customers:
        if target and c.phone != target:
            continue
        reason = eligibility(c, plan.wave, now.date(), include_examples, plan.forced)
        if not reason and c.phone in seen:
            reason = "duplicate phone (earlier row already planned)"
        if not reason and already_sent(c, plan.wave, outbound, now):
            reason = ("due nudge already sent this due cycle" if plan.wave == 1
                      else f"already sent wave {plan.wave} this month")
        if reason:
            plan.skips.append((c, reason))
        else:
            seen.add(c.phone)
            plan.sends.append(c)
    return plan


# ---------------------------------------------------------------- I/O
def read_tab(svc, tab):
    return svc.spreadsheets().values().get(
        spreadsheetId=main.SHEET_ID, range=quote_tab(tab)).execute().get("values", [])


def tab_titles(svc):
    meta = svc.spreadsheets().get(spreadsheetId=main.SHEET_ID, fields="sheets.properties.title").execute()
    return {s["properties"]["title"] for s in meta.get("sheets", [])}


def ensure_outbound_tab(svc):
    if OUTBOUND_TAB in tab_titles(svc):
        return
    svc.spreadsheets().batchUpdate(spreadsheetId=main.SHEET_ID, body={"requests": [
        {"addSheet": {"properties": {"title": OUTBOUND_TAB, "gridProperties": {"frozenRowCount": 1}}}}]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=main.SHEET_ID, range=f"{quote_tab(OUTBOUND_TAB)}!A1",
        valueInputOption="RAW", body={"values": [OUTBOUND_HEADERS]}).execute()


def append_outbound(svc, values):
    svc.spreadsheets().values().append(
        spreadsheetId=main.SHEET_ID, range=f"{quote_tab(OUTBOUND_TAB)}!A:H",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [values]}).execute()


def stamp_row(svc, cols, c: Customer, wave: int):
    status = STATUS_FOR_WAVE[wave]
    assert status in ALLOWED_STATUSES
    data = [{"range": f"{quote_tab(CUSTOMERS_TAB)}!{col_letter(cols[H_STATUS])}{c.row}", "values": [[status]]}]
    svc.spreadsheets().values().batchUpdate(spreadsheetId=main.SHEET_ID, body={
        "valueInputOption": "USER_ENTERED", "data": data}).execute()


class SendError(Exception):
    """Twilio definitively rejected the message (not sent)."""


def make_twilio_sender():
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    key, secret = os.environ.get("TWILIO_API_KEY_SID", "").strip(), os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not ((key and secret) or token):
        raise RuntimeError("Twilio credentials missing: set TWILIO_ACCOUNT_SID and "
                           "TWILIO_API_KEY_SID+TWILIO_API_KEY_SECRET (or TWILIO_AUTH_TOKEN)")
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client
    client = Client(key, secret, sid) if key and secret else Client(sid, token)

    def send_sms(to: str, body: str) -> str:
        try:
            return client.messages.create(messaging_service_sid=MESSAGING_SERVICE_SID, to=to, body=body).sid
        except TwilioRestException as e:
            raise SendError(f"Twilio {e.code}") from None
    return send_sms


def run(now=None, dry_run=True, force_wave=None, only_phone=None, include_examples=False,
        svc=None, sender=None, sleep=time.sleep, clock=None, max_sends=None, out=print):
    """Execute (or dry-run) today's batch. Returns a summary dict."""
    clock = clock or (lambda: datetime.now(TZ))
    now = (now or clock()).astimezone(TZ)
    max_sends = max_sends or int(os.environ.get("BATCH_MAX_SENDS", "200"))
    mode = "DRY RUN" if dry_run else "LIVE"
    out(f"[{mode}] now={now.isoformat(timespec='minutes')} (America/Chicago)")
    if force_wave and not dry_run and not only_phone:
        raise SystemExit("--force-wave in live mode requires --only-phone")
    if include_examples and not dry_run:
        raise SystemExit("--include-examples is dry-run only")

    svc = svc or main.get_sheets()
    if svc is None:
        raise SystemExit("Sheets unavailable (GOOGLE_SA_JSON_B64 missing/invalid)")
    cols, customers = parse_customers(read_tab(svc, CUSTOMERS_TAB))
    try:
        outbound = parse_outbound(read_tab(svc, OUTBOUND_TAB)) if OUTBOUND_TAB in tab_titles(svc) else []
    except Exception as e:
        if not dry_run:
            raise SystemExit(f"Outbound Log unreadable ({type(e).__name__}); aborting to avoid duplicates")
        outbound = []

    plan = build_plan(customers, outbound, now, force_wave, only_phone, include_examples)
    quiet_ok = in_quiet_window(now)
    summary = {"mode": mode, "wave": plan.wave, "planned": len(plan.sends),
               "sent": 0, "errors": 0, "abort": plan.abort}
    if plan.abort:
        out(f"No batch: {plan.abort}.")
    else:
        label = "due nudge" if plan.wave == 1 else "appointment reminder"
        out(f"Wave {plan.wave} ({label})" + (" (forced)" if plan.forced else "")
            + f": {len(plan.sends)} to send, {len(plan.skips)} skipped")
    if not quiet_ok:
        msg = f"outside quiet hours ({now:%-I:%M %p}; sends only 9:00 AM-8:00 PM Chicago)"
        out(("A live run would abort: " if dry_run else "Abort: ") + msg)
        if not dry_run:
            summary["abort"] = summary["abort"] or msg
            return summary
    for c, reason in plan.skips:
        out(f"  SKIP row {c.row} {c.account or '-'} {c.name or '-'}: {reason}")
    if plan.abort:
        return summary
    if len(plan.sends) > max_sends:
        out(f"Capping at BATCH_MAX_SENDS={max_sends} (of {len(plan.sends)}); the rest go on a later run")
        plan.sends = plan.sends[:max_sends]

    if dry_run:
        for c in plan.sends:
            out(f"  WOULD SEND row {c.row} {c.account or '-'} {c.phone} (Next Due {c.next_due or '-'}): "
                f"{render_message(c, plan.wave)}")
            out(f"      would write: Reminder Status -> {STATUS_FOR_WAVE[plan.wave]!r}; Outbound Log row")
        out("Dry run: nothing sent, nothing written.")
        return summary

    sender = sender or make_twilio_sender()
    ensure_outbound_tab(svc)
    for i, c in enumerate(plan.sends):
        if not in_quiet_window(clock()):
            out("Stopping: reached quiet hours"); summary["abort"] = "quiet hours reached mid-run"; break
        if i:
            sleep(1.5)
        body = render_message(c, plan.wave)
        ts = clock().astimezone(TZ).isoformat(timespec="seconds")
        sid, status = "", "sent"
        try:
            sid = sender(c.phone, body)
        except SendError as e:
            status = f"error: {e}"
        except Exception as e:  # network etc.: may or may not have been delivered
            status = f"unknown: {type(e).__name__}"
        try:
            append_outbound(svc, [ts, c.phone, c.name, str(plan.wave), body, sid, status, c.row])
        except Exception as e:
            out(f"  WARN Outbound Log append failed for row {c.row} ({type(e).__name__})")
        if status == "sent" or status.startswith("unknown"):
            try:
                stamp_row(svc, cols, c, plan.wave)
            except Exception as e:
                out(f"  WARN row {c.row} stamp failed ({type(e).__name__})")
        if status == "sent":
            summary["sent"] += 1
        else:
            summary["errors"] += 1
        out(f"  {status.upper()} row {c.row} {c.phone} sid={sid or '-'}")
    out(f"Done: {summary['sent']} sent, {summary['errors']} errors")
    return summary


def main_cli(argv=None):
    p = argparse.ArgumentParser(description="PestPod batch reminder sender")
    p.add_argument("--dry-run", action="store_true", help="plan only; send and write nothing")
    p.add_argument("--now", help="simulate a Chicago date/time, e.g. 2026-11-01T10:00 (dry-run only)")
    p.add_argument("--force-wave", type=int, choices=(1, 2),
                   help="1 = due nudge ignoring the Next Due window; 2 = appointment reminder (live needs --only-phone)")
    p.add_argument("--only-phone", help="limit to one phone number")
    p.add_argument("--include-examples", action="store_true", help="dry-run only: include EXAMPLE DATA rows")
    a = p.parse_args(argv)
    if a.now and not a.dry_run:
        p.error("--now is only allowed with --dry-run")
    now = datetime.fromisoformat(a.now).replace(tzinfo=TZ) if a.now else None
    s = run(now=now, dry_run=a.dry_run, force_wave=a.force_wave, only_phone=a.only_phone,
            include_examples=a.include_examples)
    return 1 if (not a.dry_run and s["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main_cli())
