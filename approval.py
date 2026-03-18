"""
approval.py — Pending approvals store, approval email, approve/deny logic.
"""

import json
import secrets
from datetime import datetime

from audit import write_audit_entry
from broker import approval_deadline, execute_allocations, is_market_open
from config import ET, PENDING_STORE_PATH, SERVER_BASE_URL, log
from email_service import _send_email


# ─────────────────────────────────────────────
# PERSISTENT TOKEN STORE
# ─────────────────────────────────────────────

def _load_pending() -> dict:
    """Load pending_approvals from disk; returns {} if file missing or corrupt."""
    if not PENDING_STORE_PATH.exists():
        return {}
    try:
        return json.loads(PENDING_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read pending_approvals.json — starting fresh")
        return {}


def _save_pending(data: dict):
    """Atomically write pending_approvals to disk."""
    tmp = PENDING_STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(PENDING_STORE_PATH)


# In-memory store, hydrated from disk at startup
pending_approvals: dict[str, dict] = _load_pending()


# ─────────────────────────────────────────────
# APPROVAL EMAIL
# ─────────────────────────────────────────────

def send_approval_email(token: str, allocations: dict, reasoning: str,
                        new_cash: float, deadline: datetime):
    """Send approve/deny email with one-click links."""
    approve_url  = f"{SERVER_BASE_URL}/approve/{token}"
    deny_url     = f"{SERVER_BASE_URL}/deny/{token}"
    deadline_str = deadline.strftime("%-I:%M%p ET")

    rows = "".join(
        f"<tr><td style='padding:8px 12px'><strong>{sym}</strong></td>"
        f"<td style='padding:8px 12px'>${amt:.2f}</td></tr>"
        for sym, amt in allocations.items()
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             background:#f3f4f6;margin:0;padding:24px;color:#111827">
  <div style="max-width:520px;margin:0 auto">

    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);
                color:white;border-radius:12px;padding:24px;margin-bottom:16px">
      <h1 style="margin:0 0 4px;font-size:20px">📊 DCA Contribution Ready</h1>
      <p style="margin:0;opacity:.85;font-size:14px">
        ${new_cash:.2f} · Approve by {deadline_str}
      </p>
    </div>

    <div style="background:white;border-radius:12px;padding:24px;margin-bottom:12px;
                box-shadow:0 1px 3px rgba(0,0,0,0.08)">
      <h2 style="margin:0 0 12px;font-size:15px">Proposed allocation</h2>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr style="background:#f9fafb">
          <th style="padding:8px 12px;text-align:left;color:#6b7280;
                     font-size:11px;text-transform:uppercase">Symbol</th>
          <th style="padding:8px 12px;text-align:left;color:#6b7280;
                     font-size:11px;text-transform:uppercase">Amount</th>
        </tr>
        {rows}
      </table>
      <div style="background:#f5f3ff;border-left:4px solid #7c3aed;
                  padding:12px 16px;border-radius:0 8px 8px 0;
                  font-size:14px;color:#374151;margin-top:16px;line-height:1.6">
        {reasoning}
      </div>
    </div>

    <div style="display:flex;gap:12px;margin-bottom:16px">
      <a href="{approve_url}"
         style="flex:1;display:block;text-align:center;background:#10b981;
                color:white;padding:14px;border-radius:8px;font-weight:600;
                font-size:15px;text-decoration:none">
        ✅ Approve &amp; Execute
      </a>
      <a href="{deny_url}"
         style="flex:1;display:block;text-align:center;background:#f3f4f6;
                color:#374151;padding:14px;border-radius:8px;font-weight:600;
                font-size:15px;text-decoration:none;border:1px solid #e5e7eb">
        ✗ Deny
      </a>
    </div>

    <p style="text-align:center;font-size:12px;color:#9ca3af;margin:0">
      This approval expires at {deadline_str}. Live trading.
    </p>
  </div>
</body>
</html>"""

    _send_email(f"📊 DCA Bot — Approve ${new_cash:.0f} contribution?", html)
    log.info(f"Approval email sent — token {token[:8]}… expires {deadline_str}")


# ─────────────────────────────────────────────
# APPROVE / DENY LOGIC
# ─────────────────────────────────────────────

def _result_page(title: str, body: str, color: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,sans-serif;background:#f3f4f6;
             display:flex;align-items:center;justify-content:center;
             min-height:100vh;margin:0">
  <div style="background:white;border-radius:16px;padding:40px;
              max-width:400px;text-align:center;box-shadow:0 4px 12px rgba(0,0,0,0.1)">
    <div style="font-size:40px;margin-bottom:16px">{title.split()[0]}</div>
    <h2 style="margin:0 0 12px;color:{color}">{" ".join(title.split()[1:])}</h2>
    <div style="font-size:14px;color:#6b7280;line-height:1.6">{body}</div>
  </div>
</body></html>"""


def handle_approval(token: str) -> str:
    """User clicks Approve in email -> orders execute immediately. Returns HTML."""
    pending = pending_approvals.pop(token, None)
    if not pending:
        return None

    _save_pending(pending_approvals)

    if datetime.now(ET) > datetime.fromisoformat(pending["expires_at"]):
        return _result_page(
            "⏰ Expired",
            "This approval window has closed — market is near close.",
            "#f59e0b",
        )

    if not is_market_open():
        return _result_page(
            "🚫 Market Closed",
            "Orders can only be placed during market hours (9:30am–4pm ET).",
            "#ef4444",
        )

    receipts = execute_allocations(pending["allocations"], dry_run=False)
    write_audit_entry("orders_placed", {"receipts": receipts, "approved_by": "email_link"})

    rows = "".join(f"<li>${r['amount']:.2f} of {r['symbol']}</li>" for r in receipts)
    return _result_page(
        "✅ Orders Placed",
        f"<ul style='margin:8px 0;padding-left:20px'>{rows}</ul>",
        "#10b981",
    )


def handle_denial(token: str) -> str:
    """User clicks Deny in email -> allocation discarded. Returns HTML."""
    pending = pending_approvals.pop(token, None)
    if not pending:
        return None

    _save_pending(pending_approvals)

    write_audit_entry("allocation_rejected", {
        "allocations": pending["allocations"],
        "rejected_by": "email_link",
    })
    log.info(f"Allocation denied via email — token {token[:8]}…")
    return _result_page(
        "✗ Denied",
        "The allocation was discarded. No orders were placed.",
        "#6b7280",
    )


def create_pending_approval(allocations: dict, reasoning: str, new_cash: float) -> str:
    """Create a new pending approval token and persist it. Returns the token."""
    token    = secrets.token_urlsafe(32)
    deadline = approval_deadline()

    pending_approvals[token] = {
        "allocations": allocations,
        "reasoning":   reasoning,
        "new_cash":    new_cash,
        "expires_at":  deadline.isoformat(),
    }
    _save_pending(pending_approvals)

    send_approval_email(token, allocations, reasoning, new_cash, deadline)
    write_audit_entry("approval_email_sent", {
        "token_prefix": token[:8],
        "expires_at":   deadline.isoformat(),
    })

    return token
