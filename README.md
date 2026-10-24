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
- Customers tab: `Last Reply` = Y/N and `Reminder Status` = "Confirmed Y"/"Reschedule N" (must match the column validation) on the matched row. If that combined write fails, Last Reply is retried alone.
- `Inbound Log` tab (auto-created): Timestamp (America/Chicago), Phone, Body, MessageSid, Matched Row.

## Tests
`pip install -r requirements.txt pytest httpx && pytest -q`

## Batch reminder sender (`batch.py`)
Two waves per month (America/Chicago): wave 1 on days 1-3, wave 2 on days 15-17. No third wave.
- **Day-groups (thirds):** the Customers `Wave` column (1/2/3, strict dropdown) is the customer's day-group;
  day N of a window texts group N. If `Wave` is blank, the group is `sha256(phone) mod 3 + 1` and is
  written into `Wave` on send. The last day of a window (3rd/17th) also catches up anyone still eligible
  who wasn't texted this wave this month.
- **Eligible:** `OK to Text?` TRUE, `Opt-In Status` = `Opted in`, valid phone, a `Proposed Appointment`
  that isn't in the past, `Reminder Status` not `Confirmed Y`. Wave 2 also skips `Reschedule N`.
  `EXAMPLE DATA` / `EX-` rows are never texted.
- **Idempotent:** a customer never gets the same wave twice in a month (Outbound Log + row stamp).
- **Writes after each send:** `Reminder Status` = `Nudge 1 sent` / `Nudge 2 sent`; a row in the
  `Outbound Log` tab (auto-created): Timestamp, Phone, Name, Wave, Body, MessageSid, Status, Row.
- **Quiet hours:** only sends 9:00 AM-8:00 PM Chicago; 1.5 s between texts; `BATCH_MAX_SENDS` cap (200).
- Template: `PestPod: Hi {Name}, your pest control service is scheduled for {Proposed Appointment}. Reply Y to confirm or N to reschedule. Reply STOP to opt out. Msg & data rates may apply.`
  Sent via Messaging Service `MG7aa08d66618db7ad78ad6af28b793bf2`.

```
python batch.py --dry-run                          # plan only: no texts, no writes
python batch.py --dry-run --now 2026-11-01T10:00   # simulate a day
python batch.py --only-phone +1XXXXXXXXXX --force-wave 1   # live single test send
```

### Schedule
GitHub Actions (`.github/workflows/batch-reminders.yml`) at 15:30 and 18:30 UTC on days 1-3 and 15-17
(10:30 AM / 1:30 PM CDT, 9:30 AM / 12:30 PM CST; the second run is an idempotent retry).
Scheduled runs only fire when the repo variable `PESTPOD_BATCH_ENABLED` = `true`. Manual runs:
Actions -> PestPod batch reminders -> Run workflow (dry run is the default).

Repo secrets: `GOOGLE_SA_JSON_B64`, `TWILIO_ACCOUNT_SID`, and either `TWILIO_API_KEY_SID` +
`TWILIO_API_KEY_SECRET` or `TWILIO_AUTH_TOKEN`.

Note: GitHub disables scheduled workflows in public repos after 60 days with no commits.
