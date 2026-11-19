"""Authenticated trigger for batch.py on the Render web service.

GitHub Actions runners are outside Render, and pestpod-db's allowlist is empty
on purpose, so they cannot open a Postgres connection. This endpoint runs the
batch inside the webhook service, which already has DATABASE_URL and
CUSTOMER_STORE=postgres, with no allowlist change.

Auth: Authorization: Bearer <BATCH_RUN_TOKEN> (or X-PestPod-Batch-Token).
If BATCH_RUN_TOKEN is unset, the endpoint is disabled (503). The token and
full phone numbers are never logged. dry_run defaults to true.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import batch
from main import TZ

log = logging.getLogger("pestpod.batch_api")
router = APIRouter()
_run_lock = threading.Lock()


def token_configured() -> bool:
    return bool(os.environ.get("BATCH_RUN_TOKEN", "").strip())


def _authorized(request: Request) -> bool:
    expected = os.environ.get("BATCH_RUN_TOKEN", "").strip()
    if not expected:
        return False
    got = ""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        got = auth[7:].strip()
    if not got:
        got = (request.headers.get("X-PestPod-Batch-Token") or "").strip()
    if not got or len(got) != len(expected):
        secrets.compare_digest(expected, expected)
        return False
    return secrets.compare_digest(got, expected)


class BatchBody(BaseModel):
    dry_run: bool = True
    now: str | None = None
    only_phone: str | None = None
    force_wave: int | None = Field(default=None)

    @field_validator("force_wave")
    @classmethod
    def _wave(cls, v):
        if v is None:
            return v
        if v not in (1, 2):
            raise ValueError("force_wave must be 1 or 2")
        return v


def _parse_now(value: str):
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ)
    except ValueError:
        raise HTTPException(status_code=400, detail="now must be a Chicago time like 2026-11-01T10:00")


@router.post("/api/v1/batch")
def run_batch(request: Request, body: BatchBody = BatchBody()):
    if not token_configured():
        raise HTTPException(status_code=503, detail="batch run is disabled (BATCH_RUN_TOKEN is not set)")
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    if body.now and not body.dry_run:
        raise HTTPException(status_code=400, detail="now is only allowed with dry_run")

    now = _parse_now(body.now) if body.now else None
    lines: list[str] = []
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="a batch run is already in progress")
    try:
        log.info("batch trigger: mode=%s force_wave=%s only_phone=%s now=%s",
                 "DRY RUN" if body.dry_run else "LIVE",
                 body.force_wave,
                 batch.mask_phone(body.only_phone) if body.only_phone else None,
                 body.now or None)
        try:
            summary = batch.run(
                now=now,
                dry_run=body.dry_run,
                force_wave=body.force_wave,
                only_phone=body.only_phone,
                out=lines.append,
            )
        except SystemExit as e:
            msg = str(e) or "batch aborted"
            raise HTTPException(status_code=400, detail=msg) from None
        except HTTPException:
            raise
        except Exception:
            log.exception("batch trigger failed")
            raise HTTPException(status_code=500, detail="batch failed") from None
    finally:
        _run_lock.release()

    payload = {
        "ok": not (not body.dry_run and summary.get("errors")),
        "mode": summary.get("mode"),
        "wave": summary.get("wave"),
        "planned": summary.get("planned"),
        "sent": summary.get("sent"),
        "errors": summary.get("errors"),
        "abort": summary.get("abort"),
        "log": lines,
    }
    if not payload["ok"]:
        return JSONResponse(payload, status_code=500)
    return payload
