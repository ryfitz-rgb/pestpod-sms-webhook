# Nightly pestpod-db backup (Render Cron Job)

**Status: code only.** No Render service has been created. Create it only after Clyde approves.

## What it does
Once a night at about 3:05 AM Central:
1. Exports `pestpod-db` (Postgres 17) with `pg_dump -Fc` over the **internal** `DATABASE_URL`.
   The database's outside-access allowlist is never touched.
   (Alternative: `BACKUP_SOURCE=render_export` uses Render's API "Postgres export" and downloads
   the `.dir.tar.gz`. That needs `RENDER_BACKUP_API_KEY` + `RENDER_POSTGRES_ID`.)
2. Checks the dump: `pg_restore --list` must succeed, the archive must say it came from Postgres 17,
   and `customers` and `optin_signups` must each have more than 0 rows (counted from the dump itself).
3. Uploads `/<YYYY-MM-DDTHH_MMZ>.dump` and `/<same>.json` to the Dropbox app-folder app
   **PestPod Backups** (Dropbox shows this as `Apps/PestPod Backups`). Big files use upload sessions.
   The JSON holds sha256, size in bytes, Dropbox content hash, row counts, and versions.
4. Verifies Dropbox's `content_hash` and size match the local file.
5. Retention: deletes backup files older than 60 days, but only top-level files in that app folder
   whose names match the backup pattern. It always keeps at least the 7 newest dumps.
6. On any failure it exits 1, which triggers Render's built-in "cron job failed" email.
   It also sends a custom email only if `ALERT_SMTP_*` is set. That's off by default.

## Schedule / DST
Render cron runs in UTC. The schedule is `5 8,9 * * *`, and the script exits 0 right away unless
the America/Chicago hour is 3. That gives 08:05 UTC in CDT and 09:05 UTC in CST, so exactly one
backup a day all year. Tests check every day of 2026.

## Planned Render settings
See `render.yaml.example`: Cron Job, Docker runtime (`backups/Dockerfile`, python 3.13 slim trixie +
`postgresql-client-17`), region oregon, plan **starter** (0.5 CPU / 512 MB), build filter `backups/**`.

Env vars: `DATABASE_URL` (from pestpod-db, internal), `DROPBOX_BACKUP_APP_KEY`,
`DROPBOX_BACKUP_APP_SECRET`, `DROPBOX_BACKUP_REFRESH_TOKEN` (secrets, set in the dashboard, never in git),
plus optional `RETENTION_DAYS`, `MIN_KEEP_BACKUPS`, `REQUIRED_NONEMPTY_TABLES`, `BACKUP_HOUR_CT`, `PG_MAJOR`.

## Cost (render.com/pricing, render.com/docs/cronjobs, checked Nov 10 2026)
Starter cron is $0.00016/min, billed by the second, with a **$1/month minimum per cron job**.
Roughly 60 runs a month (30 real at about 1 min, 30 gate-only at a few seconds) is about $0.01 of
compute, so the bill is **about $1/month**. That's under the $5 cap. Dropbox use is a few hundred KB a night.

## Manual / dry run
```
python -m backups.pg_backup --force --retention-list-only
```
`--force` skips the hour gate. `--retention-list-only` logs what would be deleted and deletes nothing.

## Tests
```
python -m pytest backups -q
```
