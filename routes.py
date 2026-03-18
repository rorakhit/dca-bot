"""
routes.py — All FastAPI route handlers (except /health, which is in app.py).
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from approval import handle_approval, handle_denial, pending_approvals
from audit import read_audit_log
from broker import get_portfolio_state
from dashboard import DASHBOARD_HTML, LANDING_HTML
from scheduler_jobs import handle_contribution

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def landing_page():
    """Desktop-optimized portfolio dashboard."""
    return HTMLResponse(LANDING_HTML)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Full-page portfolio dashboard — designed for phone/tablet viewing."""
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/portfolio")
def portfolio_snapshot():
    return get_portfolio_state()


@router.get("/pending")
def list_pending():
    """See what approvals are currently waiting."""
    return {k[:8]: v for k, v in pending_approvals.items()}


@router.get("/audit")
def audit_log():
    """Return parsed audit log entries as JSON, newest first."""
    return read_audit_log()


@router.post("/contribute")
async def manual_contribution(amount: float, dry_run: bool = True):
    """POST /contribute?amount=100&dry_run=true"""
    await handle_contribution(new_cash=amount, dry_run=dry_run)
    return {"status": "done", "dry_run": dry_run}


@router.get("/approve/{token}", response_class=HTMLResponse)
async def approve(token: str):
    """User clicks Approve in email -> orders execute immediately."""
    result = handle_approval(token)
    if result is None:
        raise HTTPException(status_code=404, detail="Token not found or already used.")
    return HTMLResponse(result)


@router.get("/deny/{token}", response_class=HTMLResponse)
async def deny(token: str):
    """User clicks Deny in email -> allocation discarded."""
    result = handle_denial(token)
    if result is None:
        raise HTTPException(status_code=404, detail="Token not found or already used.")
    return HTMLResponse(result)
