# Plan: pestpod-db becomes the main customer list

Status: **prep only.** Nothing is live. `CUSTOMER_STORE` is unset, which means `sheet`,
and with that setting the app behaves exactly as it does today.

## How it works
- `gridstore.py` answers the same small set of Google Sheets calls the app already makes, but from
  Postgres: tables `grid_tabs`, `grid_rows` (one JSONB row of raw cell values per sheet row) and
  `grid_changes` (an audit row for every cell change). `customer_list_v` is a typed read-only view.
- `main.get_sheets()` returns the Postgres store when `CUSTOMER_STORE=postgres` and the Google Sheet
  otherwise. The webhook, batch.py and the dashboard all go through it, so their logic doesn't change.
- Customers **I (Next Due)** and **K (OK to Text?)** are never stored. `formulas.py` computes them on
  every read with the sheet's own ARRAYFORMULAs, and any write to I or K is refused.
- Anytime stops keep `Reminder Status = "Excluded - no SMS (anytime stop)"`, which batch.py already
  skips. Tests cover it.
- Also fixed: `mark_customer_opted_in` used to write `OK to Text? = TRUE` into column K, which would
  break the K ARRAYFORMULA. It now skips I and K. K turns TRUE from Opt-In Status plus phone.

## Tools (run from a Render Shell on pestpod-sms-webhook, which has both the sheet creds and DATABASE_URL)
```
python -m gridstore import --confirm            # copy every tab into Postgres (read-only on the sheet)
python -m gridstore import --confirm --replace  # re-copy (parallel-run week / final cutover)
python -m gridstore verify                      # cell-by-cell compare, prints counts only
python -m gridstore export --out /tmp/grid.json # raw dump for rollback
```

## Cutover sequence (each step needs its own OK)
1. Merge this prep PR. No behavior change except the K write guard.
2. Run `import`, then `verify`. Repeat `import --replace` + `verify` daily for about a week. The goal is MATCH.
3. Move batch to a path that can reach the database (see the report options), with
   PESTPOD_BATCH_ENABLED still off.
4. Dashboard extras: add customer, edit more fields.
5. Cutover, in the evening: freeze sheet edits, run a final `import --replace`, get `verify` = MATCH,
   then set `CUSTOMER_STORE=postgres` on the webhook. Smoke test: /health, /dashboard, `batch.py --dry-run`.
6. Optional: a nightly one-way read-only copy of the list to a sheet for viewing.
7. Add `grid_rows` to the backup job's REQUIRED_NONEMPTY_TABLES.

## Rollback
Set `CUSTOMER_STORE=sheet` (restart, about a minute). Then copy any changes made after cutover back to the sheet,
never I or K: `grid_changes` lists every change, and `write_back_plan()` / `export` give the full grid.
Writing to the sheet needs its own approval. Nightly Dropbox backups include the grid tables.
