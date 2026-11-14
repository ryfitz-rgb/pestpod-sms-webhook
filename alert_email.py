"""Optional email alert when an opted-in customer replies Y.

Sends via the Resend API when ALERT_EMAIL_ENABLED is on and the other env vars
are set. Never raises: a mail failure must not affect the SMS reply.
Off by default. Phones are last 4 only; the API key is never logged.

Env vars:
  ALERT_EMAIL_ENABLED  true / 1 / yes to send (anything else, or unset, is off)
  ALERT_EMAIL          recipient, e.g. alerts@pestpod.ai
  ALERT_EMAIL_FROM     verified From address, e.g. alerts@send.pestpod.ai
  RESEND_API_KEY       Resend API key

CLI (sends one test email; does not run unless you ask it to):
  python -m alert_email --test
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("pestpod.alert_email")

TZ = ZoneInfo("America/Chicago")
RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SEC = 10
ENABLED_VALUES = {"true", "1", "yes"}


def last4(phone) -> str:
    """Last 4 digits only. Empty if the value is too short."""
    d = re.sub(r"\D", "", str(phone or ""))
    return d[-4:] if len(d) >= 4 else ""


def is_test_account(account: str) -> bool:
    u = (account or "").strip().upper()
    return u.startswith("TEST-") or u.startswith("EX-")


def alerts_enabled() -> bool:
    return (os.environ.get("ALERT_EMAIL_ENABLED") or "").strip().lower() in ENABLED_VALUES


def _config():
    """Return {to, from, key} when sending is fully configured, else None."""
    if not alerts_enabled():
        return None
    to = (os.environ.get("ALERT_EMAIL") or "").strip()
    frm = (os.environ.get("ALERT_EMAIL_FROM") or "").strip()
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    if not (to and frm and key):
        return None
    return {"to": to, "from": frm, "key": key}


def subject(name: str, last4_digits: str, account: str = "") -> str:
    who = (name or "").strip() or "customer"
    tail = (last4_digits or "").strip() or "----"
    text = f"PestPod: Y from {who} (***-{tail})"
    if is_test_account(account):
        text = f"[TEST] {text}"
    return text


def body(name: str, last4_digits: str, account: str = "", reply_text: str = "Y",
         appointment: str = "", now: datetime | None = None) -> str:
    """Plain-text body. `appointment` is a future appt string, or blank for wants-a-time."""
    when = (now or datetime.now(TZ)).astimezone(TZ)
    stamp = when.strftime("%Y-%m-%d %I:%M %p CT").replace(" 0", " ")
    appt = (appointment or "").strip()
    appt_line = f"Appointment: {appt} (confirmed appt)" if appt else "Appointment: wants a time"
    return (
        f"Name: {(name or '').strip() or '—'}\n"
        f"Phone: ***-{(last4_digits or '').strip() or '----'}\n"
        f"Account: {(account or '').strip() or '—'}\n"
        f"Reply: {(reply_text or '').strip() or 'Y'}\n"
        f"Time: {stamp}\n"
        f"{appt_line}\n"
    )


def _redact(text, key: str) -> str:
    s = str(text)
    if key:
        s = s.replace(key, "[redacted]")
    return s


def _post_resend(payload: dict, api_key: str) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        resp.read()


def send_y_alert(name: str, last4_digits: str, account: str = "", reply_text: str = "Y",
                 appointment: str = "", now: datetime | None = None, poster=None) -> bool:
    """Send the Y alert. Returns True if the HTTP call was attempted and succeeded.

    Never raises. No-ops when disabled or any required env var is missing.
    `poster(payload, api_key)` can replace the HTTP call in tests.
    """
    cfg = _config()
    if not cfg:
        return False
    try:
        payload = {
            "from": cfg["from"],
            "to": [cfg["to"]],
            "subject": subject(name, last4_digits, account),
            "text": body(name, last4_digits, account, reply_text, appointment, now),
        }
        (poster or _post_resend)(payload, cfg["key"])
        log.info("Y alert emailed to ALERT_EMAIL for %s ***-%s",
                 (account or "").strip() or "—", (last4_digits or "").strip() or "----")
        return True
    except Exception as e:
        log.warning("Y alert email failed (%s): %s", type(e).__name__, _redact(e, cfg["key"]))
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="PestPod Y-alert email (Resend)")
    p.add_argument("--test", action="store_true",
                   help="Send one test email using ALERT_EMAIL_* / RESEND_API_KEY")
    args = p.parse_args(argv)
    if not args.test:
        p.error("pass --test to send one test email")
    ok = send_y_alert(
        name="Test",
        last4_digits="0000",
        account="TEST-CLI",
        reply_text="Y",
        appointment="",
        now=datetime.now(TZ),
    )
    print("sent" if ok else "not sent (disabled, missing config, or error; see log)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
