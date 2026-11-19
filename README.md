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
- `TECH_NUMBERS` (optional): maps a Twilio number we own to a tech name, e.g. `blain=+1504XXXXXXX`.
  More than one is comma-separated (`blain=+1504XXXXXXX,pat=+1504YYYYYYY`). Inbound SMS to a
  mapped number is tagged with that tech on the Inbound Log and Opt-Ins rows (same idea as the
  `tech` field on a web opt-in). Leave a number out of the map (Ryan's 0822 stays off it) to
  keep those texts untagged. Unset or empty: no change from today's behavior.
- `TWILIO_AUTH_TOKEN`, `WEBHOOK_PUBLIC_URL` (optional): see Security.
- Y-alert email (optional, off unless `ALERT_EMAIL_ENABLED` is `true` / `1` / `yes`): when an opted-in
  customer replies **Y** (not YES, STOP, or other keywords), a background task emails `ALERT_EMAIL`
  via Resend. Missing any of these is a no-op; a mail failure is logged and never blocks the SMS reply.
  - `ALERT_EMAIL`: recipient (e.g. `alerts@pestpod.ai`)
  - `ALERT_EMAIL_FROM`: verified From address (e.g. `alerts@send.pestpod.ai`)
  - `RESEND_API_KEY`: Resend API key (never logged)
  - Subject is `PestPod: Y from {name} (***-{last4})`, prefixed `[TEST]` for `TEST-` / `EX-` accounts.
    The body has name, last 4, account, reply, time (CT), and either the confirmed appointment or
    "wants a time". Never a full phone.
  - One-off test send (after the env vars are set): `python -m alert_email --test`

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
- Reply to Y (opted-in customer): if Proposed Appointment is a readable date today or later, `PestPod: You're confirmed for {appt}. See you then. Reply STOP to opt out.`; otherwise (blank, past, unreadable) `PestPod: Got it! We'll text you a day and time soon.` Either way Reminder Status = "Confirmed Y". If the Y-alert email env vars are set, a background email goes to `ALERT_EMAIL` (last 4 only); it never delays this reply.
- Text-to-join confirmation (JOIN/REMIND, or Y from a non-opted-in number): `PestPod: You're opted in to appointment reminders and confirmations. Up to 4 msgs/month. Msg & data rates may apply. Reply HELP for help, STOP to cancel.`
- Dashboard **Wants a time**: read-only list of customers who replied Y and have no appointment today or later (skips cancelled rows and rows whose Next Due month hasn't started). Phones shown as last 4.
- Customers tab: `Last Reply` = Y/N and `Reminder Status` = "Confirmed Y"/"Reschedule N" (must match the column validation) on the matched row. If that combined write fails, Last Reply is retried alone.
- `Inbound Log` tab (auto-created): Timestamp (America/Chicago), Phone, Body, MessageSid, Matched Row.
  A 6th `Tech` column is added only when the text was sent to a number in `TECH_NUMBERS`.
- "Opted in" = `Opt-In Status` is `Opted in` or `OK to Text?` is TRUE.
- JOIN/REMIND, or Y/YES from a non-opted-in sender, from a phone on the Customers tab: `OK to Text?` = TRUE,
  `Opt-In Status` = `Opted in`, `Opt-In Method` = `Keyword JOIN` (must be in that column's dropdown),
  `Opt-In Date` = today (Chicago). From an unknown phone: a row in the `Opt-Ins` tab (auto-created:
  Timestamp, Phone, Keyword, MessageSid, Matched Row, and Tech when the text was to a mapped
  number); strangers are never added to Customers.
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
- **Wave 2** (appointment reminder: `PestPod: Reminder: Ryan will be out {Proposed Appointment} for your pest
  service. Reply Y to confirm. Reply STOP to opt out.`; the name comes
  from optional `BATCH_TECH_NAME`, default Ryan) is manual only: `--force-wave 2 --only-phone ...`. It never runs on the schedule.

```
python batch.py --dry-run                          # plan only: no texts, no writes (phones logged as last 4)
python batch.py --dry-run --now 2026-11-03T10:00   # simulate a day
python batch.py --only-phone +1XXXXXXXXXX --force-wave 1   # live single test send (ignores the due window)
```

### Schedule
GitHub Actions (`.github/workflows/batch-reminders.yml`) daily at 15:30 and 18:30 UTC
(10:30 AM / 1:30 PM CDT, 9:30 AM / 12:30 PM CST; the second run is an idempotent retry).
Scheduled runs only fire when the repo variable `PESTPOD_BATCH_ENABLED` = `true`. Manual runs:
Actions -> PestPod batch reminders -> Run workflow (dry run is the default).

The workflow does not talk to Postgres or the Google Sheet from the GitHub runner.
`pestpod-db`'s outside-access allowlist is empty and stays that way, so runners cannot
open a database connection. Instead Actions POSTs to `POST /api/v1/batch` on the Render
web service (`pestpod-sms-webhook`), which already has `DATABASE_URL`,
`CUSTOMER_STORE=postgres`, and the Twilio credentials, and runs `batch.py` there.
You can still run `python batch.py --dry-run` in a Render Shell the same way.

**Ryan needs to set (once), after or just before merging this branch. Cost: $0.
Do not flip `PESTPOD_BATCH_ENABLED`. Do not change the allowlist. Do not put
`DATABASE_URL` in GitHub.**

1. Create a long random token (for example `openssl rand -hex 32`).
2. Add `BATCH_RUN_TOKEN` with that value as a **secret** on the Render web service
   `pestpod-sms-webhook` (Environment). The webhook will redeploy; `/version` then
   shows `"batch_run": true` (the token itself is never shown).
3. Add the **same** value as the GitHub Actions secret `BATCH_RUN_TOKEN` on this repo.
4. Optional: repo variable `BATCH_RUN_URL` if the webhook is not at
   `https://pestpod-sms-webhook.onrender.com` (no trailing slash, origin only).

Until those are set, a manual Actions run exits 1 with a short setup message, and
the endpoint returns 503. Then do one manual dry-run (the default) to confirm it
reads Postgres. Live scheduled sends stay off until `PESTPOD_BATCH_ENABLED` is
`true`.

Note: GitHub disables scheduled workflows in public repos after 60 days with no commits.

## Dashboard v1 (`dashboard.py`)
Login-gated route view at `/dashboard` (same service). Customers grouped by due month: the `Next Due`
month, or `Office List Month` when that is later (pushed stops). EX-/TEST rows hidden.
- **Mark as serviced**: date picker (defaults to today, backdating allowed) writes ONLY `Last Service Date` (H).
- **Push to next month**: writes ONLY `Office List Month` (next month). Never touches H, I or K.
- Next Due (I) and OK to Text? (K) are formulas and are never written. No texts are sent from the dashboard
  (except the optional reply box below, off by default).
- Env: `DASHBOARD_PASSWORD` (required; dashboard returns 503 when unset), `DASHBOARD_USER` (default `ryan`),
  `DASHBOARD_SECRET` (optional). Signed HttpOnly/Secure/SameSite=Lax session cookie (30 days), CSRF token on every POST,
  login throttled after 10 failures per IP per 15 min. Changing the password signs everyone out.
- Note: `batch.py` still nudges from `Next Due` only; it does not read `Office List Month` yet.

### Add + edit customers (`customer_edit.py`, off unless `DASHBOARD_EDIT_ENABLED`)
Ships dark. Set `DASHBOARD_EDIT_ENABLED=true` (or `1`/`yes`/`on`) on the Render service to turn it on;
remove it (or set `false`) to turn it off. While off, the routes return 404 and the dashboard looks exactly
as before (the older phone/address/city Edit keeps working either way).
- **+ Add customer** (top of `/dashboard`): Account number (required, must not already be on the list),
  Customer name (required), phone, address, city, service type, frequency, last service date, opt-in
  status/method/date, pest and termite notes. Adds one row at the bottom with `Source` = `Dashboard`.
- **Edit** on each card: the same fields (Account can't be changed). Only the columns that changed are written.
- Writes go through `main.get_sheets()`, so they land in whichever store is live (`CUSTOMER_STORE=postgres`
  -> gridstore; otherwise the Sheet). RAW values; dates stored as day serials. Never writes Next Due (I),
  OK to Text? (K), Wave, Reminder Status, Last Reply, Proposed Appointment, Missed Count, Office List Month,
  Status/Cancelled On, or Import Notes.
- Checks: phone becomes `+1XXXXXXXXXX` (10-digit US), dates can't be in the future, text can't start with `=`.
- **Consent**: OK to Text? turns TRUE on its own when Opt-In Status is "Opted in" and there's a phone. So marking
  someone Opted in, or changing an opted-in customer's phone, needs the "Customer agreed to get texts at this
  number" box ticked plus an Opt-In Method (asked again in a pop-up). Opt-In Date defaults to today. That save is
  logged as action `optin`. Anyone Opted out, or whose texts end in a STOP, can't be opted back in here.
- **Undo Log**: every add/edit appends a row first (action `add`, `edit` or `optin`; old/new values as JSON of the
  changed columns in Old Status / New Status). The card's Undo button puts the old values back while nothing
  has changed since. Undoing an add blanks that row (it drops off the list; nothing is deleted).
- **Shared phone heads-up**: after an add/edit, if that phone (last 10 digits) is also on another active customer,
  the green message says so (last 4 only, plus the other account and name). It never blocks the save. Replies from
  that number match the first row on the list, so the note says which one.
- The older phone/address/city Edit (`POST /dashboard/edit`) now refuses a new phone for a customer who is
  Opted in (consent is per number) and points to the full Edit's consent box. Clearing the phone is still allowed.
- Same session login + CSRF token + origin check as every other dashboard form. Never sends a text.

### Link a sign-up to a customer (`signup_link.py`, off unless `DASHBOARD_EDIT_ENABLED`)
While editing is on, each unmatched sign-up that ticked the consent box gets a **Link to customer** button.
1. `GET /dashboard/signup/link?id=<signup id>`: pick the customer (search by name, account or city; likely
   matches first; test/example rows aren't listed). Phones show as last 4 only.
2. `GET /dashboard/signup/link?id=..&row=..&acct=..`: confirm page with the sign-up (name, last 4, time, page),
   the customer (name, account, current phone last 4, Opt-In status), what will be written, and warnings
   (different phone gets replaced, already opted in on another number, cancelled, phone also on another customer).
   Opted out / STOP customers (or a STOP in the texts for either number) are refused: no Confirm button.
3. `POST /dashboard/signup/link` (needs `confirm=yes`, session + CSRF + origin check like every other form):
   the server reads the sign-up's FULL phone from `optin_signups` and writes Phone, Opt-In Status `Opted in`,
   Opt-In Method `Web form`, Opt-In Date = the day they signed up (CT). One Undo Log row (action `link`), so
   Undo on the card puts the old values back. The sign-up leaves the unmatched list because its phone is now on a
   marked row; the link is also noted in the `optin_links` table (signup id, account, row; no phone).
- Refused: no consent (contact only), not on the unmatched list any more (already linked/matched), opted out/STOP.
- Never sends a text. The full number is never sent to the browser by these pages; it only shows up afterwards
  where every customer's number already lives (that customer's Edit form).

### Unmatched sign-ups (`unmatched.py`, read-only)
Section + red badge on `/dashboard` listing web opt-ins (`optin_signups`) that need a hand fix:
**no phone match** on the Customers tab, **matched but not marked** (Opt-In Status/Method/Date, L/M/N, not
all filled with "Opted in"), plus **no consent** (contact only; listed separately, not in the badge).
Shows name, last 4 of phone, submitted time (CT), consent, tech/page path, and a likely Customers match by
name (account + name). Import Review rows never count as a match; a phone hit there is shown as a hint.
Phones are compared on their last 10 digits (+1 and punctuation ignored). Nothing is written anywhere.
- `GET /dashboard/unmatched.json`: same list, dashboard session cookie.
- `GET /api/v1/optin/unmatched`: same list for an automated reader; dashboard session or HTTP Basic with the
  existing `DASHBOARD_USER`/`DASHBOARD_PASSWORD` (shares the login throttle). No new env var.
  The session cookie is scoped to `/dashboard`, so login (and each `/dashboard` visit) also sets the same
  signed token scoped to exactly `/api/v1/optin/unmatched`; no other `/api` route receives it. Browsers get
  a plain 401 (no `WWW-Authenticate`, so no password pop-up); scripts get the Basic challenge.
  JSON: `{generated_ct, actionable, no_consent, total, entries:[{ids, name, phone_last4, submitted_ct,
  consent, page, reasons, matched_rows, likely_match, import_review_hint, ...}]}`; never a full phone.

### Texts view + reply box (`conversations.py`, off unless `REPLY_BOX_ENABLED`)
One page per customer with every text to and from them, oldest first, times in Central (CT), and a
box to reply from Ryan's business number (ending 0822).
- **History**: the `messages` table in pestpod-db (created with the rest of `pgsync.SCHEMA`): phone,
  account, row, direction (in/out), kind (`inbound`, `auto_reply`, `batch`, `manual`), body, time,
  Twilio SID, status. Logged from the webhook (inbound + TwiML auto-replies, after the reply is sent),
  `batch.py` live sends, and the reply box. Logging runs whenever `DATABASE_URL` is set, even with the
  box off, and a logging failure never breaks a reply or a batch. The view also folds in the existing
  `Inbound Log` / `Outbound Log` tabs (read-only, de-duplicated by SID) so older texts show. No Twilio backfill.
- **Routes** (dashboard login + CSRF; 404 while `REPLY_BOX_ENABLED` is off):
  `GET /dashboard/conversation?row=N&acct=A`, `POST /dashboard/conversation/send`.
  The **Wants a time** list gets an "Open texts" link per customer when the box is on.
- **Reply box**: live character + text-count (GSM-7 160/153, UCS-2 70/67 for emoji or curly quotes).
  A "PestPod:" hint, not forced.
- **Safety (server-side)**: blocked for opted-out customers (Opt-In Status says out, their latest
  keyword was STOP/UNSUBSCRIBE/..., or Twilio last returned 21610), customers without text consent,
  and Anytime stops. Outside 9 AM-8 PM CT it shows a warning and needs a "send it anyway" tick.
  One send per form: the page's one-time key is claimed in Postgres before Twilio is called, so a
  double click or resubmit can't send twice (the button also disables on click). Without
  `DATABASE_URL` it refuses to send. Phones show as last 4 only, in the page and in logs.
- **Sender**: Ryan's Messaging Service (`MG7aa08d...`, pinned in code, not read from env) with `From`
  pinned to `REPLY_FROM_NUMBER`, which must end in 0822 and must not be Blain's 2533 or any number
  `TECH_NUMBERS` maps to another tech. Twilio errors (21711) rather than switching numbers.
- Env: `REPLY_BOX_ENABLED` (default off), `REPLY_FROM_NUMBER` (Ryan's E.164 number), and the same
  `TWILIO_ACCOUNT_SID` + `TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET` (or `TWILIO_AUTH_TOKEN`) batch.py uses.
