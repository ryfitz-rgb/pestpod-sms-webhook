# PestPod SMS Webhook

FastAPI app for Twilio inbound SMS (`POST /api/v1/sms/webhook`, `GET /health`).
Y/YES confirms, N/NO reschedules; STOP/START/HELP are left to Twilio Advanced Opt-Out.
Replies persist to the "PestPod Reminders v1" Google Sheet.

## Env vars
- `GOOGLE_SA_JSON_B64`: base64 of a Google service account JSON key. Share the sheet with the
  service account email as Editor. If unset or invalid, Sheets is disabled and only `TEST_PHONE` matches.
- `SHEET_ID` (optional): defaults to the PestPod Reminders v1 sheet.
- `TEST_PHONE` (optional): fallback match when Sheets is unavailable.

## Sheet writes
- Customers tab: `Last Reply` = Y/N and `Reminder Status` = Confirmed/Reschedule on the matched row.
- `Inbound Log` tab (auto-created): Timestamp (America/Chicago), Phone, Body, MessageSid, Matched Row.

## Tests
`pip install -r requirements.txt pytest httpx && pytest -q`
