"""Link a web sign-up to an existing customer (dashboard, DASHBOARD_EDIT_ENABLED).

An "unmatched sign-up" is a web opt-in (optin_signups) whose phone isn't on a marked Customers
row. Ryan picks the customer it belongs to, sees a confirm step, and on confirm the server copies
the sign-up onto that customer card:

  * Phone             the sign-up's FULL phone, read from optin_signups on the server. The browser
                      only ever gets the last 4 digits, and nobody types the number.
  * Opt-In Status     Opted in
  * Opt-In Method     Web form
  * Opt-In Date       the day they signed up (Central time)

Same rules as the customer Edit (customer_edit.py):
  * Refused when the customer is Opted out, or the texts on file for either number end in a STOP.
    They have to text START/JOIN themselves.
  * Refused when the sign-up didn't tick the SMS consent box (contact only).
  * Refused when the sign-up isn't on the unmatched list any more (already linked or matched).
  * One Undo Log row (action "link", old/new JSON of the changed columns), so the dashboard Undo
    button puts the card back. The link itself is noted in optin_links (signup id -> account/row).
  * Sends no texts. Never writes Next Due or OK to Text? (OK to Text? turns on by itself).
"""
from __future__ import annotations

import json
import logging
from datetime import date

import customer_edit as ce

log = logging.getLogger("pestpod.dashboard.link")

METHOD = "Web form"
STATUS = "Opted in"
ACTION = "link"

ERRORS = {
    "nosignup": "That sign-up wasn't found. Refresh the dashboard and try again.",
    "linked": "That sign-up isn't unmatched any more (it's already linked or matched). Nothing was changed.",
    "noconsent": "That sign-up didn't tick the text consent box. It's contact only, so it can't be linked "
                 "as Opted in.",
    "badphone": "The sign-up's phone doesn't look like a 10-digit US number, so it can't be copied over.",
    "optedout": ce.ERRORS["optedout"],
    "hidden": "That customer row is a test/example row. Pick a real customer.",
    "moved": ce.ERRORS["moved"],
    "needconfirm": "Nothing was saved. Use the Confirm button on the confirm page to link them.",
    "nochange": "That customer already has this phone and consent. Nothing changed.",
}


class LinkError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _dash():
    import dashboard
    return dashboard


def _signups():
    import pgsync
    if not pgsync.enabled():
        raise LinkError("nosignup")
    return pgsync.list_optins()


def _ct_date(ts):
    import unmatched
    when = unmatched._as_ct(ts)
    return when.date() if when else None


def find_signup(signup_id: int, signups, customer_rows):
    """(entry, subs) for the unmatched person that sign-up id belongs to. entry is the unmatched
    report entry (last 4 only); subs are that person's raw optin_signups rows (full phone, server
    side only). Raises LinkError."""
    import unmatched
    if not any(s.get("id") == signup_id for s in signups):
        raise LinkError("nosignup")
    entry = next((x for x in unmatched.find_unmatched(signups, customer_rows) if signup_id in x["ids"]), None)
    if entry is None:
        raise LinkError("linked")
    subs = [s for s in signups if s.get("id") in entry["ids"]]
    return entry, subs


def _full_phone(subs) -> str:
    latest = max(subs, key=lambda s: (_ct_date(s.get("created_at")) or date.min, s.get("id") or 0))
    try:
        return _dash().clean_phone(latest.get("phone"))
    except ValueError:
        raise LinkError("badphone")


def optin_date(subs):
    """The day (Central) of their latest sign-up that ticked the consent box."""
    ok = [s for s in subs if s.get("sms_consent")]
    dates = [d for d in (_ct_date(s.get("created_at")) for s in ok) if d]
    return max(dates) if dates else None


def _customer(data, row: int, acct: str):
    d = _dash()
    try:
        cur = d._verify_row(data, row, acct)
    except ValueError:
        raise LinkError("moved")
    if d.is_hidden_row(cur):
        raise LinkError("hidden")
    return cur


def _plan(signup_id: int, row: int, acct: str):
    """Everything the confirm page and the save need. Raises LinkError for hard refusals."""
    d = _dash()
    data = d.fetch_rows(force=True)
    entry, subs = find_signup(signup_id, _signups(), data["rows"])
    if not entry["consent"]:
        raise LinkError("noconsent")
    cur = _customer(data, row, acct)
    phone = _full_phone(subs)
    when = optin_date(subs)
    header = data["header"]
    old = ce._row_view(cur, header)
    new = dict(old)
    new[ce.PHONE_COL] = phone
    new[ce.OPTIN_STATUS_COL] = STATUS
    new[ce.OPTIN_METHOD_COL] = METHOD
    new[ce.OPTIN_DATE_COL] = when.isoformat() if when else ""
    return {"data": data, "entry": entry, "cur": cur, "phone": phone, "when": when, "old": old, "new": new}


def _refusal(p) -> str:
    old = p["old"]
    if ce._is_opted_out(old.get(ce.OPTIN_STATUS_COL)):
        return "optedout"
    if ce._stop_in_texts(p["phone"]) or ce._stop_in_texts(old.get(ce.PHONE_COL, "")):
        return "optedout"
    return ""


def _warnings(p) -> list:
    d = _dash()
    old, phone = p["old"], p["phone"]
    out = []
    cur_phone = old.get(ce.PHONE_COL, "")
    if cur_phone and cur_phone != phone:
        out.append(f"This customer already has a different phone (ending {cur_phone[-4:]}). It will be replaced "
                   f"by the sign-up's phone (ending {phone[-4:]}). The old number stays in the Undo Log.")
    if ce._is_opted_in(old.get(ce.OPTIN_STATUS_COL)) and cur_phone and cur_phone != phone:
        out.append("They're already Opted in on the old number. Their consent moves to the sign-up's number.")
    if d.is_cancelled(p["cur"]):
        out.append("This customer is Cancelled. Linking doesn't reactivate them.")
    others = [r for r in p["data"]["rows"]
              if r.get("_row") != p["cur"].get("_row") and not d.is_hidden_row(r) and not d.is_cancelled(r)
              and ce._last10(d._get(r, ce.PHONE_COL, "Phone")) == phone[-10:]]
    for r in others[:3]:
        out.append(f"Heads-up: the phone ending {phone[-4:]} is also on Acct "
                   f"{ce.norm(ce.ACCOUNT_COL, r.get(ce.ACCOUNT_COL)) or '?'} · {d._get(r, ce.NAME_COL, 'Name')}. "
                   f"Both will have it.")
    return out


def preview(signup_id: int, row: int, acct: str) -> dict:
    """What the confirm page shows. Phones only as last 4. 'refused' is an error code or ''."""
    p = _plan(signup_id, row, acct)
    d = _dash()
    old, entry = p["old"], p["entry"]
    return {
        "signup_id": signup_id,
        "signup_name": entry["name"],
        "signup_last4": p["phone"][-4:],
        "submitted_label": entry["submitted_label"],
        "page": entry["page"],
        "optin_date": p["when"].isoformat() if p["when"] else "",
        "row": row,
        "acct": str(p["cur"].get(ce.ACCOUNT_COL, "")).strip(),
        "name": str(d._get(p["cur"], ce.NAME_COL, "Name")),
        "cur_last4": old.get(ce.PHONE_COL, "")[-4:],
        "cur_status": old.get(ce.OPTIN_STATUS_COL, ""),
        "warnings": _warnings(p),
        "refused": _refusal(p),
    }


def link(signup_id: int, row: int, acct: str, confirm, today: date | None = None) -> int:
    """Copy the sign-up onto the customer. Returns the row. Raises LinkError. Sends no texts."""
    if str(confirm or "").strip().lower() not in ("yes", "1", "true", "on"):
        raise LinkError("needconfirm")
    d = _dash()
    m = d._main()
    svc = m.get_sheets()
    p = _plan(signup_id, row, acct)
    code = _refusal(p)
    if code:
        raise LinkError(code)
    old, new = p["old"], p["new"]
    try:  # same consent rules as the Edit form; Ryan's confirm is the consent tick
        ce.consent_check(old, new, {"consent": "yes"})
    except ce.EditError as ex:
        raise LinkError(ex.code if ex.code in ERRORS else "optedout")
    header = p["data"]["header"]
    changed = {c: v for c, v in new.items() if c in header and c in ce.EDITABLE_COLS and v != old.get(c, "")}
    if not changed:
        raise LinkError("nochange")
    old_s = json.dumps({c: old.get(c, "") for c in changed}, sort_keys=True)
    new_s = json.dumps(changed, sort_keys=True)
    d.append_undo(svc, m, row, p["cur"], ACTION, old_s=old_s, new_s=new_s)
    ce.write_cells(svc, m, header, row, ce._cells_for(changed), ce.EDITABLE_COLS)
    d.fetch_rows(force=True)
    with d._cache_lock:
        d._unm_cache.update(t=0, val=None)
    _record(p["entry"]["ids"], ce.norm(ce.ACCOUNT_COL, p["cur"].get(ce.ACCOUNT_COL)), row)
    log.info("dashboard: sign-up %s linked to row %s (%s); no text sent", signup_id, row, acct)
    return row


def _record(ids, acct: str, row: int) -> None:
    """Note the link in optin_links. Best effort: the customer card is already saved, and the
    sign-up leaves the unmatched list because its phone is now on a marked row."""
    try:
        import pgsync
        pgsync.save_optin_link(ids, acct, row)
    except Exception as ex:
        log.warning("optin link note failed: %s", type(ex).__name__)


def picker(signup_id: int) -> dict:
    """The pick-a-customer page: the sign-up (last 4 only) and every visible customer, likely
    matches first. Raises LinkError."""
    d = _dash()
    data = d.fetch_rows()
    entry, _ = find_signup(signup_id, _signups(), data["rows"])
    if not entry["consent"]:
        raise LinkError("noconsent")
    likely = [str(m.get("row")) for m in entry["matched_rows"] + entry["likely_match"]]
    out = []
    for r in data["rows"]:
        if d.is_hidden_row(r):
            continue
        phone = ce.norm(ce.PHONE_COL, d._get(r, ce.PHONE_COL, "Phone"))
        out.append({"row": r["_row"], "acct": str(r.get(ce.ACCOUNT_COL, "")).strip(),
                    "name": str(d._get(r, ce.NAME_COL, "Name")), "city": str(r.get("City") or ""),
                    "last4": phone[-4:] if phone else "", "status": str(r.get(ce.OPTIN_STATUS_COL) or ""),
                    "cancelled": d.is_cancelled(r), "likely": str(r["_row"]) in likely})
    out.sort(key=lambda c: (not c["likely"], c["name"].lower()))
    return {"entry": entry, "customers": out}
