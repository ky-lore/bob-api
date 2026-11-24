"""
One-off: register (or list / delete) the ClickUp webhook that feeds
POST /webhooks/clickup. Run AFTER the receiver is deployed.

    .venv/bin/python scripts/register_clickup_webhook.py list
    .venv/bin/python scripts/register_clickup_webhook.py create
    .venv/bin/python scripts/register_clickup_webhook.py delete <webhook_id>

`create` prints the webhook's signing secret ONCE -- put it in the Railway env
of the bob-api service as CLICKUP_WEBHOOK_SECRET (never in git/.env/docs).
Until that is set the receiver answers 503 and ClickUp will retry, then
suspend the webhook; set the variable promptly after creating.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.integrations.clickup import ClickUpClient

ENDPOINT = "https://bob-api-production-9ba0.up.railway.app/webhooks/clickup"
EVENTS = ["taskCreated", "taskStatusUpdated"]


def main(argv: list[str]) -> None:
    settings = get_settings()
    http = ClickUpClient()._client
    team = settings.clickup_workspace_id
    cmd = argv[1] if len(argv) > 1 else "list"

    if cmd == "list":
        for w in http.get(f"/team/{team}/webhook").json().get("webhooks", []):
            print(w.get("id"), w.get("endpoint"), w.get("events"), "space", w.get("space_id"), "health", w.get("health"))
    elif cmd == "create":
        resp = http.post(
            f"/team/{team}/webhook",
            json={"endpoint": ENDPOINT, "events": EVENTS, "space_id": int(settings.clickup_notify_space_id)},
        )
        resp.raise_for_status()
        data = resp.json()
        print("webhook id:", data.get("id"))
        print("CLICKUP_WEBHOOK_SECRET =", (data.get("webhook") or {}).get("secret"))
    elif cmd == "delete" and len(argv) > 2:
        http.delete(f"/webhook/{argv[2]}").raise_for_status()
        print("deleted", argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
