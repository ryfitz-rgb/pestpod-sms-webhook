"""Dashboard editing: add a customer and edit a customer's fields (DASHBOARD_EDIT_ENABLED).

Ships dark: everything here is off (routes 404, no forms shown) unless DASHBOARD_EDIT_ENABLED
is true/1/yes/on. The older phone/address/city Edit in dashboard.py keeps working either way.

How it writes:
  * Through the same Sheets-shaped client as the rest of the dashboard (main.get_sheets()), so it
    writes to whichever customer store is active (CUSTOMER_STORE=postgres -> gridstore; else the Sheet).
  * RAW values only. Dates are stored as day serials (what the store already holds for dates).
  * Never writes Next Due (I) or OK to Text? (K). Both are formulas; K turns TRUE on its own when
    Opt-In Status is "Opted in" and there's a phone.
  * Every add/edit first appends an Undo Log row with the old and new values (JSON of just the
    changed columns), so the dashboard Undo button can put it back.
  * Sends no texts. Doesn't touch batch settings.

Consent (OK to Text?): marking someone Opted in, or changing the phone of someone who is opted in,
needs Ryan to tick "Customer agreed to get texts at this number" plus an Opt-In Method. That save is
logged as action "optin". Customers who are Opted out (or whose texts show a STOP) can't be opted
back in from here; they have to text START/JOIN themselves.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime

import formulas

log = logging.getLogger("pestpod.dashboard.edit")

FLAG = "DASHBOARD_EDIT_ENABLED"

ACCOUNT_COL = "Account"
NAME_COL = "Customer Name"
PHONE_COL = "Phone (+1XXXXXXXXXX)"
LAST_COL = "Last Service Date"
OPTIN_STATUS_COL = "Opt-In Status"
OPTIN_METHOD_COL = "Opt-In Method"
OPTIN_DATE_COL = "Opt-In Date"
SOURCE_COL = "Source"
SOURCE_VALUE = "Dashboard"

# form field name -> (column, kind, max length)
FIELDS = (
    ("name", NAME_COL, "text", 120),
    ("phone", PHONE_COL, "phone", 0),
    ("address", "Address", "text", 200),
    ("city", "City", "text", 80),
    ("service", "Service Type", "text", 60),
    ("freq", "Frequency", "freq", 40),
    ("last", LAST_COL, "date", 0),
    ("optin_status", OPTIN_STATUS_COL, "optin", 0),
    ("optin_method", OPTIN_METHOD_COL, "text", 60),
    ("optin_date", OPTIN_DATE_COL, "date", 0),
    ("pest_notes", "Pest Notes", "text", 1000),
    ("termite_notes", "Termite Notes", "text", 1000),
)
EDITABLE_COLS = tuple(c for _, c, _, _ in FIELDS)
DATE_COLS = {LAST_COL, OPTIN_DATE_COL}
# Columns the dashboard must never write (formulas, webhook/batch bookkeeping, Cancel/Push own theirs).
NEVER_WRITE = {"next due", "ok to text?", "wave", "reminder status", "last reply", "proposed appointment",
               "missed count", "office list month", "status", "cancelled on", "import notes"}
# Columns an undo may put back (an undone add blanks Account and Source too).
UNDO_COLS = set(EDITABLE_COLS) | {ACCOUNT_COL, SOURCE_COL}

FREQ_CHOICES = ("Weekly", "Monthly", "EOM", "Quarterly", "Semi-Annual", "Annual", "Anytime")
OPTIN_CHOICES = ("Not asked", "Pending", "Opted in", "Opted out")
METHOD_SUGGESTIONS = ("Verbal (in person)", "Paper form", "Web form", "Keyword JOIN")
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/#-]{0,39}$")

ERRORS = {
    "badname": "Customer name is required.",
    "badacct": "Account number is required (letters, numbers, dashes; up to 40 characters).",
    "dupacct": "That account number is already on the Customers list. Use a different one.",
    "badphone": "That phone number doesn't look right. Use a 10-digit US number, like (504) 555-0123.",
    "baddate": "That date doesn't look right, or it's in the future.",
    "badoptin": "Pick an Opt-In Status from the list.",
    "badtext": "Text fields can't start with '='.",
    "needconsent": "To mark someone OK to text, tick \"Customer agreed to get texts at this number\".",
    "needmethod": "Add an Opt-In Method (how they agreed) before marking them OK to text.",
    "optedout": "This customer opted out (STOP). They have to text START or JOIN themselves to opt back in.",
    "moved": "That row changed since the page loaded. Refresh and try again.",
}


class EditError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def enabled() -> bool:
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _dash():
    import dashboard
    return dashboard


# ---------------------------------------------------------------- value helpers
def norm(col: str, v) -> str:
    """Cell value -> the string kept in the Undo Log and compared for changes."""
    if v is None or v == "":
        return ""
    if col in DATE_COLS:
        return _dash().norm_date(v)
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = " ".join(str(v).split())
    if col == PHONE_COL:
        try:
            return _dash().clean_phone(s) or s
        except ValueError:
            return s
    return s


def to_cell(col: str, s: str):
    """Undo Log string -> the RAW value to write back (ISO dates go back to day serials)."""
    if s in (None, ""):
        return ""
    if col in DATE_COLS and re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return formulas.serial_from_date(date.fromisoformat(s))
    return s


def acct_key(v) -> str:
    return norm(ACCOUNT_COL, v).casefold()


def parse_date(raw, today: date, earliest: date):
    s = str(raw or "").strip()
    if not s:
        return ""
    d = None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            d = datetime.strptime(s, fmt).date()
            break
        except ValueError:
            pass
    if d is None or d > today or d < earliest:
        raise EditError("baddate")
    return d


def _clean_text(raw, limit: int) -> str:
    s = " ".join(str(raw or "").split())[:limit]
    if s.startswith("="):
        raise EditError("badtext")
    return s


def clean_form(form, today: date, current: dict | None = None) -> dict:
    """{column: cleaned string} for the fields present in the form (absent fields aren't touched).
    Dates come back as ISO strings. Raises EditError."""
    current = current or {}
    out = {}
    for key, col, kind, limit in FIELDS:
        if key not in form or form.get(key) is None:
            continue
        raw = form.get(key)
        if kind == "phone":
            try:
                out[col] = _dash().clean_phone(raw)
            except ValueError:
                raise EditError("badphone")
        elif kind == "date":
            d = parse_date(raw, today, date(2000, 1, 1))
            out[col] = d.isoformat() if d else ""
        elif kind == "freq":
            s = _clean_text(raw, limit)
            out[col] = next((c for c in FREQ_CHOICES if c.casefold() == s.casefold()), s)
        elif kind == "optin":
            s = _clean_text(raw, 60)
            pick = next((c for c in OPTIN_CHOICES if c.casefold() == s.casefold()), None)
            if pick is None and s and s.casefold() != norm(col, current.get(col)).casefold():
                raise EditError("badoptin")
            out[col] = pick if pick is not None else s
        else:
            out[col] = _clean_text(raw, limit)
    if NAME_COL in out and not out[NAME_COL]:
        raise EditError("badname")
    return out


def _is_opted_in(status) -> bool:
    return str(status or "").strip().casefold() == "opted in"


def _is_opted_out(status) -> bool:
    s = str(status or "").strip().casefold()
    return "out" in s or "stop" in s


def _stop_in_texts(phone: str) -> bool:
    """True when the texts on file for this phone end in a STOP (or a Twilio 'unsubscribed' error)."""
    if not phone:
        return False
    try:
        import conversations
        msgs, _ = conversations.thread(phone)
        return conversations.opted_out({}, msgs)
    except Exception as ex:  # a failed read doesn't block; the sheet status check still applies
        log.warning("stop-history check failed: %s", type(ex).__name__)
        return False


def consent_check(old: dict, new: dict, form) -> bool:
    """Decides whether this save turns on (or moves) text consent. Returns True if so (and the
    checks passed). `old`/`new` are full {column: normalized string} views of the row."""
    old_ok = formulas.ok_to_text(old.get(NAME_COL, ""), old.get(PHONE_COL, ""), old.get(OPTIN_STATUS_COL, ""))
    new_ok = formulas.ok_to_text(new.get(NAME_COL, ""), new.get(PHONE_COL, ""), new.get(OPTIN_STATUS_COL, ""))
    becomes_opted_in = _is_opted_in(new.get(OPTIN_STATUS_COL)) and not _is_opted_in(old.get(OPTIN_STATUS_COL))
    phone_moved = new_ok is True and new.get(PHONE_COL, "") != old.get(PHONE_COL, "")
    needed = becomes_opted_in or (new_ok is True and old_ok is not True) or phone_moved
    if not needed:
        return False
    if becomes_opted_in and _is_opted_out(old.get(OPTIN_STATUS_COL)):
        raise EditError("optedout")
    if _stop_in_texts(new.get(PHONE_COL, "")):
        raise EditError("optedout")
    if str(form.get("consent") or "").strip().lower() not in ("yes", "on", "1", "true"):
        raise EditError("needconsent")
    if not new.get(OPTIN_METHOD_COL, "").strip():
        raise EditError("needmethod")
    return True


# ---------------------------------------------------------------- writes
def write_cells(svc, m, header, row: int, values: dict, allowed) -> None:
    """RAW write of {column: value} on one Customers row, refusing formula/bookkeeping columns."""
    d = _dash()
    tab = m.quote_tab(m.CUSTOMERS_TAB)
    data = []
    for col, val in values.items():
        if col not in allowed or col.strip().lower() in NEVER_WRITE:
            raise RuntimeError("refusing to write that column")
        idx = d._col_index(header, col)
        letter = d._safe_letter(m, header, idx)
        data.append({"range": f"{tab}!{letter}{row}", "values": [[val]]})
    if data:
        svc.spreadsheets().values().batchUpdate(spreadsheetId=m.SHEET_ID, body={
            "valueInputOption": "RAW", "data": data}).execute()


def _cells_for(changes: dict) -> dict:
    return {c: to_cell(c, v) for c, v in changes.items()}


def _row_view(d_row: dict, header) -> dict:
    return {c: norm(c, d_row.get(c)) for c in header if c}


def save_edit(row: int, acct: str, form, today: date | None = None) -> str:
    """Edit one customer. Returns 'saved', 'consent' (saved and consent recorded) or 'nochange'.
    Raises EditError for bad input."""
    d = _dash()
    m = d._main()
    svc = m.get_sheets()
    today = today or datetime.now(d.TZ).date()
    data = d.fetch_rows(force=True)
    try:
        cur = d._verify_row(data, row, acct)
    except ValueError:
        raise EditError("moved")
    header = data["header"]
    old = _row_view(cur, header)
    vals = {c: v for c, v in clean_form(form, today, cur).items() if c in header}
    for c in DATE_COLS:
        # A date box can't show text like "call first"; leaving it blank keeps that text.
        if vals.get(c) == "" and old.get(c) and not re.match(r"^\d{4}-\d{2}-\d{2}$", old[c]):
            vals.pop(c)
    new = dict(old, **vals)
    if consent_check(old, new, form):
        action = "optin"
        if OPTIN_DATE_COL in header and new.get(OPTIN_DATE_COL, "") == old.get(OPTIN_DATE_COL, ""):
            new[OPTIN_DATE_COL] = today.isoformat()  # consent is dated the day Ryan records it
    else:
        action = "edit"
    changed = {c: new[c] for c in new if new[c] != old.get(c, "") and c in EDITABLE_COLS}
    if not changed:
        return "nochange"
    old_s = json.dumps({c: old.get(c, "") for c in changed}, sort_keys=True)
    new_s = json.dumps(changed, sort_keys=True)
    d.append_undo(svc, m, row, cur, action, old_s=old_s, new_s=new_s)
    write_cells(svc, m, header, row, _cells_for(changed), EDITABLE_COLS)
    d.fetch_rows(force=True)
    log.info("dashboard: row %s (%s) %s -> %s", row, acct, action, ", ".join(sorted(changed)))
    return "consent" if action == "optin" else "saved"


def _ensure_rows(svc, m, need: int) -> None:
    """Sheet mode only: grow the Customers grid if the new row is past its end."""
    try:
        meta = svc.spreadsheets().get(spreadsheetId=m.SHEET_ID,
                                      fields="sheets.properties(sheetId,title,gridProperties)").execute()
        p = next(s["properties"] for s in meta.get("sheets", []) if s["properties"]["title"] == m.CUSTOMERS_TAB)
    except Exception:
        return
    have = (p.get("gridProperties") or {}).get("rowCount")
    if have and have < need:
        svc.spreadsheets().batchUpdate(spreadsheetId=m.SHEET_ID, body={"requests": [
            {"appendDimension": {"sheetId": p["sheetId"], "dimension": "ROWS", "length": need - have}}]}).execute()


def _last_used_row(data) -> int:
    last = 1
    for r in data["rows"]:
        if any(v not in (None, "") for k, v in r.items() if k != "_row"):
            last = max(last, r["_row"])
    return last


def add_customer(form, today: date | None = None):
    """Add one customer as a new row at the bottom. Returns (row, result) where result is
    'added' or 'consent'. Raises EditError for bad input or a duplicate account number."""
    d = _dash()
    m = d._main()
    svc = m.get_sheets()
    today = today or datetime.now(d.TZ).date()
    acct = " ".join(str(form.get("acct") or "").split())
    if not ACCOUNT_RE.match(acct):
        raise EditError("badacct")
    data = d.fetch_rows(force=True)
    header = data["header"]
    if ACCOUNT_COL not in header or NAME_COL not in header:
        raise RuntimeError("Customers header is missing Account / Customer Name")
    if any(acct_key(r.get(ACCOUNT_COL)) == acct.casefold() for r in data["rows"]):
        raise EditError("dupacct")
    if not str(form.get("name") or "").strip():
        raise EditError("badname")
    vals = {c: v for c, v in clean_form(form, today).items() if c in header}
    if OPTIN_STATUS_COL in header and not vals.get(OPTIN_STATUS_COL):
        vals[OPTIN_STATUS_COL] = "Not asked"
    new = {ACCOUNT_COL: acct, **vals}
    if SOURCE_COL in header:
        new[SOURCE_COL] = SOURCE_VALUE
    action = "add"
    if consent_check({}, new, form):
        action = "optin"
        if OPTIN_DATE_COL in header and not new.get(OPTIN_DATE_COL):
            new[OPTIN_DATE_COL] = today.isoformat()
    new = {c: v for c, v in new.items() if v != ""}
    row = _last_used_row(data) + 1
    _ensure_rows(svc, m, row)
    d.append_undo(svc, m, row, {ACCOUNT_COL: acct, NAME_COL: new.get(NAME_COL, "")}, action,
                  old_s=json.dumps({c: "" for c in new}, sort_keys=True), new_s=json.dumps(new, sort_keys=True))
    write_cells(svc, m, header, row, _cells_for(new), UNDO_COLS)
    d.fetch_rows(force=True)
    log.info("dashboard: added row %s (%s)%s", row, acct, " with text consent" if action == "optin" else "")
    return row, ("consent" if action == "optin" else "added")


# ---------------------------------------------------------------- shared phone heads-up
def _last10(v) -> str:
    return re.sub(r"\D", "", norm(PHONE_COL, v))[-10:]


def shared_phone_note(rows, row: int) -> str:
    """Non-blocking heads-up when this row's phone is also on another active customer.
    Only the last 4 digits are ever shown. '' when there's nothing to say."""
    d = _dash()
    me = next((r for r in rows if r.get("_row") == row), None)
    key = _last10(d._get(me, PHONE_COL, "Phone")) if me else ""
    if len(key) != 10:
        return ""
    others, first = [], None
    for r in rows:
        if _last10(d._get(r, PHONE_COL, "Phone")) != key:
            continue
        if first is None:
            first = r  # the webhook matches replies to the first row with that phone
        if r.get("_row") != row and not d.is_hidden_row(r) and not d.is_cancelled(r):
            others.append(f"Acct {norm(ACCOUNT_COL, r.get(ACCOUNT_COL)) or '?'} · {d._get(r, NAME_COL, 'Name')}")
    if not others:
        return ""
    who = "; ".join(others[:3]) + (f" and {len(others) - 3} more" if len(others) > 3 else "")
    first_acct = norm(ACCOUNT_COL, first.get(ACCOUNT_COL)) or "?"
    return (f"Heads-up: the phone ending {key[-4:]} is also on {who}. Both are saved. Texts from that number "
            f"get matched to the first one on the list (Acct {first_acct}).")


# ---------------------------------------------------------------- undo support
JSON_ACTIONS = ("edit", "optin", "add")


def state_matches(d_row: dict, new_json: str) -> bool:
    """Undo is allowed only while every column the change wrote still holds what it wrote."""
    try:
        want = json.loads(new_json or "{}")
    except ValueError:
        return False
    return all(norm(c, d_row.get(c)) == str(v).strip() for c, v in want.items())


def undo_json(svc, m, header, row: int, old_json: str) -> None:
    old = {c: v for c, v in json.loads(old_json or "{}").items() if c in header}
    write_cells(svc, m, header, row, _cells_for(old), UNDO_COLS)
