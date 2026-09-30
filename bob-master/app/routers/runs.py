"""
Manual trigger page for Pulse and CMDCTR runs (2026-09-30) -- a UI over the
existing job_id/poll endpoints (app/routers/atlas_report.py,
app/routers/cmdctr_report.py), not a new backend surface. Plain
server-rendered shell + vanilla JS polling, same convention as every other
page in app/templates/ -- no JS framework.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")


@router.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "runs.html", {})
