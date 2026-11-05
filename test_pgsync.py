from datetime import datetime
import pgsync

def test_rows_from_values_maps_headers_and_skips_blank():
    vals = [["PestPod title"], ["Account", "Customer Name", "Phone (+1XXXXXXXXXX)", "Next Due", "OK to Text?"],
            ["1", "A Bufkin", "+15045550100", "Feb", "Y"], [], ["", "", ""], ["2", "Lee", "", "", ""]]
    recs = pgsync.rows_from_values("Import Review - Feb", vals)
    assert [r["name"] for r in recs] == ["A Bufkin", "Lee"]
    assert recs[0]["sheet_row"] == 3 and recs[0]["phone"] == "+15045550100"
    assert recs[0]["next_due"] == "Feb" and recs[0]["ok_to_text"] == "Y"
    assert recs[1]["phone"] is None and recs[0]["raw"]["Account"] == "1"

def test_no_header_returns_empty():
    assert pgsync.rows_from_values("Customers", [["x"], ["y"]]) == []

def test_next_run():
    n = datetime(2026, 11, 5, 3, 0, tzinfo=pgsync.CT)
    assert pgsync.next_run(n, 2).day == 6
    assert pgsync.next_run(n.replace(hour=1), 2).hour == 2

def test_sync_never_writes_sheet():
    import inspect
    src = inspect.getsource(pgsync)
    for w in ("batchUpdate", "values().update", "values().append", "values().clear"):
        assert w not in src
