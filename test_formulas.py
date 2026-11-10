"""formulas.py must match the Customers ARRAYFORMULAs exactly (I = Next Due, K = OK to Text?)."""
from datetime import date

import pytest

import formulas as f

S = f.serial_from_date


def test_serial_roundtrip_matches_sheets():
    assert S(date(2026, 8, 3)) == 46237          # the sheet's H2 serial (EX-001)
    assert f.date_from_serial(46329) == date(2026, 11, 3)
    assert f.date_from_serial(46329.375) == date(2026, 11, 3)


@pytest.mark.parametrize("freq, last, expect", [
    ("Quarterly", 46237, 46329),                  # sheet row 2: 2026-08-03 -> 2026-11-03
    ("Monthly", 46300, 46331),                    # row 3: 2026-10-05 -> 2026-11-05
    ("Weekly", 46314, 46321),                     # +7 days
    ("EOM", 46321, 46382),                        # EOM = EDATE(+2), as the sheet has it
    ("Semi-Annual", 46152, 46336),
    ("quarterly", 46237, 46329),                  # Sheets "=" ignores case
    ("Monthly", S(date(2026, 1, 31)), S(date(2026, 2, 28))),   # EDATE clamps to month end
    ("Quarterly", S(date(2027, 11, 30)), S(date(2028, 2, 29))),  # leap year
    ("Monthly", S(date(2026, 12, 15)), S(date(2027, 1, 15))),  # year rollover
    ("Monthly", "2026-10-05", 46331),             # a typed ISO date coerces like Sheets
])
def test_next_due(freq, last, expect):
    assert f.next_due(freq, last) == expect


@pytest.mark.parametrize("freq, last", [
    ("", 46237), ("Quarterly", ""), (None, None),
    ("Anytime", 46237), ("Annual", 46237), ("Weekly ", 46237),   # not in the IFS list -> ""
    ("Quarterly", "not a date"), ("Quarterly", True),
])
def test_next_due_blank_cases(freq, last):
    assert f.next_due(freq, last) == ""


@pytest.mark.parametrize("name, phone, status, expect", [
    ("Alice", "+15555550101", "Opted in", True),
    ("Alice", "+15555550101", "opted in", True),   # case-insensitive like Sheets
    ("Alice", "", "Opted in", False),               # no phone
    ("Alice", "+15555550101", "Not asked", False),
    ("Alice", "+15555550101", "Opted out", False),
    ("Alice", " ", "Opted in", True),               # LEN(" ") > 0, exactly like the formula
    ("", "+15555550101", "Opted in", ""),           # blank name -> blank cell
    ("Alice", 5555550101, "Opted in", True),
])
def test_ok_to_text(name, phone, status, expect):
    assert f.ok_to_text(name, phone, status) == expect


def test_computed_cells_positions():
    row = [""] * 12
    row[1], row[2], row[6], row[7], row[11] = "A", "+15555550101", "Quarterly", 46237, "Opted in"
    assert f.computed_cells(row) == {8: 46329, 10: True}
    assert f.computed_cells(["x"]) == {8: "", 10: ""}          # no name -> K blank
    assert f.computed_cells(["x", "Bob"]) == {8: "", 10: False}
