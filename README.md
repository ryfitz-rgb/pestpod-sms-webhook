# PestPod SMS Webhook

FastAPI app for Twilio inbound SMS (`POST /api/v1/sms/webhook`, `GET /health`, `GET /version`).
Y/YES from an opted-in customer confirms; Y/YES from anyone else, and JOIN/REMIND from anyone, opts in
(text-to-join). N/NO from an opted-in customer reschedules (otherwise help only, nothing marked).
Replies are case-insensitive, trimmed, trailing "." / "!" ignored. STOP/START/HELP are left to
Twilio Advanced Opt-Out (the webhook stays silent on them). `/version` returns Render's `RENDER_GIT_COMMIT`
and `signature_validation` (true/false).
Replies persist to the "PestPod Reminders v1" Google Sheet.

## Env vars
- `GOOGLE_SA_JSON_B64`: base64 of a Google service account JSON key. Share the sheet with the
  service account email as Editor. If unset or invalid, Sheets is disabled and only `TEST_PHONE` matches.
- `SHEET_ID` (optional): defaults to the PestPod Reminders v1 sheet.
- `TEST_PHONE` (optional): fallback match when Sheets is unavailable.
- `TWILIO_AUTH_TOKEN`, `WEBHOOK_PUBLIC_URL` (optional): see Security.

## Security (Twilio signature validation)
`POST /api/v1/sms/webhook` checks the `X-Twilio-Signature` header (HMAC-SHA1 of the URL + form params,
via `twilio.request_validator.RequestValidator`) before doing anything else. A missing or wrong
signature gets HTTP 403: no lookup, no sheet writes, no log rows, no TwiML; a warning is logged.
- `TWILIO_AUTH_TOKEN`: the Twilio account's **primary Auth Token** (Console -> Account -> API keys &
  tokens). API key secrets can't validate signatures. If unset, validation is OFF: requests are still
  accepted and a loud warning is logged at startup and on every request (rollout safety).
- `WEBHOOK_PUBLIC_URL` (optional): the exact URL configured in Twilio, e.g.
  `https://pestpod-sms-webhook.onrender.com/api/v1/sms/webhook`. Without it the signed URL is rebuilt
  as `<X-Forwarded-Proto, default https>://<Host><path>?<query>` (Render terminates TLS at its proxy).
- `GET /version` shows `"signature_validation": true` when enforced (the token itself is never exposed).
  `/health` and `/version` need no signature.

## Sheet writes
- Reply to Y (opted-in customer, wants service): `PestPod: Got it! We'll text you a day and time soon.` Reminder Status stays "Confirmed Y" (no "awaiting time" value in the dropdown).
- Customers tab: `Last Reply` = Y/N and `Reminder Status` = "Confirmed Y"/"Reschedule N" (must match the column validation) on the matched row. If that combined write fails, Last Reply is retried alone.
- `Inbound Log` tab (auto-created): Timestamp (America/Chicago), Phone, Body, MessageSid, Matched Row.
- "Opted in" = `Opt-In Status` is `Opted in` or `OK to Text?` is TRUE.
- JOIN/REMIND, or Y/YES from a non-opted-in sender, from a phone on the Customers tab: `OK to Text?` = TRUE,
  `Opt-In Status` = `Opted in`, `Opt-In Method` = `Keyword JOIN` (must be in that column's dropdown),
  `Opt-In Date` = today (Chicago). From an unknown phone: a row in the `Opt-Ins` tab (auto-created:
  Timestamp, Phone, Keyword, MessageSid, Matched Row); strangers are never added to Customers.
  Either way the reply is the opt-in confirmation (`JOIN_REPLY` in `main.py`), except for YES:
  YES is a Twilio Advanced Opt-Out opt-in keyword, so Twilio already sends its own "You're
  subscribed" reply; the webhook records the opt-in and returns empty TwiML (no double welcome).
  Y is not a Twilio keyword, so Y still gets `JOIN_REPLY`.

## Speed (Twilio's 15 s limit)
The reply is decided from the Customers tab only, then returned right away. Inbound Log appends,
Customers writes (Last Reply / Reminder Status / opt-in) and Opt-Ins appends run as FastAPI background
tasks after the response is sent; failures are logged, never raised. Customers is cached in memory:
used as-is for 60 s, then (up to 15 min old) served immediately while a background thread re-reads it;
older or empty means one synchronous read. The cache is warmed on startup and re-read after each
webhook write.

## Tests
`pip install -r requirements.txt pytest httpx && pytest -q`

## Batch reminder sender (`batch.py`)
Customers have no fixed appointments. The first text is a **due nudge** based on `Next Due`
(the Customers formula: `Last Service Date` + `Frequency`; EOM = every 2 months). `Proposed Appointment`
only gets filled after Ryan proposes a day/time and the customer replies Y.
- **When:** customers are due in a **month**, not on a day: the month their `Next Due` falls in (Ryan just
  finishes every stop by month end). The daily runs (10:30 AM / 1:30 PM CDT) nudge each customer once,
  starting on the 1st of their due month; a later run that month catches anyone missed. Once the due month
  ends, that cycle gets no more texts. Due month not started yet: no text.
- **Eligible:** `OK to Text?` TRUE, `Opt-In Status` = `Opted in`, valid phone, a readable `Next Due`,
  no `Proposed Appointment` set for today or later, `Reminder Status` not starting with `Excluded`.
  `EXAMPLE DATA` / `EX-` rows are never texted.
- **Once per due cycle (due month):** no second nudge for the same `Next Due` month (Outbound Log, plus the row stamp if the
  log has nothing). When `Last Service Date` moves forward, the next cycle gets a new nudge.
- **Writes after each send:** `Reminder Status` = `Nudge 1 sent`; a row in the `Outbound Log` tab
  (auto-created): Timestamp, Phone, Name, Wave, Body, MessageSid, Status, Row. `Wave` is no longer used or written.
- **Quiet hours:** only sends 9:00 AM-8:00 PM Chicago; 1.5 s between texts; `BATCH_MAX_SENDS` cap (200).
- Template (exact): `PestPod: You're due for pest service. Reply Y and we'll set a time. Reply STOP to opt out.`
  Sent via Messaging Service `MG7aa08d66618db7ad78ad6af28b793bf2`.
- **Wave 2** (appointment reminder, old template `PestPod: Hi {Name}, your pest control service is scheduled for
  {Proposed Appointment}. Reply Y to confirm or N to reschedule. Reply STOP to opt out. Msg & data rates may apply.`)
  is manual only: `--force-wave 2 --only-phone ...`. It never runs on the schedule.

```
python batch.py --dry-run                          # plan only: no texts, no writes
python batch.py --dry-run --now 2026-11-03T10:00   # simulate a day
python batch.py --only-phone +1XXXXXXXXXX --force-wave 1   # live single test send (ignores the due window)
```

### Schedule
GitHub Actions (`.github/workflows/batch-reminders.yml`) daily at 15:30 and 18:30 UTC
(10:30 AM / 1:30 PM CDT, 9:30 AM / 12:30 PM CST; the second run is an idempotent retry).
Scheduled runs only fire when the repo variable `PESTPOD_BATCH_ENABLED` = `true`. Manual runs:
Actions -> PestPod batch reminders -> Run workflow (dry run is the default).

Repo secrets: `GOOGLE_SA_JSON_B64`, `TWILIO_ACCOUNT_SID`, and either `TWILIO_API_KEY_SID` +
`TWILIO_API_KEY_SECRET` or `TWILIO_AUTH_TOKEN`.

Note: GitHub disables scheduled workflows in public repos after 60 days with no commits.
