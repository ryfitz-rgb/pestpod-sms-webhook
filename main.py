"""
PestPod SMS Webhook: FastAPI app that receives Twilio inbound SMS, replies with
approved templates, and persists replies to the "PestPod Reminders v1" Google Sheet.

Env vars:
  GOOGLE_SA_JSON_B64  base64 of a Google service account JSON key (spreadsheets scope).
                      If missing/invalid, the app falls back to TEST_PHONE-only matching.
  SHEET_ID            optional, defaults to the PestPod Reminders v1 sheet.
  TEST_PHONE          optional fallback match when Sheets is unavailable.
"""
import base64
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pestpod")



@asynccontextmanager
async def lifespan(_app):
    if not _auth_token():
        log.warning(UNSIGNED_WARNING)
    # Warm the Sheets client + Customers cache without blocking startup, so the first
    # reply after a cold start usually skips the Sheets round-trip.
    threading.Thread(target=warm_cache, name="warm-customers", daemon=True).start()
    yield


app = FastAPI(title="PestPod SMS Webhook", lifespan=lifespan)

# Fallback test match; number comes from the TEST_PHONE env var (kept out of the repo).
TEST_PHONE = os.environ.get("TEST_PHONE", "")
SHEET_ID = os.environ.get("SHEET_ID") or "1k2DgUsGiEk6VaTG1lDD554o9BqvWWJ7pOg7I4IrW79M"
CUSTOMERS_TAB = "Customers"
INBOUND_TAB = "Inbound Log"
INBOUND_HEADERS = ["Timestamp", "Phone", "Body", "MessageSid", "Matched Row"]
OPTINS_TAB = "Opt-Ins"
OPTINS_HEADERS = ["Timestamp", "Phone", "Keyword", "MessageSid", "Matched Row"]
TZ = ZoneInfo("America/Chicago")
CACHE_TTL = 60  # seconds: Customers data younger than this is used as-is
CACHE_MAX_STALE = 15 * 60  # seconds: older (but not this old) is used, then refreshed in background
HTTP_TIMEOUT = 5  # seconds per Sheets call; Twilio gives us 15s total

PHONE_HEADERS = ("phone", "phone (+1xxxxxxxxxx)")
NAME_HEADERS = ("customer name", "name")


def normalize_phone(raw) -> str:
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw


# ---------------------------------------------------------------- Sheets client
_service = None
_service_lock = threading.Lock()
_service_failed = False


def get_sheets():
    """Return a Sheets API service, or None if creds are missing/invalid. Never raises."""
    global _service, _service_failed
    if _service is not None or _service_failed:
        return _service
    with _service_lock:
        if _service is not None or _service_failed:
            return _service
        b64 = os.environ.get("GOOGLE_SA_JSON_B64", "").strip()
        if not b64:
            log.warning("GOOGLE_SA_JSON_B64 not set; Sheets disabled, using TEST_PHONE fallback")
            _service_failed = True
            return None
        try:
            import httplib2
            from google.oauth2 import service_account
            from google_auth_httplib2 import AuthorizedHttp
            from googleapiclient.discovery import build

            info = json.loads(base64.b64decode(b64))
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
            _service = build("sheets", "v4", http=http, cache_discovery=False)
            log.info("Sheets enabled as %s", info.get("client_email", "?"))
        except Exception as e:  # never log the key itself
            log.warning("GOOGLE_SA_JSON_B64 invalid (%s); Sheets disabled, using TEST_PHONE fallback",
                        type(e).__name__)
            _service_failed = True
            _service = None
        return _service


def col_letter(idx: int) -> str:
    """0-based column index -> A1 letters."""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def quote_tab(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


_cache = {"at": 0.0, "data": None}
_fetch_lock = threading.Lock()  # one Customers read at a time; waiters reuse its result
_refreshing = threading.Event()  # a background refresh is in flight


def _fetch_customers(svc):
    """Read the Customers tab from Sheets and store it in the cache. Raises on failure."""
    res = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=quote_tab(CUSTOMERS_TAB)
    ).execute()
    values = res.get("values", [])
    header_idx, cols = None, {}
    for i, row in enumerate(values[:20]):
        lowered = [str(c).strip().lower() for c in row]
        if any(h in lowered for h in PHONE_HEADERS):
            header_idx = i
            cols = {h: j for j, h in enumerate(lowered) if h}
            break
    if header_idx is None:
        raise ValueError("Customers header row not found")
    rows = [(header_idx + 2 + k, r) for k, r in enumerate(values[header_idx + 1:])]
    data = (header_idx + 1, cols, rows)
    _cache.update(at=time.time(), data=data)
    return data


def _fresh_cached():
    data = _cache["data"]
    if data is not None and time.time() - _cache["at"] < CACHE_TTL:
        return data
    return None


def _refresh_in_background(svc) -> None:
    """Start one background Customers re-read (no-op if one is already running)."""
    if _refreshing.is_set():
        return
    _refreshing.set()

    def run():
        try:
            with _fetch_lock:
                _fetch_customers(svc)
        except Exception as e:
            log.warning("background Customers refresh failed (%s)", type(e).__name__)
        finally:
            _refreshing.clear()

    threading.Thread(target=run, name="refresh-customers", daemon=True).start()


def read_customers(svc, allow_stale=False):
    """Return (header_row_number, {header_lower: col_idx}, rows_with_rownum).

    Cached for CACHE_TTL seconds. With allow_stale=True (the reply path), data up to
    CACHE_MAX_STALE old is returned immediately and refreshed in a background thread, so
    a reply never waits on Sheets unless the cache is empty or very old.
    """
    data = _fresh_cached()
    if data is not None:
        return data
    data, age = _cache["data"], time.time() - _cache["at"]
    if allow_stale and data is not None and age < CACHE_MAX_STALE:
        _refresh_in_background(svc)
        return data
    with _fetch_lock:
        data = _fresh_cached()  # another thread may have just refreshed it
        if data is not None:
            return data
        return _fetch_customers(svc)


def _after_write(svc) -> None:
    """After a Customers write (in background), re-read so the next reply sees it. Never raises."""
    _cache["data"] = None
    try:
        with _fetch_lock:
            _fetch_customers(svc)
    except Exception as e:
        log.warning("Customers re-read after write failed (%s)", type(e).__name__)


def warm_cache() -> None:
    """Build the Sheets client and load Customers. Never raises."""
    try:
        svc = get_sheets()
        if svc is not None:
            read_customers(svc)
    except Exception as e:
        log.warning("cache warm-up failed (%s)", type(e).__name__)


def _find_col(cols: dict, *names):
    for n in names:
        if n in cols:
            return cols[n]
    return None


def _test_customer(phone: str):
    if TEST_PHONE and normalize_phone(phone) == normalize_phone(TEST_PHONE):
        return {"row": None, "name": "Ryan", "proposed_appointment": "Tue 10/27 at 9 AM",
                "opted_in": True, "fields": {}}
    return None


def find_customer_by_phone(phone: str):
    target = normalize_phone(phone)
    svc = get_sheets()
    if svc is None:
        return _test_customer(phone)
    try:
        _, cols, rows = read_customers(svc, allow_stale=True)
    except Exception as e:
        log.warning("Customers read failed (%s); using TEST_PHONE fallback", type(e).__name__)
        return _test_customer(phone)
    pcol = _find_col(cols, *PHONE_HEADERS)
    ncol = _find_col(cols, *NAME_HEADERS)
    acol = _find_col(cols, "proposed appointment")
    headers = {j: h for h, j in cols.items()}
    for rownum, row in rows:
        if pcol < len(row) and target and normalize_phone(row[pcol]) == target:
            def cell(j):
                return str(row[j]).strip() if j is not None and j < len(row) else ""
            fields = {headers[j]: row[j] for j in range(len(row)) if j in headers}
            return {
                "row": rownum,
                "name": cell(ncol),
                "proposed_appointment": cell(acol),
                "opted_in": (cell(_find_col(cols, "opt-in status")).lower() == "opted in"
                             or cell(_find_col(cols, "ok to text?")).upper() == "TRUE"),
                "fields": fields,
            }
    return None


def update_customer_reply(row, reply: str) -> None:
    """Set Last Reply (and Reminder Status if present) for a sheet row. Never raises."""
    print(f"[UPDATE] row {row} -> Last Reply = {reply}")
    if not row:
        return
    svc = get_sheets()
    if svc is None:
        return
    try:
        _, cols, _ = read_customers(svc)
    except Exception as e:
        log.warning("update_customer_reply read failed (%s)", type(e).__name__)
        return
    lr = _find_col(cols, "last reply")
    if lr is None:
        log.warning("No 'Last Reply' column found")
        return
    last_reply = {"range": f"{quote_tab(CUSTOMERS_TAB)}!{col_letter(lr)}{row}", "values": [[reply]]}
    data = [last_reply]
    rs = _find_col(cols, "reminder status")
    if rs is not None:
        # Must match the column's ONE_OF_LIST validation exactly.
        status = "Confirmed Y" if reply == "Y" else "Reschedule N"
        data.append({"range": f"{quote_tab(CUSTOMERS_TAB)}!{col_letter(rs)}{row}", "values": [[status]]})

    def write(d):
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "RAW", "data": d}
        ).execute()

    try:
        write(data)
    except Exception as e:
        log.warning("combined reply write failed (%s); retrying Last Reply alone", type(e).__name__)
        try:
            write([last_reply])
        except Exception as e2:
            log.warning("Last Reply write failed (%s)", type(e2).__name__)
    _after_write(svc)  # refresh so the next reply sees it


_inbound_tab_ready = False


def _ensure_inbound_tab(svc) -> None:
    global _inbound_tab_ready
    if _inbound_tab_ready:
        return
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID, fields="sheets.properties.title").execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if INBOUND_TAB not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": INBOUND_TAB}}}]},
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{quote_tab(INBOUND_TAB)}!A1",
            valueInputOption="RAW", body={"values": [INBOUND_HEADERS]},
        ).execute()
    _inbound_tab_ready = True


def log_inbound(phone: str, body: str, message_sid: str, timestamp=None, matched_row=None) -> None:
    """Append an inbound message to the Inbound Log tab. Never raises."""
    ts = (timestamp or datetime.now(TZ)).astimezone(TZ).isoformat(timespec="seconds")
    print(f"[INBOUND] {ts} | {phone} | {body} | SID={message_sid} | row={matched_row}")
    svc = get_sheets()
    if svc is None:
        return
    try:
        _ensure_inbound_tab(svc)
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{quote_tab(INBOUND_TAB)}!A:E",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[ts, phone, body, message_sid, matched_row or ""]]},
        ).execute()
    except Exception as e:
        log.warning("log_inbound failed (%s)", type(e).__name__)


# ---------------------------------------------------------------- text-to-join
JOIN_KEYWORDS = {"JOIN", "REMIND"}
JOIN_METHOD = "Keyword JOIN"  # must be in the Opt-In Method dropdown on the Customers tab
# Y/YES is a confirm from an opted-in customer, and a text-to-join from anyone else
# (the printed kit and site say "Text YES"; Y works too). Reuses JOIN_METHOD in the sheet.
YES_WORDS = {"Y", "YES"}
# YES is also a Twilio Advanced Opt-Out opt-in keyword on the Messaging Service, so Twilio
# already auto-replies "You're subscribed..." to it. Record the opt-in but send nothing
# ourselves for YES, so a new signup doesn't get two welcome texts. Y is not a Twilio keyword.
TWILIO_OPTIN_WORDS = {"YES"}
# Mirrors the opt-in message registered on the A2P campaign, with the "PestPod:" prefix.
JOIN_REPLY = (
    "PestPod: You're opted in to appointment reminders and confirmations. Up to 4 msgs per visit. "
    "Msg & data rates may apply. Reply HELP for help, STOP to cancel."
)


def mark_customer_opted_in(row, today=None) -> bool:
    """Set OK to Text?/Opt-In Status/Method/Date on a Customers row. Never raises."""
    print(f"[OPT-IN] row {row} -> Opted in via {JOIN_METHOD}")
    if not row:
        return False
    svc = get_sheets()
    if svc is None:
        return False
    try:
        _, cols, _ = read_customers(svc)
    except Exception as e:
        log.warning("mark_customer_opted_in read failed (%s)", type(e).__name__)
        return False
    day = (today or datetime.now(TZ).date()).isoformat()
    wanted = [("ok to text?", True), ("opt-in status", "Opted in"),
              ("opt-in method", JOIN_METHOD), ("opt-in date", day)]
    data = []
    for header, value in wanted:
        j = _find_col(cols, header)
        if j is not None:
            data.append({"range": f"{quote_tab(CUSTOMERS_TAB)}!{col_letter(j)}{row}", "values": [[value]]})
    if not data:
        return False
    try:
        # USER_ENTERED so TRUE stays a checkbox boolean and the date stays a real date.
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID, body={"valueInputOption": "USER_ENTERED", "data": data}
        ).execute()
    except Exception as e:
        log.warning("opt-in write failed (%s)", type(e).__name__)
        return False
    finally:
        _after_write(svc)
    return True


_optins_tab_ready = False


def _ensure_optins_tab(svc) -> None:
    global _optins_tab_ready
    if _optins_tab_ready:
        return
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID, fields="sheets.properties.title").execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if OPTINS_TAB not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": OPTINS_TAB}}}]},
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{quote_tab(OPTINS_TAB)}!A1",
            valueInputOption="RAW", body={"values": [OPTINS_HEADERS]},
        ).execute()
    _optins_tab_ready = True


def log_optin(phone: str, keyword: str, message_sid: str, matched_row=None, timestamp=None) -> None:
    """Append a keyword opt-in to the Opt-Ins tab. Never raises."""
    ts = (timestamp or datetime.now(TZ)).astimezone(TZ).isoformat(timespec="seconds")
    print(f"[OPT-INS] {ts} | {phone} | {keyword} | SID={message_sid} | row={matched_row}")
    svc = get_sheets()
    if svc is None:
        return
    try:
        _ensure_optins_tab(svc)
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{quote_tab(OPTINS_TAB)}!A:E",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[ts, phone, keyword, message_sid, matched_row or ""]]},
        ).execute()
    except Exception as e:
        log.warning("log_optin failed (%s)", type(e).__name__)


# ---------------------------------------------------------------- replies
# STOP/START/HELP are handled by Twilio Advanced Opt-Out on the Messaging Service.
OPT_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "UNSTOP", "HELP", "INFO"}
DEFAULT_NAME = "Ryan"
DEFAULT_APPT = "Tue 10/27 at 9 AM"


def confirm_reply(name: str, appointment: str) -> str:
    return (
        f"PestPod: Thanks {name}, your pest control service is confirmed for {appointment}. "
        "Reply HELP for help, STOP to opt out."
    )


def reschedule_reply(name: str) -> str:
    return (
        f"PestPod: Got it {name}, we'll reschedule your pest control service. "
        "Reply HELP for help, STOP to opt out."
    )


# ---------------------------------------------------------------- Twilio signature check
UNSIGNED_WARNING = ("SECURITY: TWILIO_AUTH_TOKEN is not set; Twilio signature validation is OFF "
                    "and the SMS webhook accepts unsigned requests. Set it on Render.")


def _auth_token() -> str:
    # Read per request so a Render env change (which restarts the service) or a test patch applies.
    return os.environ.get("TWILIO_AUTH_TOKEN", "").strip()


def signed_url(request: Request) -> str:
    """The URL Twilio signed. Render terminates TLS at its proxy, so request.url says http://."""
    override = os.environ.get("WEBHOOK_PUBLIC_URL", "").strip()
    if override:
        return override
    proto = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip() or "https"
    host = request.headers.get("host") or request.url.netloc
    host = host.split(",")[0].strip()
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += "?" + request.url.query
    return url


async def verify_twilio_signature(request: Request) -> None:
    """Runs before the webhook body: a bad/missing X-Twilio-Signature -> 403, nothing else happens."""
    token = _auth_token()
    if not token:
        log.warning(UNSIGNED_WARNING)
        return
    signature = request.headers.get("x-twilio-signature", "")
    form = await request.form()  # Starlette caches this; the Form() params reuse it
    url = signed_url(request)
    if not signature or not RequestValidator(token).validate(url, form, signature):
        log.warning("rejected SMS webhook request: %s Twilio signature (url=%s)",
                    "bad" if signature else "missing", url)
        raise HTTPException(status_code=403, detail="Forbidden")


def _background(fn, *args, **kwargs) -> None:
    """Run a post-response Sheets write; log failures, never raise."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        log.warning("background %s failed (%s)", getattr(fn, "__name__", "task"), type(e).__name__)


@app.post("/api/v1/sms/webhook", dependencies=[Depends(verify_twilio_signature)])
def sms_webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
):
    # Speed matters: Twilio drops our TwiML reply after 15s. The only Sheets work done before
    # replying is the (cached) Customers lookup; every log/write below runs as a background
    # task after the response has been sent, in the order added.
    def later(fn, *args, **kwargs):
        background_tasks.add_task(_background, fn, *args, **kwargs)

    # Case-insensitive, trimmed, trailing punctuation ignored ("y." / "Yes!" -> Y / YES).
    reply = re.sub(r"[\s.!]+$", "", Body.strip()).upper()
    phone = normalize_phone(From)

    try:
        customer = find_customer_by_phone(phone)
    except Exception as e:
        log.warning("lookup failed (%s)", type(e).__name__)
        customer = None
    row = customer.get("row") if customer else None
    opted_in = bool(customer and customer.get("opted_in"))
    later(log_inbound, phone, Body, MessageSid, datetime.now(TZ), row)

    resp = MessagingResponse()

    # Opt-out/help keywords: Twilio Advanced Opt-Out already replied, so stay silent.
    if reply in OPT_KEYWORDS:
        return PlainTextResponse(str(resp), media_type="application/xml")

    # Text-to-join: JOIN/REMIND always; Y/YES unless the sender is an opted-in customer
    # (then it's an appointment confirm). Strangers never get added to Customers.
    if reply in JOIN_KEYWORDS or (reply in YES_WORDS and not opted_in):
        if row:
            later(mark_customer_opted_in, row, datetime.now(TZ).date())
        else:
            later(log_optin, phone, reply, MessageSid, None, datetime.now(TZ))
        if reply not in TWILIO_OPTIN_WORDS:
            resp.message(JOIN_REPLY)
        return PlainTextResponse(str(resp), media_type="application/xml")

    if customer:
        name = customer.get("name") or DEFAULT_NAME
        appt = customer.get("proposed_appointment") or DEFAULT_APPT
        if reply in YES_WORDS:  # opted in (otherwise handled above)
            resp.message(confirm_reply(name, appt))
            later(update_customer_reply, row, "Y")
        elif reply in ("N", "NO") and opted_in:
            resp.message(reschedule_reply(name))
            later(update_customer_reply, row, "N")
        else:
            # Includes N from a non-opted-in row: help only, nothing marked.
            resp.message("PestPod: Reply Y to confirm or N to reschedule. Reply HELP for help, STOP to opt out.")
    else:
        resp.message("PestPod: No matching appointment found. Reply HELP for help, STOP to opt out.")

    return PlainTextResponse(str(resp), media_type="application/xml")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/version")
async def version():
    # Render sets RENDER_GIT_COMMIT on each deploy.
    return {"commit": os.environ.get("RENDER_GIT_COMMIT", "unknown"),
            "signature_validation": bool(_auth_token())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
