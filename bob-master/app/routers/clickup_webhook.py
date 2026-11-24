"""
POST /webhooks/clickup -- receiver for the AM Delivery 2.0 space webhook
(see app/tasks/clickup_task_notifications.py). ClickUp signs each delivery:
X-Signature = hex HMAC-SHA256(webhook secret, raw body). Answer fast (ClickUp
retries slow/failed deliveries and eventually suspends the webhook), do the
ClickUp/Atlas/Slack round trips on a background task.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import get_settings
from app.tasks.clickup_task_notifications import process_event

logger = logging.getLogger(__name__)
router = APIRouter()


def _run(payload: dict) -> None:
    try:
        result = process_event(payload)
        logger.info("clickup notify: %s", result)
    except Exception:
        logger.exception("clickup notify failed for task %s", payload.get("task_id"))


@router.post("/webhooks/clickup")
async def clickup_webhook(request: Request, background: BackgroundTasks) -> dict:
    secret = get_settings().clickup_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    body = await request.body()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-signature", "")):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json")
    background.add_task(_run, payload)
    return {"ok": True}
