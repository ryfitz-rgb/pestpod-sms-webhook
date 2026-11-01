"""PestPod dashboard v1: a login-gated route view of the Customers tab.

Routes (all under /dashboard):
  GET  /dashboard/login, POST /dashboard/login, POST /dashboard/logout
  GET  /dashboard                      customers grouped by due month
  POST /dashboard/serviced             writes ONLY column H (Last Service Date) of one row
  POST /dashboard/push                 writes ONLY the row's Office List Month (next month)
  POST /dashboard/cancel               writes ONLY Status=Cancelled + Cancelled On (columns added at the END)
  POST /dashboard/reactivate           writes ONLY Status=Active + clears Cancelled On
  POST /dashboard/undo                 reverts the row's most recent un-undone change from the Undo Log

Every serviced/cancel/reactivate write first appends a row to the "Undo Log" tab.

Never writes Next Due (I) or OK to Text? (K): both are sheet formulas. Sends no texts.

Env vars:
  DASHBOARD_PASSWORD   required; dashboard is disabled (503) when unset.
  DASHBOARD_USER       optional, default "ryan".
  DASHBOARD_SECRET     optional extra key material for session signing.
"""
import base64
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("pestpod.dashboard")
router = APIRouter()
TZ = ZoneInfo("America/Chicago")
COOKIE = "pp_session"
LOGIN_CSRF_COOKIE = "pp_login_csrf"
SESSION_TTL = 30 * 24 * 3600
MAX_SKIP_COLS = {"Next Due", "OK to Text?"}  # formulas: never written

H_COL = "Last Service Date"
OLM_COL = "Office List Month"
STATUS_COL = "Status"
CANCELLED_ON_COL = "Cancelled On"
UNDO_TAB = "Undo Log"
UNDO_HEADERS = ["Timestamp CT", "Row", "Account", "Customer", "Old Last Service", "New Last Service", "Undone",
                "Action", "Old Status", "New Status"]
PROTECTED_LETTERS = ("I", "K")
PHONE_COL = "Phone (+1XXXXXXXXXX)"
EDIT_COLS = (PHONE_COL, "Address", "City")  # the only columns Edit may write
NEVER_EDIT = {"next due", "ok to text?", "opt-in status", "opt-in method", "opt-in date", "reminder status",
              "wave", "last reply"}

_fail = {}  # ip -> (count, first_ts)
_fail_lock = threading.Lock()


# ------------------------------------------------------------------ auth helpers
def _password():
    return os.environ.get("DASHBOARD_PASSWORD", "")


def _user():
    return os.environ.get("DASHBOARD_USER", "ryan")


def _key() -> bytes:
    return hashlib.sha256(("pestpod-dash|" + _password() + "|" + os.environ.get("DASHBOARD_SECRET", "")).encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(user: str, now=None) -> str:
    exp = int((now or time.time()) + SESSION_TTL)
    nonce = secrets.token_hex(8)
    payload = f"{user}|{exp}|{nonce}"
    raw = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{raw}.{_sign(payload)}"


def read_session(token):
    if not token or "." not in token or not _password():
        return None
    raw, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(raw.encode()).decode()
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    parts = payload.split("|")
    if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) < time.time():
        return None
    return payload


def csrf_for(session_payload: str) -> str:
    return _sign("csrf|" + session_payload)[:40]


def _secure(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _throttled(ip: str) -> bool:
    with _fail_lock:
        n, t0 = _fail.get(ip, (0, time.time()))
        if time.time() - t0 > 900:
            _fail.pop(ip, None)
            return False
        return n >= 10


def _record_fail(ip: str):
    with _fail_lock:
        n, t0 = _fail.get(ip, (0, time.time()))
        if time.time() - t0 > 900:
            n, t0 = 0, time.time()
        _fail[ip] = (n + 1, t0)


def _headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
    return resp


def _disabled():
    return _headers(HTMLResponse(_page("Dashboard unavailable",
        "<div class='card narrow'><h1>Dashboard not configured</h1><p>Set DASHBOARD_PASSWORD on the server.</p></div>"),
        status_code=503))


# ------------------------------------------------------------------ sheet helpers
_cache = {"t": 0, "rows": None}
_cache_lock = threading.Lock()


def _main():
    import main
    return main


def serial_to_date(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_month(v):
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def next_month(ym):
    y, m = ym
    return (y + 1, 1) if m == 12 else (y, m + 1)


def fetch_rows(force=False):
    with _cache_lock:
        if not force and _cache["rows"] is not None and time.time() - _cache["t"] < 30:
            return _cache["rows"]
    m = _main()
    svc = m.get_sheets()
    if svc is None:
        raise RuntimeError("Sheets unavailable")
    vals = svc.spreadsheets().values().get(
        spreadsheetId=m.SHEET_ID, range=m.quote_tab(m.CUSTOMERS_TAB),
        valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER",
    ).execute().get("values", [])
    if not vals:
        return {"header": [], "rows": [], "undo": []}
    header = [str(h).strip() for h in vals[0]]
    rows = []
    for i, r in enumerate(vals[1:], start=2):
        d = {header[j]: (r[j] if j < len(r) else "") for j in range(len(header))}
        d["_row"] = i
        rows.append(d)
    data = {"header": header, "rows": rows, "undo": read_undo_log(svc, m)}
    with _cache_lock:
        _cache.update(t=time.time(), rows=data)
    return data


def _get(d, *names):
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return ""


def is_hidden_row(d) -> bool:
    acct = str(d.get("Account", "")).strip().upper()
    name = str(_get(d, "Customer Name", "Name"))
    if not name.strip():
        return True
    return acct.startswith("EX-") or acct.startswith("TEST") or "EXAMPLE DATA" in name.upper()


ANYTIME = re.compile(r"anytime stop|just shows up|anytime,? no texts", re.I)
PREF = re.compile(r"(preferred time[^;]*|prefers? [^;]*(morning|afternoon|early|am\b|pm\b)[^;]*|"
                  r"best around [^;]*|[A-Za-z]+ mornings only[^;]*|mondays[^;]*)", re.I)


def view_row(d, today: date):
    nd = serial_to_date(d.get("Next Due"))
    ls = serial_to_date(d.get(H_COL))
    due_ym = (nd.year, nd.month) if nd else None
    olm = parse_month(d.get(OLM_COL))
    eff = due_ym
    pushed = False
    if olm and (eff is None or olm > eff):
        pushed = eff is not None
        eff = olm if eff is not None else None  # no Next Due = no cycle; keep "No due date"
    notes = "; ".join(str(x) for x in (d.get("Pest Notes", ""), d.get("Termite Notes", "")) if str(x).strip())
    prefs = []
    for mt in PREF.finditer(notes):
        seg = mt.group(0).strip(" ;")
        if seg and seg not in prefs:
            prefs.append(seg)
    cur = (today.year, today.month)
    return {
        "row": d["_row"], "acct": str(d.get("Account", "")), "name": str(_get(d, "Customer Name", "Name")),
        "address": str(d.get("Address", "")), "city": str(d.get("City", "")),
        "phone": str(_get(d, "Phone (+1XXXXXXXXXX)", "Phone")).strip(),
        "has_phone": bool(str(_get(d, "Phone (+1XXXXXXXXXX)", "Phone")).strip()),
        "service": str(d.get("Service Type", "")), "freq": str(d.get("Frequency", "")),
        "last": ls, "next_due": nd, "eff": eff, "pushed": pushed,
        "overdue": bool(eff and eff < cur), "anytime": bool(ANYTIME.search(notes)),
        "prefs": prefs, "notes": notes,
    }


# ------------------------------------------------------------------ status + undo helpers
def is_cancelled(d) -> bool:
    return str(d.get(STATUS_COL, "")).strip().lower() == "cancelled"


def norm_date(v) -> str:
    """Cell value -> ISO date string ('' if blank; raw text if not a date)."""
    if v in (None, ""):
        return ""
    dt = serial_to_date(v)
    return dt.isoformat() if dt else str(v).strip()


def status_state(d) -> str:
    """'' = Active, else 'Cancelled|<ISO date>'."""
    if not is_cancelled(d):
        return ""
    return "Cancelled|" + norm_date(d.get(CANCELLED_ON_COL))


def read_undo_log(svc, m):
    try:
        vals = svc.spreadsheets().values().get(
            spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(UNDO_TAB)}!A:J",
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    except Exception:
        return []  # tab not created yet
    out = []
    for i, r in enumerate(vals[1:], start=2):
        r = [str(x) if x is not None else "" for x in r] + [""] * (len(UNDO_HEADERS) - len(r))
        e_ = dict(zip(UNDO_HEADERS, r))
        e_["_log_row"] = i
        e_["Action"] = e_["Action"] or "serviced"
        out.append(e_)
    return out


def edit_state(d) -> str:
    import json
    return json.dumps({c: str(d.get(c, "") if d.get(c) is not None else "").strip() for c in EDIT_COLS},
                      sort_keys=True)


def current_value(d, action):
    if action == "serviced":
        return norm_date(d.get(H_COL))
    if action == "edit":
        return edit_state(d)
    return status_state(d)


def clean_phone(raw) -> str:
    """'' stays ''; any 10-digit US number (or 1 + 10) -> +1XXXXXXXXXX; else ValueError."""
    s = str(raw or "").strip()
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        raise ValueError("bad phone")
    return "+1" + digits


def _write_edit_cells(svc, m, header, row, values: dict):
    tab = m.quote_tab(m.CUSTOMERS_TAB)
    data = []
    for col, val in values.items():
        if col not in EDIT_COLS:
            raise RuntimeError("refusing to edit that column")
        idx = _col_index(header, col)
        if header[idx].strip().lower() in NEVER_EDIT:
            raise RuntimeError("refusing to edit that column")
        letter = _safe_letter(m, header, idx, ("H",))
        data.append({"range": f"{tab}!{letter}{row}", "values": [[val]]})
    svc.spreadsheets().values().batchUpdate(spreadsheetId=m.SHEET_ID, body={
        "valueInputOption": "RAW", "data": data}).execute()


def write_edit(row: int, acct: str, phone: str, address: str, city: str):
    """Returns 'saved' or 'nochange'. Raises ValueError('bad phone') for invalid numbers.
    Writes ONLY phone/address/city; never touches opt-in/consent/status columns (no auto opt-in)."""
    import json
    m = _main()
    svc = m.get_sheets()
    p = clean_phone(phone)
    data = fetch_rows(force=True)
    d = _verify_row(data, row, acct)
    header = data["header"]
    new = {PHONE_COL: p, "Address": " ".join(address.split())[:200], "City": " ".join(city.split())[:80]}
    new = {k: v for k, v in new.items() if k in header}
    old_state = edit_state(d)
    new_full = json.loads(old_state)
    new_full.update(new)
    new_state = json.dumps(new_full, sort_keys=True)
    if new_state == old_state:
        return "nochange"
    append_undo(svc, m, row, d, "edit", old_s=old_state, new_s=new_state)
    _write_edit_cells(svc, m, header, row, new)
    fetch_rows(force=True)
    return "saved"


def undo_candidate(data, row: int):
    """Latest un-undone Undo Log entry for row, plus whether it can still be undone."""
    d = next((x for x in data["rows"] if x["_row"] == row), None)
    for e_ in reversed(data.get("undo") or []):
        if str(e_["Row"]).strip() in (str(row), f"{row}.0") and not e_["Undone"].strip():
            if d is None or str(d.get("Account", "")).strip() != e_["Account"].strip():
                return e_, False
            new = e_["New Last Service"] if e_["Action"] == "serviced" else e_["New Status"]
            return e_, current_value(d, e_["Action"]) == new.strip()
    return None, False


def _ensure_undo_tab(svc, m):
    meta = svc.spreadsheets().get(spreadsheetId=m.SHEET_ID, fields="sheets.properties.title").execute()
    if UNDO_TAB not in {s["properties"]["title"] for s in meta.get("sheets", [])}:
        svc.spreadsheets().batchUpdate(spreadsheetId=m.SHEET_ID, body={"requests": [
            {"addSheet": {"properties": {"title": UNDO_TAB}}}]}).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(UNDO_TAB)}!A1",
            valueInputOption="RAW", body={"values": [UNDO_HEADERS]}).execute()


def append_undo(svc, m, row, d, action, old_h="", new_h="", old_s="", new_s=""):
    _ensure_undo_tab(svc, m)
    ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    svc.spreadsheets().values().append(
        spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(UNDO_TAB)}!A:J", valueInputOption="RAW",
        insertDataOption="INSERT_ROWS", body={"values": [[
            ts, row, str(d.get("Account", "")), str(_get(d, "Customer Name", "Name")),
            old_h, new_h, "", action, old_s, new_s]]}).execute()


def _safe_letter(m, header, idx, extra=()):
    letter = m.col_letter(idx)
    if header[idx] in MAX_SKIP_COLS or letter in PROTECTED_LETTERS + tuple(extra):
        raise RuntimeError("refusing to write a formula column")
    return letter


def _ensure_status_cols(svc, m, data):
    """Add Status / Cancelled On headers at the END of the Customers header row if missing."""
    header = list(data["header"])
    missing = [c for c in (STATUS_COL, CANCELLED_ON_COL) if c not in header]
    if not missing:
        return header
    while header and not header[-1]:
        header.pop()
    start = len(header)
    need = start + len(missing)
    meta = svc.spreadsheets().get(spreadsheetId=m.SHEET_ID,
                                  fields="sheets.properties(sheetId,title,gridProperties.columnCount)").execute()
    p = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == m.CUSTOMERS_TAB)
    have = p.get("gridProperties", {}).get("columnCount", 26)
    if have < need:
        svc.spreadsheets().batchUpdate(spreadsheetId=m.SHEET_ID, body={"requests": [
            {"appendDimension": {"sheetId": p["sheetId"], "dimension": "COLUMNS", "length": need - have}}]}).execute()
    for c in missing:
        if m.col_letter(len(header)) in PROTECTED_LETTERS:
            raise RuntimeError("refusing to add a column there")
        header.append(c)
    svc.spreadsheets().values().update(
        spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(m.CUSTOMERS_TAB)}!{m.col_letter(start)}1",
        valueInputOption="RAW", body={"values": [missing]}).execute()
    return header


def _write_status(svc, m, header, row, state):
    """state '' = Active (Status 'Active', Cancelled On blank) or 'Cancelled|ISO'."""
    si, ci = _col_index(header, STATUS_COL), _col_index(header, CANCELLED_ON_COL)
    sl, cl = _safe_letter(m, header, si, ("H",)), _safe_letter(m, header, ci, ("H",))
    status, _, on = state.partition("|") if state else ("Active", "", "")
    tab = m.quote_tab(m.CUSTOMERS_TAB)
    svc.spreadsheets().values().batchUpdate(spreadsheetId=m.SHEET_ID, body={
        "valueInputOption": "USER_ENTERED", "data": [
            {"range": f"{tab}!{sl}{row}", "values": [[status]]},
            {"range": f"{tab}!{cl}{row}", "values": [[on]]}]}).execute()


def write_status(row: int, acct: str, cancel: bool, today: date):
    m = _main()
    svc = m.get_sheets()
    data = fetch_rows(force=True)
    d = _verify_row(data, row, acct)
    if cancel == is_cancelled(d):
        return False  # already in that state
    header = _ensure_status_cols(svc, m, data)
    old = status_state(d)
    new = f"Cancelled|{today.isoformat()}" if cancel else ""
    append_undo(svc, m, row, d, "cancel" if cancel else "reactivate", old_s=old, new_s=new)
    _write_status(svc, m, header, row, new)
    fetch_rows(force=True)
    return True


def undo_last(row: int, acct: str):
    """Returns 'undone', 'changed' (cell no longer matches), or 'none'."""
    m = _main()
    svc = m.get_sheets()
    data = fetch_rows(force=True)
    d = _verify_row(data, row, acct)
    e_, ok = undo_candidate(data, row)
    if e_ is None:
        return "none"
    if not ok or e_["Account"].strip() != acct.strip():
        return "changed"
    tab = m.quote_tab(m.CUSTOMERS_TAB)
    if e_["Action"] == "serviced":
        letter = _safe_letter(m, data["header"], _col_index(data["header"], H_COL))
        svc.spreadsheets().values().update(
            spreadsheetId=m.SHEET_ID, range=f"{tab}!{letter}{row}", valueInputOption="USER_ENTERED",
            body={"values": [[e_["Old Last Service"]]]}).execute()
    elif e_["Action"] == "edit":
        import json
        old = {k: v for k, v in json.loads(e_["Old Status"]).items() if k in data["header"]}
        _write_edit_cells(svc, m, data["header"], row, old)
    else:
        _write_status(svc, m, data["header"], row, e_["Old Status"].strip())
    svc.spreadsheets().values().update(
        spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(UNDO_TAB)}!G{e_['_log_row']}", valueInputOption="RAW",
        body={"values": [[datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")]]}).execute()
    fetch_rows(force=True)
    _ = d
    return "undone"


# ------------------------------------------------------------------ writes
def _col_index(header, name):
    try:
        return header.index(name)
    except ValueError:
        raise RuntimeError(f"column {name!r} not found")


def _verify_row(data, row: int, acct: str):
    for d in data["rows"]:
        if d["_row"] == row:
            if str(d.get("Account", "")).strip() != acct.strip():
                raise ValueError("row moved; refresh and try again")
            return d
    raise ValueError("row not found")


def write_last_service(row: int, acct: str, when: date):
    m = _main()
    data = fetch_rows(force=True)
    d = _verify_row(data, row, acct)
    idx = _col_index(data["header"], H_COL)
    letter = m.col_letter(idx)
    if data["header"][idx] in MAX_SKIP_COLS or letter in ("I", "K"):
        raise RuntimeError("refusing to write a formula column")
    append_undo(m.get_sheets(), m, row, d, "serviced", old_h=norm_date(d.get(H_COL)), new_h=when.isoformat())
    m.get_sheets().spreadsheets().values().update(
        spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(m.CUSTOMERS_TAB)}!{letter}{row}",
        valueInputOption="USER_ENTERED", body={"values": [[when.isoformat()]]}).execute()
    fetch_rows(force=True)


def write_push(row: int, acct: str, today: date):
    m = _main()
    data = fetch_rows(force=True)
    d = _verify_row(data, row, acct)
    v = view_row(d, today)
    base = v["eff"] or (today.year, today.month)
    target = next_month(base)
    idx = _col_index(data["header"], OLM_COL)
    letter = m.col_letter(idx)
    if data["header"][idx] in MAX_SKIP_COLS or letter in ("H", "I", "K"):
        raise RuntimeError("refusing to write that column")
    m.get_sheets().spreadsheets().values().update(
        spreadsheetId=m.SHEET_ID, range=f"{m.quote_tab(m.CUSTOMERS_TAB)}!{letter}{row}",
        valueInputOption="RAW", body={"values": [[f"{target[0]:04d}-{target[1]:02d}"]]}).execute()
    fetch_rows(force=True)
    return target


# ------------------------------------------------------------------ HTML
CSS = """
:root{--g:#1f5132;--g2:#2e7d4f;--lg:#eaf4ec;--red:#c8332b;--ink:#1d2a22;--mut:#5f6f65;--bd:#d9e4dc}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:#f5f8f5}
header{background:var(--g);color:#fff;padding:10px 16px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:5;box-shadow:0 2px 6px rgba(0,0,0,.15)}
header img{width:36px;height:36px;border-radius:9px}header .t{font-weight:700;font-size:18px;letter-spacing:.2px}header .s{opacity:.8;font-size:13px}
header button{background:transparent;border:1px solid rgba(255,255,255,.5);color:#fff;border-radius:8px;padding:6px 10px;font-size:13px}
main{max-width:980px;margin:0 auto;padding:14px 12px 60px}
.card{background:#fff;border:1px solid var(--bd);border-radius:14px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.narrow{max-width:380px;margin:9vh auto}
.login h1{font-size:22px;margin:10px 0 4px}.login p{color:var(--mut);margin:0 0 18px}
.login .logo{display:flex;justify-content:center}.login .logo img{width:84px;height:84px;border-radius:20px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px;color:var(--mut)}
input[type=text],input[type=password],input[type=date],input[type=search]{width:100%;padding:11px 12px;font-size:16px;border:1px solid var(--bd);border-radius:10px;background:#fff}
.btn{display:inline-block;border:0;border-radius:10px;padding:11px 14px;font-size:15px;font-weight:600;cursor:pointer;background:var(--g2);color:#fff}
.btn.full{width:100%;margin-top:18px}.btn.ghost{background:#fff;color:var(--g);border:1px solid var(--g2)}.btn.sm{padding:8px 11px;font-size:14px}
.err{background:#fdecea;color:#8a1f17;border-radius:10px;padding:10px 12px;margin-top:12px;font-size:14px}
details.edit{width:100%}details.edit summary{list-style:none;display:inline-block}details.edit summary::-webkit-details-marker{display:none}
details.edit form{margin-top:8px}details.edit input{margin-bottom:6px}
.ok{background:var(--lg);color:var(--g);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:14px}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}.bar input{flex:1;min-width:200px}
.sum{color:var(--mut);font-size:13px}
details.month{margin:14px 0}details.month>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;font-weight:700;font-size:17px;padding:8px 4px;border-bottom:2px solid var(--g2)}
details.month>summary::-webkit-details-marker{display:none}.count{background:var(--lg);color:var(--g);border-radius:20px;padding:1px 9px;font-size:13px}
.od-h{color:var(--red)}
.cust{background:#fff;border:1px solid var(--bd);border-left:5px solid var(--g2);border-radius:12px;padding:12px 14px;margin:10px 0}
.cust.overdue{border-left-color:var(--red);background:#fffafa}
.top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.nm{font-weight:700;font-size:16px}
.addr{color:var(--mut);font-size:14px}.addr a{color:inherit}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.tag{font-size:12px;border-radius:20px;padding:2px 9px;background:#eef2ef;color:#384a3f;white-space:nowrap}
.tag.red{background:#fdecea;color:#8a1f17;font-weight:700}.tag.grn{background:var(--lg);color:var(--g)}.tag.amb{background:#fff4dc;color:#7a5200}.tag.blu{background:#e7f0fb;color:#1d4f8a}
.meta{font-size:13px;color:var(--mut)}.pref{font-size:13px;margin-top:4px}.pref b{color:var(--g)}
details.notes{font-size:13px;color:var(--mut);margin-top:6px}details.notes summary{cursor:pointer}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}.acts form{display:flex;gap:6px;align-items:center;margin:0}
.acts input[type=date]{width:auto;padding:7px 8px;font-size:15px}
footer{color:var(--mut);font-size:12px;text-align:center;margin-top:30px}
.tt{margin-left:auto;display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;padding:0!important;border-radius:50%!important;cursor:pointer}
header form{margin-left:8px}.tt svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tt .i-sun{display:none}html[data-theme=dark] .tt .i-sun{display:block}html[data-theme=dark] .tt .i-moon{display:none}
.tt:focus-visible{outline:2px solid #fff;outline-offset:2px}
html[data-theme=dark]{color-scheme:dark;--g:#14301f;--g2:#3a9a62;--lg:#1d3326;--ink:#e3ece6;--mut:#9fb1a6;--bd:#2c3b32}
html[data-theme=dark] body{background:#0f1512}
html[data-theme=dark] .card,html[data-theme=dark] .cust,html[data-theme=dark] input,html[data-theme=dark] .btn.ghost{background:#17201b;color:var(--ink)}
html[data-theme=dark] .btn.ghost{color:#8fd3a8}html[data-theme=dark] .cust.overdue{background:#24171a}
html[data-theme=dark] .tag{background:#24302a;color:#c9d6ce}html[data-theme=dark] .tag.red,html[data-theme=dark] .err{background:#3b1d1b;color:#f3b4ae}
html[data-theme=dark] .tag.grn,html[data-theme=dark] .ok,html[data-theme=dark] .count{color:#8fd3a8}html[data-theme=dark] .tag.amb{background:#3a2e12;color:#f0cf8a}
html[data-theme=dark] .tag.blu{background:#16263a;color:#9cc3ef}html[data-theme=dark] .pref b{color:#8fd3a8}html[data-theme=dark] .od-h{color:#f08a82}
@media(max-width:560px){.top{flex-direction:column}.acts{flex-direction:column;align-items:stretch}.acts form{width:100%}.acts form .btn{flex:1}.acts input[type=date]{flex:1}}
"""


THEME_INIT = ("<script>(function(){var t;try{t=localStorage.getItem('pestpod-theme')}catch(x){}"
              "if(t!=='dark'&&t!=='light')t=window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';"
              "document.documentElement.setAttribute('data-theme',t)})()</script>")

THEME_TOGGLE = ("<button type=button class=tt id=theme-toggle aria-label='Toggle dark mode' title='Toggle dark mode'>"
                "<svg class=i-moon viewBox='0 0 24 24' aria-hidden=true><path d='M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z'/></svg>"
                "<svg class=i-sun viewBox='0 0 24 24' aria-hidden=true><circle cx=12 cy=12 r=4.5/>"
                "<path d='M12 1.5v2.5M12 20v2.5M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M1.5 12H4M20 12h2.5M4.6 19.4l1.8-1.8M17.6 6.4l1.8-1.8'/></svg></button>")

THEME_SCRIPT = ("<script>(function(){var b=document.getElementById('theme-toggle');if(!b)return;var d=document.documentElement;"
                "function sync(){var k=d.getAttribute('data-theme')==='dark';b.setAttribute('aria-pressed',k?'true':'false');"
                "var m=document.querySelector('meta[name=theme-color]');if(m)m.setAttribute('content',k?'#14301f':'#1f5132')}"
                "sync();b.addEventListener('click',function(){var t=d.getAttribute('data-theme')==='dark'?'light':'dark';"
                "d.setAttribute('data-theme',t);try{localStorage.setItem('pestpod-theme',t)}catch(x){}sync()})})()</script>")


def e(s):
    return html.escape(str(s or ""), quote=True)


def _page(title, body, header=""):
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content='noindex,nofollow'><meta name=theme-color content='#1f5132'>"
            f"<link rel=icon href='/dashboard/static/pestpod-logo.png'>"
            f"<link rel=apple-touch-icon href='/dashboard/static/apple-touch-icon.png'>"
            f"<title>{e(title)} · PestPod</title>{THEME_INIT}<style>{CSS}</style></head><body>{header}<main>{body}</main>{THEME_SCRIPT}</body></html>")


def _login_page(csrf, error=""):
    err = f"<div class=err role=alert>{e(error)}</div>" if error else ""
    body = f"""<div class="card narrow login"><div class=logo><img src="/dashboard/static/pestpod-logo.png" alt="PestPod"></div>
<h1 style="text-align:center">PestPod Route Dashboard</h1><p style="text-align:center">Sign in to see your stops.</p>
<form method=post action="/dashboard/login" autocomplete=on><input type=hidden name=csrf value="{e(csrf)}">
<label for=u>Username</label><input id=u name=username type=text autocapitalize=none autocomplete=username required>
<label for=p>Password</label><input id=p name=password type=password autocomplete=current-password required>
<button class="btn full" type=submit>Sign in</button>{err}</form></div>
<footer>PestPod · pestpod.ai</footer>"""
    hdr = (f"<header><img src=\"/dashboard/static/pestpod-logo.png\" alt=\"\"><div><div class=t>PestPod</div>"
           f"<div class=s>Route dashboard</div></div>{THEME_TOGGLE}</header>")
    return _page("Sign in", body, hdr)


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def _fmt(d):
    return d.strftime("%b %-d, %Y") if d else "—"


def _undo_form(v, csrf, label="Undo"):
    hidden = (f"<input type=hidden name=csrf value='{e(csrf)}'><input type=hidden name=row value='{v['row']}'>"
              f"<input type=hidden name=acct value='{e(v['acct'])}'>")
    return (f"<form method=post action=\"/dashboard/undo\" onsubmit=\"return confirm('Undo the last change for "
            f"{e(v['name'])}?')\">{hidden}<button class=\"btn sm ghost\" type=submit>{e(label)}</button></form>")


def _card(v, csrf, today):
    tags = []
    if v["overdue"]:
        tags.append("<span class='tag red'>Overdue</span>")
    if v["pushed"]:
        tags.append("<span class='tag amb'>Pushed</span>")
    if v["anytime"]:
        tags.append("<span class='tag blu'>Anytime stop</span>")
    tags.append(f"<span class='tag {'grn' if v['has_phone'] else ''}'>{'Phone ✓' if v['has_phone'] else 'No phone'}</span>")
    if v["service"]:
        tags.append(f"<span class=tag>{e(v['service'])}</span>")
    if v["freq"]:
        tags.append(f"<span class=tag>{e(v['freq'])}</span>")
    addr = ", ".join(x for x in (v["address"], v["city"]) if x)
    maps = f"https://maps.google.com/?q={html.escape(addr.replace(' ', '+'))}" if addr else ""
    addr_html = f"<a href='{maps}' target=_blank rel=noopener>{e(addr)}</a>" if addr else "No address on file"
    prefs = "".join(f"<div class=pref><b>⏰</b> {e(p)}</div>" for p in v["prefs"])
    notes = f"<details class=notes><summary>Notes</summary>{e(v['notes'])}</details>" if v["notes"] else ""
    nd = _fmt(v["next_due"])
    hidden = (f"<input type=hidden name=csrf value='{e(csrf)}'><input type=hidden name=row value='{v['row']}'>"
              f"<input type=hidden name=acct value='{e(v['acct'])}'>")
    search = e(" ".join([v["name"], v["address"], v["city"], v["acct"], v["service"]]).lower())
    extra = (f"<form method=post action=\"/dashboard/cancel\" onsubmit=\"return confirm('Cancel {e(v['name'])}? "
             f"They move to the Cancelled list (nothing is deleted).')\">{hidden}"
             f"<button class=\"btn sm ghost\" type=submit>Cancel</button></form>")
    if v.get("undoable"):
        extra += _undo_form(v, csrf)
    extra += (f"<details class=edit><summary class=\"btn sm ghost\">Edit</summary>"
              f"<form method=post action=\"/dashboard/edit\">{hidden}"
              f"<label>Phone<input type=tel name=phone value=\"{e(v.get('phone', ''))}\" placeholder=\"(504) 555-0123\" inputmode=tel></label>"
              f"<label>Address<input type=text name=address value=\"{e(v['address'])}\" maxlength=200></label>"
              f"<label>City<input type=text name=city value=\"{e(v['city'])}\" maxlength=80></label>"
              f"<button class=\"btn sm\" type=submit>Save</button></form>"
              f"<div class=meta>Changing the phone does not opt them in to texts.</div></details>")
    return f"""<div class="cust{' overdue' if v['overdue'] else ''}" data-s="{search}">
<div class=top><div><div class=nm>{e(v['name'])}</div><div class=addr>{addr_html}</div></div>
<div class=meta>Acct {e(v['acct'])}</div></div>
<div class=tags>{''.join(tags)}</div>
<div class=meta>Last service: <b>{_fmt(v['last'])}</b> · Next due: {nd}</div>{prefs}{notes}
<div class=acts>
<form method=post action="/dashboard/serviced" onsubmit="return confirm('Mark {e(v['name'])} as serviced on '+this.when.value+'?')">{hidden}
<input type=date name=when value="{today.isoformat()}" max="{today.isoformat()}" required aria-label="Service date">
<button class="btn sm" type=submit>Mark as serviced</button></form>
<form method=post action="/dashboard/push" onsubmit="return confirm('Push {e(v['name'])} to next month?')">{hidden}
<button class="btn sm ghost" type=submit>Push to next month</button></form>{extra}</div></div>"""


def _cancelled_card(v, csrf):
    hidden = (f"<input type=hidden name=csrf value='{e(csrf)}'><input type=hidden name=row value='{v['row']}'>"
              f"<input type=hidden name=acct value='{e(v['acct'])}'>")
    undo = _undo_form(v, csrf) if v.get("undoable") else ""
    on = f" on {e(v['cancelled_on'])}" if v.get("cancelled_on") else ""
    return f"""<div class=cust data-s="{e((v['name'] + ' ' + v['address'] + ' ' + v['acct']).lower())}">
<div class=top><div><div class=nm>{e(v['name'])}</div><div class=addr>{e(v['address'])}</div></div>
<div class=meta>Acct {e(v['acct'])}</div></div><div class=meta>Cancelled{on}</div>
<div class=acts><form method=post action="/dashboard/reactivate" onsubmit="return confirm('Reactivate {e(v['name'])}?')">{hidden}
<button class="btn sm" type=submit>Reactivate</button></form>{undo}</div></div>"""


def render_dashboard(views, csrf, today, flash="", show_hidden=False, flash_undo=None):
    cancelled = [v for v in views if v.get("cancelled")]
    views = [v for v in views if not v.get("cancelled")]
    groups = {}
    for v in views:
        groups.setdefault(v["eff"], []).append(v)
    cur = (today.year, today.month)
    keys = sorted([k for k in groups if k], key=lambda k: k) + ([None] if None in groups else [])
    parts = []
    od = sum(1 for v in views if v["overdue"])
    for k in keys:
        lst = sorted(groups[k], key=lambda v: (not v["overdue"], v["city"].lower(), v["name"].lower()))
        if k:
            label = f"{MONTHS[k[1]-1]} {k[0]}"
            if k < cur:
                label += " · overdue"
            elif k == cur:
                label += " · this month"
        else:
            label = "No due date (no cycle or no last service)"
        is_open = " open" if (k is None or k <= next_month(cur)) else ""
        cls = " class=od-h" if k and k < cur else ""
        parts.append(f"<details class=month{is_open}><summary><span{cls}>{e(label)}</span><span class=count>{len(lst)}</span></summary>"
                     + "".join(_card(v, csrf, today) for v in lst) + "</details>")
    if cancelled:
        parts.append(f"<details class=month><summary><span>Cancelled</span><span class=count>{len(cancelled)}</span></summary>"
                     + "".join(_cancelled_card(v, csrf) for v in sorted(cancelled, key=lambda v: v["name"].lower()))
                     + "</details>")
    fu = f" {_undo_form(flash_undo, csrf)}" if flash and flash_undo else ""
    fl = f"<div class=ok role=status>{e(flash)}{fu}</div>" if flash else ""
    hdr = f"""<header><img src="/dashboard/static/pestpod-logo.png" alt=""><div><div class=t>PestPod</div><div class=s>Route dashboard</div></div>{THEME_TOGGLE}
<form method=post action="/dashboard/logout"><input type=hidden name=csrf value="{e(csrf)}"><button type=submit>Sign out</button></form></header>"""
    body = f"""{fl}<div class=bar><input type=search id=q placeholder="Search name, street, city, account…" aria-label=Search>
<a class="btn sm ghost" href="/dashboard">Refresh</a></div>
<div class=sum>{len(views)} customers · {od} overdue · updated {datetime.now(TZ).strftime('%b %-d, %-I:%M %p')} CT</div>
{''.join(parts)}<footer>Mark as serviced writes only Last Service. Next Due recalculates in the sheet. No texts are sent from here.</footer>
<script>document.getElementById('q').addEventListener('input',function(){{var q=this.value.toLowerCase().trim();
document.querySelectorAll('.cust').forEach(function(c){{c.style.display=!q||c.dataset.s.indexOf(q)>=0?'':'none'}});
if(q)document.querySelectorAll('details.month').forEach(function(d){{d.open=true}});}});</script>"""
    return _page("Dashboard", body, hdr)


# ------------------------------------------------------------------ routes
def _session(request: Request):
    return read_session(request.cookies.get(COOKIE))


def _set_cookie(resp, request, name, value, max_age):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=_secure(request),
                    samesite="lax", path="/dashboard")


@router.get("/dashboard/static/{name}")
def static(name: str):
    if name not in ("pestpod-logo.png", "apple-touch-icon.png"):
        return Response(status_code=404)
    import dashboard_assets as a
    data = base64.b64decode(a.LOGO_PNG if name == "pestpod-logo.png" else a.TOUCH_PNG)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/dashboard/login")
def login_form(request: Request):
    if not _password():
        return _disabled()
    if _session(request):
        return RedirectResponse("/dashboard", status_code=303)
    tok = secrets.token_urlsafe(24)
    resp = _headers(HTMLResponse(_login_page(tok)))
    _set_cookie(resp, request, LOGIN_CSRF_COOKIE, tok, 3600)
    return resp


@router.post("/dashboard/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    if not _password():
        return _disabled()
    ip = _client_ip(request)
    cookie_tok = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    new_tok = secrets.token_urlsafe(24)

    def fail(msg, code=401):
        resp = _headers(HTMLResponse(_login_page(new_tok, msg), status_code=code))
        _set_cookie(resp, request, LOGIN_CSRF_COOKIE, new_tok, 3600)
        return resp

    if not cookie_tok or not hmac.compare_digest(cookie_tok, csrf):
        return fail("Your sign-in form expired. Please try again.", 403)
    if _throttled(ip):
        return fail("Too many attempts. Wait 15 minutes and try again.", 429)
    ok_u = hmac.compare_digest(username.strip().lower().encode(), _user().lower().encode())
    ok_p = hmac.compare_digest(password.encode(), _password().encode())
    if not (ok_u and ok_p):
        _record_fail(ip)
        time.sleep(0.5)
        log.warning("dashboard login failed from %s", ip)
        return fail("Wrong username or password.")
    resp = RedirectResponse("/dashboard", status_code=303)
    _set_cookie(resp, request, COOKIE, make_session(_user()), SESSION_TTL)
    resp.delete_cookie(LOGIN_CSRF_COOKIE, path="/dashboard")
    log.info("dashboard login ok from %s", ip)
    return _headers(resp)


@router.post("/dashboard/logout")
def logout(request: Request, csrf: str = Form("")):
    s = _session(request)
    resp = RedirectResponse("/dashboard/login", status_code=303)
    if s and hmac.compare_digest(csrf, csrf_for(s)):
        resp.delete_cookie(COOKIE, path="/dashboard")
    return resp


@router.get("/dashboard")
@router.get("/dashboard/")
def dashboard(request: Request, msg: str = "", test: int = 0, row: int = 0):
    if not _password():
        return _disabled()
    s = _session(request)
    if not s:
        return RedirectResponse("/dashboard/login", status_code=303)
    today = datetime.now(TZ).date()
    try:
        data = fetch_rows()
    except Exception as ex:
        log.warning("dashboard read failed: %s", type(ex).__name__)
        return _headers(HTMLResponse(_page("Error", "<div class='card narrow'><h1>Can't reach the sheet</h1>"
                                           "<p>Try again in a minute.</p><a class=btn href='/dashboard'>Retry</a></div>"), 502))
    views = []
    for d in data["rows"]:
        if (test and str(d.get("Account", "")).upper().startswith("TEST")) or not is_hidden_row(d):
            v = view_row(d, today)
            v["cancelled"] = is_cancelled(d)
            v["cancelled_on"] = norm_date(d.get(CANCELLED_ON_COL)) if v["cancelled"] else ""
            v["undoable"] = undo_candidate(data, d["_row"])[1]
            views.append(v)
    flash = {"serviced": "Saved. Last Service updated; Next Due recalculates in the sheet.",
             "pushed": "Pushed to next month.", "cancelled": "Cancelled. They're in the Cancelled list at the bottom.",
             "reactivated": "Reactivated. They're back on the route list.",
             "undone": "Undone. The previous value is back.",
             "changed": "That was already changed since, so it can't be undone from here.",
             "noundo": "Nothing to undo for that customer.",
             "edited": "Saved. Phone/address updated (this does not opt anyone in to texts).",
             "nochange": "Nothing changed.",
             "badphone": "That phone number doesn't look right. Use a 10-digit US number, like (504) 555-0123.",
             "err": "That didn't save. Refresh and try again."}.get(msg, "")
    fu = None
    if msg in ("serviced", "cancelled", "reactivated", "edited") and row:
        fu = next((v for v in views if v["row"] == row and v["undoable"]), None)
    return _headers(HTMLResponse(render_dashboard(views, csrf_for(s), today, flash, flash_undo=fu)))


def _check_post(request: Request, csrf: str):
    s = _session(request)
    if not s:
        return None, RedirectResponse("/dashboard/login", status_code=303)
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('host')}":
        return None, Response("Bad origin", status_code=403)
    if not csrf or not hmac.compare_digest(csrf, csrf_for(s)):
        return None, Response("Bad CSRF token", status_code=403)
    return s, None


@router.post("/dashboard/serviced")
def serviced(request: Request, row: int = Form(...), acct: str = Form(...), when: str = Form(...),
             csrf: str = Form("")):
    if not _password():
        return _disabled()
    _, bad = _check_post(request, csrf)
    if bad:
        return bad
    today = datetime.now(TZ).date()
    try:
        d = datetime.strptime(when, "%Y-%m-%d").date()
    except ValueError:
        return Response("Bad date", status_code=400)
    if d > today or d < today - timedelta(days=400):
        return Response("Date out of range", status_code=400)
    if row < 2:
        return Response("Bad row", status_code=400)
    try:
        write_last_service(row, acct, d)
        log.info("dashboard: row %s (%s) Last Service -> %s", row, acct, d)
        return RedirectResponse(f"/dashboard?msg=serviced&row={row}", status_code=303)
    except Exception as ex:
        log.warning("dashboard serviced failed: %s", ex)
        return RedirectResponse("/dashboard?msg=err", status_code=303)


@router.post("/dashboard/push")
def push(request: Request, row: int = Form(...), acct: str = Form(...), csrf: str = Form("")):
    if not _password():
        return _disabled()
    _, bad = _check_post(request, csrf)
    if bad:
        return bad
    if row < 2:
        return Response("Bad row", status_code=400)
    try:
        t = write_push(row, acct, datetime.now(TZ).date())
        log.info("dashboard: row %s (%s) Office List Month -> %04d-%02d", row, acct, *t)
        return RedirectResponse("/dashboard?msg=pushed", status_code=303)
    except Exception as ex:
        log.warning("dashboard push failed: %s", ex)
        return RedirectResponse("/dashboard?msg=err", status_code=303)


def _simple_post(request, csrf, row, fn):
    if not _password():
        return _disabled()
    _, bad = _check_post(request, csrf)
    if bad:
        return bad
    if row < 2:
        return Response("Bad row", status_code=400)
    try:
        return RedirectResponse(fn(), status_code=303)
    except Exception as ex:
        log.warning("dashboard write failed: %s", ex)
        return RedirectResponse("/dashboard?msg=err", status_code=303)


@router.post("/dashboard/cancel")
def cancel(request: Request, row: int = Form(...), acct: str = Form(...), csrf: str = Form("")):
    def go():
        write_status(row, acct, True, datetime.now(TZ).date())
        log.info("dashboard: row %s (%s) Status -> Cancelled", row, acct)
        return f"/dashboard?msg=cancelled&row={row}"
    return _simple_post(request, csrf, row, go)


@router.post("/dashboard/reactivate")
def reactivate(request: Request, row: int = Form(...), acct: str = Form(...), csrf: str = Form("")):
    def go():
        write_status(row, acct, False, datetime.now(TZ).date())
        log.info("dashboard: row %s (%s) Status -> Active", row, acct)
        return f"/dashboard?msg=reactivated&row={row}"
    return _simple_post(request, csrf, row, go)


@router.post("/dashboard/undo")
def undo(request: Request, row: int = Form(...), acct: str = Form(...), csrf: str = Form("")):
    def go():
        r = undo_last(row, acct)
        log.info("dashboard: row %s (%s) undo -> %s", row, acct, r)
        return "/dashboard?msg=" + {"undone": "undone", "changed": "changed"}.get(r, "noundo")
    return _simple_post(request, csrf, row, go)


@router.post("/dashboard/edit")
def edit(request: Request, row: int = Form(...), acct: str = Form(...), phone: str = Form(""),
         address: str = Form(""), city: str = Form(""), csrf: str = Form("")):
    def go():
        try:
            r = write_edit(row, acct, phone, address, city)
        except ValueError as ex:
            if str(ex) == "bad phone":
                return "/dashboard?msg=badphone"
            raise
        log.info("dashboard: row %s (%s) edit -> %s", row, acct, r)
        return f"/dashboard?msg=edited&row={row}" if r == "saved" else "/dashboard?msg=nochange"
    return _simple_post(request, csrf, row, go)
