"""Per-customer text conversations and the dashboard reply box.

Everything a customer and Ryan's business number say to each other, in one place:
  - inbound texts (the Twilio webhook),
  - the webhook's automatic replies (TwiML),
  - batch reminder sends (batch.py),
  - manual replies typed into the dashboard reply box.

Storage: the `messages` table in pestpod-db (created by pgsync.SCHEMA). Logging is best-effort and
NEVER breaks the send/reply flow: every failure is caught and logged with the phone masked.
Logging runs whenever DATABASE_URL is set, even with the reply box off, so the history is already
there the day the box is turned on. It sends nothing and touches no Sheet.

The conversation view also folds in the existing Sheet logs ("Inbound Log", "Outbound Log"),
read-only, so texts from before this table existed still show. No Twilio backfill is done.

Unread texts: an inbound text is "unread" until Ryan opens that customer's conversation (or sends
them a reply from the box). Read state lives in the `conversation_reads` table (pgsync.SCHEMA); the
dashboard shows the counts only while REPLY_BOX_ENABLED is on.

Free-text acknowledgement: the webhook claims at most one "Thanks, Ryan will get back to you soon"
auto-reply per phone per Central day through claim_daily_ack (a unique key in `messages`, so two
texts racing each other still get one reply). No database, an error or a slow database = no reply.

Routes (all login-gated with the existing dashboard session; 404 while REPLY_BOX_ENABLED is off):
  GET  /dashboard/conversation?row=N&acct=A   one customer's texts, oldest first, times in CT
  POST /dashboard/conversation/send           send ONE text from Ryan's number (ending 0822)

Safety on send (all enforced server-side): dashboard session + CSRF; opted-out (STOP) customers
blocked; no-consent and Anytime stops blocked; outside 9 AM-8 PM CT needs an explicit confirm;
one send per idempotency key (stored in Postgres, so a double click or a resubmit can't send
twice); only Ryan's Messaging Service and a From number ending 0822 (never Blain's 2533).

Env vars:
  REPLY_BOX_ENABLED   true/1/yes to show the reply box and enable its routes. Off by default.
  REPLY_FROM_NUMBER   Ryan's Twilio number (E.164). Must end in 0822; anything else refuses to send.
  TWILIO_ACCOUNT_SID  plus TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET, or TWILIO_AUTH_TOKEN
                      (same credentials batch.py uses).
  DATABASE_URL        pestpod-db; required to send (idempotency + history), optional for logging.
"""
import hashlib
import logging
import math
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("pestpod.conversations")
router = APIRouter()
TZ = ZoneInfo("America/Chicago")

# Ryan's sender. The Messaging Service is pinned here on purpose (NOT read from
# TWILIO_MESSAGING_SERVICE_SID) so an env change can never point the reply box at another tech's
# service. It is the same "PestPod Appointment Alerts" service batch.py defaults to.
RYAN_MESSAGING_SERVICE_SID = "MG7aa08d66618db7ad78ad6af28b793bf2"
RYAN_LAST4 = "0822"
BLOCKED_SENDER_LAST4 = {"2533"}  # Blain's number: never send from it

DIRECTIONS = ("in", "out")
KINDS = ("inbound", "auto_reply", "batch", "manual")
MAX_BODY = 640  # 5 GSM-7 segments; keeps a typo from becoming a 10-part text
QUIET_START_HOUR, QUIET_END_HOUR = 9, 20  # same window as batch.py

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
START_WORDS = {"START", "UNSTOP", "YES", "Y", "JOIN", "REMIND"}
OPTOUT_ERROR = "21610"  # Twilio: recipient has replied STOP


def enabled() -> bool:
    return os.environ.get("REPLY_BOX_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def mask(phone) -> str:
    """Only the last 4 digits ever leave this module (UI and logs)."""
    d = re.sub(r"\D", "", str(phone or ""))
    return f"***-{d[-4:]}" if len(d) >= 4 else "-"


def norm_phone(raw) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return str(raw or "").strip()


# ------------------------------------------------------------------ SMS segment math
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€\f")


def sms_segments(text: str) -> dict:
    """Characters, encoding and segment count the way carriers bill it.
    GSM-7: 160 in one text, 153 per part when split (extended chars like [ ] { } € count 2).
    Anything outside GSM-7 (emoji, curly quotes) switches the whole text to UCS-2: 70 / 67,
    counted in UTF-16 units (an emoji is 2)."""
    text = text or ""
    if all(ch in GSM7_BASIC or ch in GSM7_EXT for ch in text):
        units = sum(2 if ch in GSM7_EXT else 1 for ch in text)
        enc, single, multi = "GSM-7", 160, 153
    else:
        units = len(text.encode("utf-16-le")) // 2
        enc, single, multi = "UCS-2", 70, 67
    segs = 0 if units == 0 else (1 if units <= single else math.ceil(units / multi))
    return {"chars": len(text), "units": units, "encoding": enc, "segments": segs}


# ------------------------------------------------------------------ storage
class PgStore:
    """messages table in pestpod-db (schema lives in pgsync.SCHEMA)."""
    _ready = False

    def _conn(self):
        import pgsync
        conn = pgsync.connect()
        if not PgStore._ready:
            pgsync.ensure_schema(conn)
            PgStore._ready = True
        return conn

    def add(self, rec: dict):
        """Insert one message. Returns the new id, or None if it is a duplicate (same Twilio SID
        and direction, or same idempotency key)."""
        cols = ["created_at", "phone", "account", "customer_row", "direction", "kind", "body",
                "twilio_sid", "status", "tech", "idempotency_key"]
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO messages ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                    "ON CONFLICT DO NOTHING RETURNING id", [rec.get(c) for c in cols])
                got = cur.fetchone()
            conn.commit()
        return got[0] if got else None

    def finish(self, msg_id, sid, status):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE messages SET twilio_sid=%s, status=%s WHERE id=%s",
                            (sid or None, status, msg_id))
            conn.commit()

    def for_phone(self, phone, limit=500):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, created_at, phone, account, customer_row, direction, kind, body, twilio_sid, "
                    "status, tech FROM messages WHERE phone=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                    (phone, limit))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()][::-1]

    def unread(self, since):
        """{phone: {count, latest, body}} for inbound texts newer than both the last time Ryan
        opened that conversation and his last reply-box send (failed sends don't count)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(UNREAD_SQL, (since,))
                return {r[0]: {"count": int(r[1]), "latest": r[2], "body": r[3] or ""} for r in cur.fetchall()}

    def mark_read(self, phone, at):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO conversation_reads (phone, last_read_at) VALUES (%s, %s) "
                    "ON CONFLICT (phone) DO UPDATE SET last_read_at = "
                    "GREATEST(conversation_reads.last_read_at, EXCLUDED.last_read_at)", (phone, at))
            conn.commit()


UNREAD_SQL = """
SELECT m.phone, count(*), max(m.created_at),
       (array_agg(m.body ORDER BY m.created_at DESC, m.id DESC))[1]
FROM messages m
LEFT JOIN conversation_reads r ON r.phone = m.phone
WHERE m.direction = 'in' AND m.created_at >= %s
  AND m.created_at > COALESCE(r.last_read_at, '-infinity'::timestamptz)
  AND m.created_at > COALESCE((SELECT max(o.created_at) FROM messages o
                               WHERE o.phone = m.phone AND o.direction = 'out' AND o.kind = 'manual'
                                 AND COALESCE(o.status, '') NOT LIKE 'error%%'), '-infinity'::timestamptz)
GROUP BY m.phone
"""


class MemoryStore:
    """Same behaviour as PgStore, in memory (tests and the local screenshot only)."""

    def __init__(self):
        self.rows, self._lock, self._n = [], threading.Lock(), 0
        self.reads = {}

    def add(self, rec):
        with self._lock:
            key, sid = rec.get("idempotency_key"), rec.get("twilio_sid")
            for r in self.rows:
                if key and r.get("idempotency_key") == key:
                    return None
                if sid and r.get("twilio_sid") == sid and r.get("direction") == rec.get("direction"):
                    return None
            self._n += 1
            self.rows.append({**rec, "id": self._n})
            return self._n

    def finish(self, msg_id, sid, status):
        with self._lock:
            for r in self.rows:
                if r["id"] == msg_id:
                    r.update(twilio_sid=sid or None, status=status)

    def for_phone(self, phone, limit=500):
        rows = [dict(r) for r in self.rows if r["phone"] == phone]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        return rows[-limit:]

    def unread(self, since):
        out = {}
        with self._lock:
            rows = sorted(self.rows, key=lambda r: (r["created_at"], r["id"]))
        for phone in {r["phone"] for r in rows}:
            mine = [r for r in rows if r["phone"] == phone]
            cut = [self.reads.get(phone)] + [r["created_at"] for r in mine if r["direction"] == "out"
                                             and r["kind"] == "manual"
                                             and not str(r.get("status") or "").startswith("error")]
            cut = max((c for c in cut if c is not None), default=None)
            new = [r for r in mine if r["direction"] == "in" and r["created_at"] >= since
                   and (cut is None or r["created_at"] > cut)]
            if new:
                out[phone] = {"count": len(new), "latest": new[-1]["created_at"], "body": new[-1]["body"]}
        return out

    def mark_read(self, phone, at):
        with self._lock:
            cur = self.reads.get(phone)
            self.reads[phone] = at if cur is None or at > cur else cur


_store_override = None


def set_store(store) -> None:
    """Tests (and the local screenshot) swap in a MemoryStore."""
    global _store_override
    _store_override = store


def get_store():
    if _store_override is not None:
        return _store_override
    import pgsync
    return PgStore() if pgsync.enabled() else None


def record(phone, direction, kind, body, sid="", status="", account="", row=None, tech=None,
           when=None, idempotency_key=None):
    """Log one text. Never raises; returns the row id or None (disabled, duplicate, or error)."""
    try:
        assert direction in DIRECTIONS and kind in KINDS
        store = get_store()
        if store is None:
            return None
        rec = {"created_at": (when or datetime.now(TZ)).astimezone(timezone.utc), "phone": norm_phone(phone),
               "account": (str(account).strip() or None) if account else None,
               "customer_row": row or None, "direction": direction, "kind": kind,
               "body": str(body or "")[:2000], "twilio_sid": sid or None, "status": status or None,
               "tech": tech or None, "idempotency_key": idempotency_key}
        return store.add(rec)
    except Exception as e:
        log.warning("message log failed (%s, %s %s)", type(e).__name__, kind, mask(phone))
        return None


def record_twiml(phone, twiml: str, account="", row=None, tech=None, when=None):
    """Log each <Message> in a webhook TwiML reply as an auto_reply. Never raises."""
    try:
        import xml.etree.ElementTree as ET
        for el in ET.fromstring(twiml).iter("Message"):
            body = "".join(el.itertext()).strip()
            if body:
                record(phone, "out", "auto_reply", body, status="twiml", account=account, row=row,
                       tech=tech, when=when)
    except Exception as e:
        log.warning("auto-reply log failed (%s, %s)", type(e).__name__, mask(phone))


ACK_DB_TIMEOUT = 3.0  # seconds; Twilio drops the webhook reply after 15s


def ack_key(phone, day) -> str:
    """Idempotency key for the once-a-day free-text acknowledgement (no phone digits in it)."""
    return "ack-" + hashlib.sha256(f"{norm_phone(phone)}|{day.isoformat()}".encode()).hexdigest()[:40]


def claim_daily_ack(phone, body, when, account="", row=None, tech=None, timeout=None) -> bool:
    """Record the free-text acknowledgement as today's (Central) auto-reply for this phone.
    True only if this call claimed it; the webhook sends the text only then. Any doubt (no
    database, an error, a duplicate, or a database slower than `timeout`) is False, so the worst
    case is no acknowledgement, never a second one. Never raises."""
    try:
        store = get_store()
        if store is None:
            return False
        when = when or datetime.now(TZ)
        rec = {"created_at": when.astimezone(timezone.utc), "phone": norm_phone(phone),
               "account": (str(account).strip() or None) if account else None, "customer_row": row or None,
               "direction": "out", "kind": "auto_reply", "body": body, "twilio_sid": None, "status": "twiml",
               "tech": tech or None, "idempotency_key": ack_key(phone, when.astimezone(TZ).date())}
        result = {}

        def run():
            try:
                result["id"] = store.add(rec)
            except Exception as e:  # noqa: BLE001
                result["err"] = type(e).__name__
        t = threading.Thread(target=run, name="ack-claim", daemon=True)
        t.start()
        t.join(ACK_DB_TIMEOUT if timeout is None else timeout)
        if t.is_alive():
            log.warning("free-text ack skipped: database slow (%s)", mask(phone))

            def tidy():  # if the insert lands late, say so on that row: the text did not go out
                t.join()
                if result.get("id"):
                    try:
                        store.finish(result["id"], None, "error: not sent (database slow)")
                    except Exception:  # noqa: BLE001
                        pass
            threading.Thread(target=tidy, name="ack-tidy", daemon=True).start()
            return False
        if "err" in result:
            log.warning("free-text ack skipped (%s, %s)", result["err"], mask(phone))
            return False
        return bool(result.get("id"))
    except Exception as e:  # noqa: BLE001
        log.warning("free-text ack skipped (%s, %s)", type(e).__name__, mask(phone))
        return False


UNREAD_WINDOW_DAYS = 30  # older unread texts drop off the dashboard badge


def unread_counts(now=None) -> dict:
    """{E.164 phone: {count, latest, body}} of unread inbound texts. {} if unavailable. Never raises."""
    try:
        store = get_store()
        if store is None:
            return {}
        since = ((now or datetime.now(TZ)) - timedelta(days=UNREAD_WINDOW_DAYS)).astimezone(timezone.utc)
        return store.unread(since)
    except Exception as e:  # noqa: BLE001
        log.warning("unread count failed (%s)", type(e).__name__)
        return {}


def mark_read(phone, at=None) -> None:
    """Ryan opened this conversation: everything up to `at` is read. Never raises."""
    try:
        store = get_store()
        if store is None or not phone:
            return
        store.mark_read(norm_phone(phone), (at or datetime.now(TZ)).astimezone(timezone.utc))
    except Exception as e:  # noqa: BLE001
        log.warning("mark read failed (%s, %s)", type(e).__name__, mask(phone))


# ------------------------------------------------------------------ thread assembly
def _parse_ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=TZ)
    try:
        dt = datetime.fromisoformat(str(v).strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def sheet_log_messages(phone, svc=None):
    """Read-only: this phone's rows from the existing 'Inbound Log' and 'Outbound Log' tabs.
    Missing tabs or a failed read just give fewer messages. Never writes the Sheet."""
    import main
    out = []
    try:
        svc = svc or main.get_sheets()
    except Exception:
        svc = None
    if svc is None:
        return out
    for tab, direction in (("Inbound Log", "in"), ("Outbound Log", "out")):
        try:
            vals = svc.spreadsheets().values().get(
                spreadsheetId=main.SHEET_ID, range=main.quote_tab(tab)).execute().get("values", [])
        except Exception:
            continue
        if not vals:
            continue
        hdr = [str(h).strip().lower() for h in vals[0]]

        def col(name):
            return hdr.index(name) if name in hdr else None
        ci, cp, cb, cs = col("timestamp"), col("phone"), col("body"), col("messagesid")
        cst = col("status")
        if None in (ci, cp, cb):
            continue
        for r in vals[1:]:
            def cell(j):
                return r[j] if j is not None and j < len(r) else ""
            if norm_phone(cell(cp)) != phone:
                continue
            at = _parse_ts(cell(ci))
            if at is None:
                continue
            out.append({"created_at": at, "direction": direction,
                        "kind": "inbound" if direction == "in" else "batch",
                        "body": str(cell(cb)), "twilio_sid": str(cell(cs) or "") or None,
                        "status": str(cell(cst) or "") or None, "tech": None, "source": "sheet"})
    return out


def thread(phone, svc=None, include_sheet=True):
    """All texts for one phone, oldest first, with sheet-log copies of DB rows dropped."""
    phone = norm_phone(phone)
    msgs, err = [], ""
    store = get_store()
    if store is not None:
        try:
            msgs = [{**m, "source": "db"} for m in store.for_phone(phone)]
        except Exception as e:
            log.warning("conversation read failed (%s, %s)", type(e).__name__, mask(phone))
            err = "Saved messages couldn't be loaded right now."
    seen = {(m.get("twilio_sid"), m["direction"]) for m in msgs if m.get("twilio_sid")}
    if include_sheet:
        for m in sheet_log_messages(phone, svc):
            if m.get("twilio_sid") and (m["twilio_sid"], m["direction"]) in seen:
                continue
            msgs.append(m)
    for m in msgs:
        m["created_at"] = _parse_ts(m["created_at"]).astimezone(TZ)
    msgs.sort(key=lambda m: m["created_at"])
    return msgs, err


# ------------------------------------------------------------------ send rules
def in_quiet_window(now: datetime) -> bool:
    """True inside 9:00 AM-8:00 PM Central (the normal texting window)."""
    now = now.astimezone(TZ)
    return QUIET_START_HOUR <= now.hour < QUIET_END_HOUR


def _keyword(body) -> str:
    return re.sub(r"[\s.!]+$", "", str(body or "").strip()).upper()


def opted_out(customer: dict, msgs) -> bool:
    """STOP wins: the Sheet says opted out, the customer's latest opt keyword was a STOP word,
    or Twilio last refused a send because they unsubscribed (21610)."""
    st = str(customer.get("Opt-In Status", "")).strip().lower()
    if "out" in st or "stop" in st:
        return True
    state = None
    for m in msgs:
        if m["direction"] == "in":
            k = _keyword(m.get("body"))
            if k in STOP_WORDS:
                state = "out"
            elif k in START_WORDS:
                state = "in"
        elif OPTOUT_ERROR in str(m.get("status") or ""):
            state = "out"
    return state == "out"


def has_consent(customer: dict) -> bool:
    ok = customer.get("OK to Text?")
    return (str(customer.get("Opt-In Status", "")).strip().lower() == "opted in"
            or ok is True or str(ok).strip().upper() == "TRUE")


def block_reason(customer: dict, msgs, anytime: bool) -> str:
    """Why this customer can't be texted from the reply box, or '' if they can."""
    if opted_out(customer, msgs):
        return "Opted out (STOP). Texts to this customer are blocked."
    if not has_consent(customer):
        return "No text consent on file (not opted in), so the reply box is off for this customer."
    if anytime:
        return "Anytime stop: no texts for this customer."
    return ""


class ConfigError(Exception):
    """Reply box isn't set up to send safely (wrong/missing number or credentials)."""


class SendError(Exception):
    """Twilio definitively rejected the message (not sent)."""


def reply_from_number() -> str:
    """Ryan's number from REPLY_FROM_NUMBER, checked: must end in 0822, never Blain's 2533,
    and never a number TECH_NUMBERS maps to another tech."""
    num = norm_phone(os.environ.get("REPLY_FROM_NUMBER", ""))
    d = re.sub(r"\D", "", num)
    if len(d) < 10:
        raise ConfigError("REPLY_FROM_NUMBER is not set")
    if d[-4:] in BLOCKED_SENDER_LAST4 or d[-4:] != RYAN_LAST4:
        raise ConfigError(f"REPLY_FROM_NUMBER must be Ryan's number ending {RYAN_LAST4}")
    import main
    tech = main.parse_tech_numbers().get(d[-10:])
    if tech and tech != "ryan":
        raise ConfigError("REPLY_FROM_NUMBER belongs to another tech")
    return num


def make_sender():
    """A send(to, body) -> sid function on Ryan's Messaging Service with From pinned to 0822.
    Passing both MessagingServiceSid and From turns off Twilio's automatic sender pick (Twilio
    errors 21711 if From isn't in that service), so it can't go out from any other number."""
    from_number = reply_from_number()
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    key, secret = os.environ.get("TWILIO_API_KEY_SID", "").strip(), os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not ((key and secret) or token):
        raise ConfigError("Twilio credentials missing")
    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client
    client = Client(key, secret, sid) if key and secret else Client(sid, token)

    def send(to: str, body: str) -> str:
        try:
            return client.messages.create(messaging_service_sid=RYAN_MESSAGING_SERVICE_SID,
                                          from_=from_number, to=to, body=body).sid
        except TwilioRestException as e:
            raise SendError(f"Twilio {e.code}") from None
    return send


_sender_factory = make_sender


def set_sender_factory(fn) -> None:
    """Tests swap in a fake; nothing in the test suite ever reaches Twilio."""
    global _sender_factory
    _sender_factory = fn or make_sender


def _clock():
    """Now in Central. Tests patch this."""
    return datetime.now(TZ)


_inflight = set()
_inflight_lock = threading.Lock()


def send_reply(customer: dict, phone: str, body: str, idem: str, confirm_quiet: bool,
               anytime=False, now=None) -> tuple:
    """Send one manual reply. Returns (outcome, detail). Outcomes: sent, duplicate, blocked,
    quiet, empty, too_long, bad_key, no_db, config, error, unknown."""
    now = (now or _clock()).astimezone(TZ)
    body = (body or "").replace("\r\n", "\n").strip()
    if not body:
        return "empty", ""
    if len(body) > MAX_BODY:
        return "too_long", ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", idem or ""):
        return "bad_key", ""
    phone = norm_phone(phone)
    if len(re.sub(r"\D", "", phone)) < 10:
        return "blocked", "No phone number on file."
    store = get_store()
    if store is None:
        return "no_db", ""
    msgs, _ = thread(phone)
    why = block_reason(customer, msgs, anytime)
    if why:
        return "blocked", why
    if not in_quiet_window(now) and not confirm_quiet:
        return "quiet", ""
    try:
        sender = _sender_factory()
    except ConfigError as e:
        log.warning("reply box not configured: %s", e)
        return "config", str(e)
    with _inflight_lock:  # one in-flight send per key in this process
        if idem in _inflight:
            return "duplicate", ""
        _inflight.add(idem)
    try:
        # Claiming the key in Postgres BEFORE calling Twilio is what makes a resubmit safe.
        msg_id = store.add({"created_at": now.astimezone(timezone.utc), "phone": phone,
                            "account": str(customer.get("Account", "")).strip() or None,
                            "customer_row": customer.get("_row"), "direction": "out", "kind": "manual",
                            "body": body, "twilio_sid": None, "status": "sending", "tech": "ryan",
                            "idempotency_key": idem})
        if msg_id is None:
            return "duplicate", ""
        try:
            sid = sender(phone, body)
            outcome, status = "sent", "sent"
        except SendError as e:
            sid, outcome, status = "", "error", f"error: {e}"
        except Exception as e:  # network etc.: may or may not have gone out, so never auto-retry
            sid, outcome, status = "", "unknown", f"unknown: {type(e).__name__}"
        try:
            store.finish(msg_id, sid, status)
        except Exception as e:
            log.warning("reply status update failed (%s)", type(e).__name__)
        log.info("reply box: %s to %s sid=%s", outcome, mask(phone), (sid[:6] + "…") if sid else "-")
        return outcome, status
    finally:
        with _inflight_lock:
            _inflight.discard(idem)


# ------------------------------------------------------------------ HTML
CSS = """
.convo{display:flex;flex-direction:column;gap:8px;margin:12px 0}
.msg{max-width:78%;padding:8px 12px;border-radius:14px;font-size:15px;white-space:pre-wrap;word-wrap:break-word}
.msg.in{align-self:flex-start;background:#fff;border:1px solid var(--bd);border-bottom-left-radius:4px}
.msg.out{align-self:flex-end;background:var(--g2);color:#fff;border-bottom-right-radius:4px}
.msg .when{display:block;font-size:11px;opacity:.75;margin-top:4px;white-space:normal}
.msg.err{background:#8a1f17}
textarea{width:100%;min-height:96px;padding:11px 12px;font:16px/1.4 inherit;border:1px solid var(--bd);border-radius:10px;background:#fff;color:var(--ink)}
.counter{font-size:13px;color:var(--mut);margin:4px 0 8px}.counter.warn{color:var(--red);font-weight:600}
.warnbox{background:#fff4dc;color:#7a5200;border-radius:10px;padding:10px 12px;margin:8px 0;font-size:14px}
.crumb{font-size:14px;margin-bottom:8px}.crumb a{color:var(--g2)}
a.convo-link{font-size:13px;font-weight:600;color:var(--g2);text-decoration:none}
html[data-theme=dark] .msg.in{background:#17201b;color:var(--ink)}html[data-theme=dark] textarea{background:#17201b;color:var(--ink)}
html[data-theme=dark] .warnbox{background:#3a2e12;color:#f0cf8a}html[data-theme=dark] .crumb a,html[data-theme=dark] a.convo-link{color:#8fd3a8}
"""

KIND_LABEL = {"inbound": "", "auto_reply": "auto-reply", "batch": "reminder", "manual": "sent by Ryan"}
FLASH = {"sent": "Sent.", "duplicate": "Already sent. That click didn't send a second text.",
         "blocked": "Not sent. This customer can't be texted from here.",
         "empty": "Type a message first.", "too_long": f"Too long. Keep it under {MAX_BODY} characters.",
         "bad_key": "That form expired. Refresh and try again.", "no_db": "Not sent. Message storage is unavailable.",
         "config": "Not sent. The reply box isn't set up to send from Ryan's number yet.",
         "error": "Not sent. Twilio rejected it.",
         "unknown": "Twilio didn't answer cleanly. It may or may not have gone out, so check before resending."}


def ct(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%b %-d, %-I:%M %p") + " CT"


def render_conversation(customer: dict, phone: str, msgs, csrf: str, idem: str, now: datetime,
                        block="", flash="", needs_confirm=False, draft="", load_err=""):
    import dashboard as d
    e = d.e
    name = str(d._get(customer, "Customer Name", "Name")) or "(no name)"
    acct = str(customer.get("Account", "")).strip()
    row = customer.get("_row")
    bubbles = []
    for m in msgs:
        cls = "in" if m["direction"] == "in" else "out"
        status = str(m.get("status") or "")
        if cls == "out" and (status.startswith("error") or status.startswith("unknown")):
            cls += " err"
        label = KIND_LABEL.get(m.get("kind"), "")
        bits = [ct(m["created_at"])] + ([label] if label else [])
        if status.startswith(("error", "unknown")):
            bits.append(status)
        elif status == "sending":
            bits.append("sending…")
        if m.get("tech") and m["tech"] != "ryan" and cls.startswith("in"):
            bits.append(f"to {m['tech']}'s number")
        bubbles.append(f"<div class='msg {cls}'>{e(m.get('body'))}<span class=when>{e(' · '.join(bits))}</span></div>")
    convo = "".join(bubbles) or "<div class=meta>No texts with this customer yet.</div>"
    if load_err:
        convo = f"<div class=err>{e(load_err)}</div>" + convo
    fl = f"<div class=ok role=status>{e(flash)}</div>" if flash else ""
    if block:
        box = f"<div class=err role=alert>{e(block)}</div>"
    else:
        quiet = not in_quiet_window(now)
        warn = ""
        if quiet or needs_confirm:
            warn = (f"<div class=warnbox role=alert>It's {now.astimezone(TZ).strftime('%-I:%M %p')} CT, outside "
                    "the normal texting hours (9 AM-8 PM CT). Only send now if it can't wait."
                    "<label style='margin-top:8px'><input type=checkbox name=confirm_quiet value=1 id=cq required> "
                    "Yes, send it anyway</label></div>")
        seg = sms_segments(draft)
        box = f"""<form method=post action="/dashboard/conversation/send" id=reply>
<input type=hidden name=csrf value="{e(csrf)}"><input type=hidden name=row value="{e(row)}">
<input type=hidden name=acct value="{e(acct)}"><input type=hidden name=idem value="{e(idem)}">
<label for=body>Reply from Ryan's number (ending {RYAN_LAST4})</label>
<textarea id=body name=body maxlength={MAX_BODY} required placeholder="PestPod: ">{e(draft)}</textarea>
<div class=counter id=ctr aria-live=polite>{seg['chars']} characters · {seg['segments']} text{'s' if seg['segments'] != 1 else ''} ({seg['encoding']})</div>
<div class=meta>Tip: texts usually start with "PestPod:". Keep it to 1 text (160 characters) when you can.</div>
{warn}<button class="btn full" type=submit id=sendbtn>Send text</button></form>
<script>(function(){{var f=document.getElementById('reply'),t=document.getElementById('body'),c=document.getElementById('ctr'),b=document.getElementById('sendbtn');
var G="@£$¥èéùìòÇ\\nØø\\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà",X="^{{}}\\\\[~]|€\\f";
function seg(s){{var g=true,u=0,i;for(i=0;i<s.length;i++){{var ch=s.charAt(i);if(X.indexOf(ch)>=0)u+=2;else if(G.indexOf(ch)>=0)u+=1;else{{g=false;break}}}}
if(!g)u=s.length;var one=g?160:70,many=g?153:67,n=u===0?0:(u<=one?1:Math.ceil(u/many));return [s.length,n,g?'GSM-7':'UCS-2']}}
function upd(){{var r=seg(t.value.replace(/\\r\\n/g,'\\n'));c.textContent=r[0]+' characters · '+r[1]+' text'+(r[1]===1?'':'s')+' ('+r[2]+')';c.className='counter'+(r[1]>1?' warn':'')}}
t.addEventListener('input',upd);upd();
f.addEventListener('submit',function(ev){{if(f.dataset.sent){{ev.preventDefault();return}}f.dataset.sent='1';b.disabled=true;b.textContent='Sending…'}});}})()</script>"""
    hdr = (f"<header><img src=\"/dashboard/static/pestpod-logo.png\" alt=\"\"><div><div class=t>PestPod</div>"
           f"<div class=s>Texts</div></div>{d.THEME_TOGGLE}</header>")
    body = f"""<style>{CSS}</style><div class=crumb><a href="/dashboard#wants-time">← Back to dashboard</a></div>{fl}
<div class=cust><div class=top><div><div class=nm>{e(name)}</div><div class=addr>{e(customer.get('City') or '')}{' · ' if customer.get('City') else ''}Phone ending {e(mask(phone)[4:] if mask(phone) != '-' else '—')}</div></div>
<div class=meta>Acct {e(acct or '—')}</div></div></div>
<div class=convo id=convo>{convo}</div>{box}
<footer>Times are Central (CT). Texts go out only from Ryan's PestPod number. Opted-out customers can't be texted.</footer>
<script>window.scrollTo(0,document.body.scrollHeight)</script>"""
    return d._page(f"Texts · {name}", body, hdr)


# ------------------------------------------------------------------ routes
def _not_found():
    return Response("Not Found", status_code=404)


def _load_customer(row: int, acct: str):
    import dashboard as d
    data = d.fetch_rows(force=True)
    return d._verify_row(data, row, acct)


def _anytime(customer) -> bool:
    import dashboard as d
    notes = "; ".join(str(x) for x in (customer.get("Pest Notes", ""), customer.get("Termite Notes", "")) if str(x).strip())
    return bool(d.ANYTIME.search(notes))


def _phone_of(customer) -> str:
    import dashboard as d
    return norm_phone(d._get(customer, "Phone (+1XXXXXXXXXX)", "Phone"))


def _view(request, row, acct, flash="", needs_confirm=False, draft="", status=200, now=None):
    import dashboard as d
    s = d._session(request)
    try:
        cust = _load_customer(row, acct)
    except ValueError:
        return d._headers(HTMLResponse(d._page("Not found", "<div class='card narrow'><h1>Customer not found</h1>"
                                               "<p>The list changed. Go back and try again.</p>"
                                               "<a class=btn href='/dashboard'>Back</a></div>"), 404))
    except Exception as ex:
        log.warning("conversation read failed: %s", type(ex).__name__)
        return d._headers(HTMLResponse(d._page("Error", "<div class='card narrow'><h1>Can't reach the sheet</h1>"
                                               "<p>Try again in a minute.</p></div>"), 502))
    phone = _phone_of(cust)
    msgs, err = thread(phone) if phone else ([], "")
    block = block_reason(cust, msgs, _anytime(cust)) if phone else "No phone number on file."
    now = now or _clock()
    if phone and not err:
        # Opening the conversation marks it read (newest message shown, or now if later).
        latest = max((m["created_at"] for m in msgs), default=now)
        mark_read(phone, max(latest, now))
    html_ = render_conversation(cust, phone, msgs, d.csrf_for(s), secrets.token_urlsafe(24), now, block=block,
                                flash=flash, needs_confirm=needs_confirm, draft=draft, load_err=err)
    return d._headers(HTMLResponse(html_, status_code=status))


@router.get("/dashboard/conversation")
def conversation(request: Request, row: int = 0, acct: str = "", msg: str = ""):
    import dashboard as d
    if not enabled():
        return _not_found()
    if not d._password():
        return d._disabled()
    if not d._session(request):
        return RedirectResponse("/dashboard/login", status_code=303)
    if row < 2:
        return Response("Bad row", status_code=400)
    return _view(request, row, acct, flash=FLASH.get(msg, ""))


@router.post("/dashboard/conversation/send")
def conversation_send(request: Request, row: int = Form(0), acct: str = Form(""), body: str = Form(""),
                      idem: str = Form(""), confirm_quiet: str = Form(""), csrf: str = Form("")):
    import dashboard as d
    from urllib.parse import urlencode
    if not enabled():
        return _not_found()
    if not d._password():
        return d._disabled()
    _, bad = d._check_post(request, csrf)
    if bad:
        return bad
    if row < 2:
        return Response("Bad row", status_code=400)
    try:
        cust = _load_customer(row, acct)
    except ValueError:
        return Response("Customer not found; refresh and try again", status_code=404)
    except Exception as ex:
        log.warning("reply box sheet read failed: %s", type(ex).__name__)
        return Response("Customers unavailable", status_code=502)
    outcome, _detail = send_reply(cust, _phone_of(cust), body, idem, confirm_quiet == "1", anytime=_anytime(cust))
    if outcome == "quiet":
        # Nothing sent: show the same draft again with the after-hours confirm box.
        return _view(request, row, acct, needs_confirm=True, draft=body, status=409)
    if outcome in ("empty", "too_long"):
        return _view(request, row, acct, flash=FLASH[outcome], draft=body, status=400)
    qs = urlencode({"row": row, "acct": acct, "msg": outcome})
    return RedirectResponse(f"/dashboard/conversation?{qs}", status_code=303)
