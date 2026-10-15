"""
Local preview of the Sales Audit dashboard (2026-10-15).

Run from the repo root:   .venv/bin/python scripts/dev_sales_audit.py [--port 8765] [--refresh]

Serves ONLY the sales-audit router (not app.main) on a throwaway SQLite DB
seeded from the real production run. Deliberately not the full app: app.main's
startup launches the scheduler, whose hourly CMDCTR job pushes to prod Atlas,
and the local DATABASE_URL is Railway's unresolved template anyway.

- Only network call: one read-only GET of prod's /reports/sales-audit/latest
  (via curl -- Python's urllib hits SSL cert errors in this dev environment).
  The snapshot is cached in /tmp; pass --refresh to re-fetch.
- Overrides/assignments you make write only to the local SQLite file, and are
  wiped each time this script starts.
- Don't click "Refresh data" locally: it would try real Atlas/GHL/Drive reads
  with your local .env keys. Edit app/templates/sales_audit.html and reload the
  browser -- templates are re-read on every request.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # the router loads templates from the relative path app/templates

PROD = "https://bob-api-production-9ba0.up.railway.app/reports/sales-audit/latest"
SNAPSHOT = Path("/tmp/sales_audit_prod_snapshot.json")
DB_FILE = Path("/tmp/sales_audit_local.db")


def fetch_snapshot() -> dict:
    # prod's dashboard is behind an access password (2026-10-15); pass it via the environment, never on the command line
    pw = os.environ.get("SALES_DASHBOARD_PASSWORD") or sys.exit("prod is password-gated: export SALES_DASHBOARD_PASSWORD=<the password> first")
    subprocess.run(["curl", "-sf", "-m", "30", "-H", f"X-Sales-Password: {pw}", PROD, "-o", str(SNAPSHOT)], check=True)
    data = json.loads(SNAPSHOT.read_text())
    if not data.get("rows"):
        sys.exit("prod has no sales-audit run yet -- nothing to preview (trigger one on prod first)")
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--refresh", action="store_true", help="re-fetch the prod snapshot instead of using the cached one")
    args = ap.parse_args()

    if args.refresh or not SNAPSHOT.exists():
        print("fetching prod snapshot (read-only)...")
        data = fetch_snapshot()
    else:
        data = json.loads(SNAPSHOT.read_text())
        print(f"using cached snapshot {SNAPSHOT} (pass --refresh to re-fetch)")

    # imported late so a bad prod fetch fails before any app/DB setup
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.routers.sales_audit as router_mod
    from app.db import Base, get_db
    from app.models import SalesAuditRun

    DB_FILE.unlink(missing_ok=True)
    engine = create_engine(f"sqlite:///{DB_FILE}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    keep = ("generated_at", "count", "rows", "summary", "source_errors", "rules", "billing_meta", "zoom_transcripts_indexed")
    with session_factory() as db:
        db.add(SalesAuditRun(run_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
                             report_json=json.dumps({k: data[k] for k in keep if k in data})))
        db.commit()

    app = FastAPI(title="Sales Audit (local preview)")
    app.include_router(router_mod.router)
    app.dependency_overrides[get_db] = lambda: session_factory()
    print(f"\n  {len(data['rows'])} deals from prod run {data.get('run_id')} ({data.get('run_at')})")
    print(f"  open  http://127.0.0.1:{args.port}/reports/sales-audit   (Ctrl-C to stop)\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
