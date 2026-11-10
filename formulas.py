"""Python copies of the two Customers-tab ARRAYFORMULAs (PestPod Reminders v2).

Read from the live sheet on 2026-11-10 (row 2, FORMULA render):

  I2 (Next Due):
    =ARRAYFORMULA(IF((H2:H="")+(G2:G=""),"",IFERROR(IFS(
        G2:G="Weekly",H2:H+7, G2:G="Monthly",EDATE(H2:H,1), G2:G="EOM",EDATE(H2:H,2),
        G2:G="Quarterly",EDATE(H2:H,3), G2:G="Semi-Annual",EDATE(H2:H,6)),"")))

  K2 (OK to Text?):
    =ARRAYFORMULA(IF(B2:B="","",(L2:L="Opted in")*(LEN(C2:C)>0)=1))

Columns: B Customer Name, C Phone, G Frequency, H Last Service Date, L Opt-In Status.
When Postgres is the store these are computed here and never stored, so nothing can
overwrite them. Sheets semantics kept on purpose:
  * "=" text comparison is case-insensitive ("quarterly" matches "Quarterly").
  * An unlisted frequency (Anytime, Annual, blank...) gives "" (IFS error -> IFERROR "").
  * EDATE keeps the day of month, clamped to the last day (Jan 31 + 1 month = Feb 28/29).
  * Dates are Sheets serial numbers (days since 1899-12-30); Next Due is a whole-day serial.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

EPOCH = date(1899, 12, 30)
MONTHS = {"monthly": 1, "eom": 2, "quarterly": 3, "semi-annual": 6}
_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


def serial_from_date(d: date) -> int:
    return (d - EPOCH).days


def date_from_serial(v) -> date:
    return EPOCH + timedelta(days=int(v // 1) if isinstance(v, float) else int(v))


def _blank(v) -> bool:
    return v is None or v == ""


def _as_serial(v):
    """H cell -> serial number, or None when Sheets would error (text that isn't a date)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    m = _ISO.match(str(v))
    if m:
        try:
            return serial_from_date(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return None
    return None


def edate(serial, months: int) -> int:
    d = date_from_serial(serial)
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return serial_from_date(date(y, m, min(d.day, calendar.monthrange(y, m)[1])))


def next_due(frequency, last_service):
    """Column I. Returns a serial number (int/float) or ''."""
    if _blank(last_service) or _blank(frequency):
        return ""
    s = _as_serial(last_service)
    if s is None:
        return ""
    f = str(frequency).casefold() if isinstance(frequency, str) else None
    if f == "weekly":
        return s + 7
    if f in MONTHS:
        return edate(s, MONTHS[f])
    return ""


def ok_to_text(name, phone, opt_in_status):
    """Column K. Returns True/False, or '' when Customer Name is blank."""
    if _blank(name):
        return ""
    opted = isinstance(opt_in_status, str) and opt_in_status.casefold() == "opted in"
    has_phone = len("" if _blank(phone) else (str(phone).upper() if isinstance(phone, bool) else _num_text(phone))) > 0
    return opted and has_phone


def _num_text(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# Column positions (0-based) the formulas use on the Customers tab.
COL_B, COL_C, COL_G, COL_H, COL_I, COL_K, COL_L = 1, 2, 6, 7, 8, 10, 11


def computed_cells(row: list) -> dict:
    """{col_index: value} for the formula columns of one Customers data row."""
    def c(j):
        return row[j] if j < len(row) else ""
    return {COL_I: next_due(c(COL_G), c(COL_H)), COL_K: ok_to_text(c(COL_B), c(COL_C), c(COL_L))}
