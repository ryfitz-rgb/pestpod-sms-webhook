"""
PestPod SMS Webhook — minimal FastAPI app for local/ngrok testing.
Receives Twilio inbound SMS, replies with approved templates, logs to console.
Sheet lookup stubs are placeholders.
"""
import os
import re
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI(title="PestPod SMS Webhook")

# Hardcoded test match; number comes from the TEST_PHONE env var (kept out of the repo).
TEST_PHONE = os.environ.get("TEST_PHONE", "")

def normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", raw.strip())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return raw.strip()

# STOP/START/HELP are handled by Twilio Advanced Opt-Out on the Messaging Service.
OPT_KEYWORDS = {"STOP","STOPALL","UNSUBSCRIBE","CANCEL","END","QUIT","START","UNSTOP","HELP","INFO"}

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

def find_customer_by_phone(phone: str) -> dict | None:
    if TEST_PHONE and normalize_phone(phone) == normalize_phone(TEST_PHONE):
        return {
            "name": "Ryan",
            "proposed_appointment": "Tue 10/27 at 9 AM",
            "opted_in": True,
        }
    return None

def log_inbound(phone: str, body: str, sid: str) -> None:
    print(f"[INBOUND] {phone} | {body} | SID={sid}")

def update_customer_reply(phone: str, reply: str) -> None:
    print(f"[UPDATE] {phone} → Last Reply = {reply}")

@app.post("/api/v1/sms/webhook")
async def sms_webhook(
    From: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
):
    reply = Body.strip().upper()
    phone = normalize_phone(From)
    log_inbound(phone, Body, MessageSid)

    resp = MessagingResponse()
    customer = find_customer_by_phone(phone)

    # Opt-out/help keywords: Twilio Advanced Opt-Out already replied, so stay silent.
    if reply in OPT_KEYWORDS:
        return PlainTextResponse(str(resp), media_type="application/xml")

    if customer:
        if reply in ("Y", "YES"):
            resp.message(confirm_reply(customer["name"], customer["proposed_appointment"]))
            update_customer_reply(phone, "Y")
        elif reply in ("N", "NO"):
            resp.message(reschedule_reply(customer["name"]))
            update_customer_reply(phone, "N")
        else:
            resp.message("PestPod: Reply Y to confirm or N to reschedule. Reply HELP for help, STOP to opt out.")
    else:
        resp.message("PestPod: No matching appointment found. Reply HELP for help, STOP to opt out.")

    return PlainTextResponse(str(resp), media_type="application/xml")

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
