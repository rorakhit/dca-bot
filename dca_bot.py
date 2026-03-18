"""
dca_bot.py — Personal DCA Allocation Bot

Portfolio (three-fund + small-cap value tilt):
  VTI  50% — Total US market
  VXUS 35% — International
  AVUV 15% — US small-cap value (factor tilt)

Contribution flow (live):
  1. Scheduler fires at 10am on the 1st and 16th (day after payday)
  2. Checks Alpaca calendar — skips if market is closed or it's a holiday
  3. Fetches portfolio state from Alpaca
  4. Asks AI how to allocate the $100 contribution
  5. Emails an approve/deny link to your inbox
  6. You click approve → orders execute immediately
  7. Pending approvals expire at 3:30pm ET if not acted on

Contribution flow (dry run):
  POST /contribute?amount=100&dry_run=true
  → Runs steps 1-4, logs the proposal, skips email and orders

Endpoints:
  GET  /dashboard  → mobile-friendly portfolio dashboard
  GET  /health     → server status, next scheduled run, pending approvals count
  GET  /portfolio  → current holdings and allocation JSON
  GET  /audit      → full audit log as JSON
  POST /contribute → manually trigger a contribution cycle

Dependencies:
    pip install alpaca-py anthropic apscheduler fastapi uvicorn python-dotenv --break-system-packages
"""

import json
import logging
import logging.handlers
import os
import secrets
import resend
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# ─────────────────────────────────────────────
# LOAD CREDENTIALS FROM .env
# ─────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

ALPACA_API_KEY     = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY  = os.environ["ALPACA_SECRET_KEY"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
NOTIFY_EMAIL       = os.environ["NOTIFY_EMAIL"]
resend.api_key     = os.environ["RESEND_API_KEY"]
EMAIL_FROM         = os.environ.get("EMAIL_FROM", "DCA Bot <onboarding@resend.dev>")

# ─────────────────────────────────────────────
# CONFIG (non-secret)
# ─────────────────────────────────────────────

# The URL approve/deny links point to. Must be reachable from your phone/laptop.
SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "https://dca-bot.up.railway.app")

# Target portfolio weights — must sum to 1.0
TARGET_ALLOCATION = {
    "VTI":  0.50,   # Total US market
    "VXUS": 0.35,   # International
    "AVUV": 0.15,   # US small-cap value (factor tilt)
}

# How much to deploy each contribution cycle
CONTRIBUTION_AMOUNT = 100.00

# Hard safety rails — never touched by AI
MAX_SINGLE_ORDER_USD = 10_000
MIN_ORDER_USD        = 1.00

ET = ZoneInfo("America/New_York")

BASE_DIR           = Path(__file__).parent
AUDIT_LOG_PATH     = BASE_DIR / "audit_log.jsonl"
PENDING_STORE_PATH = BASE_DIR / "pending_approvals.json"
LOG_DIR            = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# LOGGING — rotating file + console
# ─────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Rotate at 1 MB, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "dca_bot.log", maxBytes=1_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    logger = logging.getLogger("dca_bot")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

log = _setup_logging()

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
# CLIENTS
# ─────────────────────────────────────────────

broker    = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)  # paper=False for live
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
scheduler = AsyncIOScheduler(timezone=ET)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    log.info("Scheduler started — jobs: contribution@10:00 on 1st/16th, expire@15:30")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ─────────────────────────────────────────────
# MARKET HOURS & HOLIDAY DETECTION
# ─────────────────────────────────────────────

def is_trading_day(check_date: date | None = None) -> bool:
    """
    Returns True if NYSE is open on check_date (defaults to today).
    Uses Alpaca's calendar API so holidays are always correct.
    Falls back to weekday check if the API call fails.
    """
    if check_date is None:
        check_date = datetime.now(ET).date()

    try:
        calendars = broker.get_calendar(
            GetCalendarRequest(start=check_date, end=check_date)
        )
        return len(calendars) > 0
    except Exception as exc:
        log.warning(f"Calendar API failed ({exc}) — falling back to weekday check")
        return check_date.weekday() < 5  # Mon-Fri


def is_market_open() -> bool:
    """
    Returns True if NYSE is currently open (trading day + within hours).
    9:30am–4:00pm ET, holidays excluded.
    """
    now = datetime.now(ET)
    if not is_trading_day(now.date()):
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now < market_close


def approval_deadline() -> datetime:
    """3:30pm ET today — 30 min before close, last sensible time to approve."""
    return datetime.now(ET).replace(hour=15, minute=30, second=0, microsecond=0)


# ─────────────────────────────────────────────
# PORTFOLIO STATE
# ─────────────────────────────────────────────

def get_portfolio_state() -> dict:
    """Fetch current holdings and cash from Alpaca."""
    account   = broker.get_account()
    positions = broker.get_all_positions()

    total_value = float(account.portfolio_value)
    cash        = float(account.cash)

    holdings = {}
    for pos in positions:
        holdings[pos.symbol] = {
            "market_value":  float(pos.market_value),
            "weight":        float(pos.market_value) / total_value if total_value > 0 else 0,
            "unrealized_pl": float(pos.unrealized_pl),
        }

    drift = {
        symbol: round(holdings.get(symbol, {}).get("weight", 0) - target, 4)
        for symbol, target in TARGET_ALLOCATION.items()
    }

    return {
        "total_value":       total_value,
        "cash_available":    cash,
        "holdings":          holdings,
        "target_allocation": TARGET_ALLOCATION,
        "drift_from_target": drift,
    }


# ─────────────────────────────────────────────
# AI ALLOCATION ENGINE
# ─────────────────────────────────────────────

def ask_ai_for_allocation(portfolio: dict, new_cash: float) -> dict:
    """
    Ask Claude how to allocate new_cash to minimise drift from target weights.
    Retries up to 3 times with backoff on API overload (HTTP 529).
    """
    prompt = f"""You are managing a personal investment portfolio using a dollar-cost averaging strategy.
A new cash contribution of ${new_cash:.2f} has arrived and needs to be allocated.

Current portfolio state:
{json.dumps(portfolio, indent=2)}

Your job is to allocate the ${new_cash:.2f} across the target assets to bring the portfolio
closer to its target allocation. Prioritise the most underweight assets (most negative drift).

Rules:
- Only allocate to symbols in: {list(TARGET_ALLOCATION.keys())}
- Allocations must sum to exactly ${new_cash:.2f}
- Minimum order size is ${MIN_ORDER_USD}
- Briefly explain your reasoning

Respond ONLY with valid JSON — no markdown, no code fences:
{{
  "allocations": {{"SYMBOL": dollar_amount, ...}},
  "reasoning": "one or two sentences"
}}
"""
    for attempt in range(3):
        try:
            response = ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < 2:
                wait = 10 * (attempt + 1)
                log.warning(f"Anthropic overloaded — retrying in {wait}s")
                time.sleep(wait)
            else:
                raise

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw.strip())
    log.info(f"AI reasoning: {result['reasoning']}")
    return result


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def _send_email(subject: str, html_body: str):
    """Send email via Resend HTTP API. Raises on failure."""
    resend.Emails.send({
        "from":    EMAIL_FROM,
        "to":      [NOTIFY_EMAIL],
        "subject": subject,
        "html":    html_body,
    })


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


def send_error_email(context: str, error: Exception):
    """
    Send a plain-text error notification so failures don't go unnoticed.
    Swallows its own exceptions so a broken SMTP config doesn't cause a double-fault.
    """
    try:
        html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,sans-serif;padding:24px;color:#111827">
  <div style="max-width:520px;background:#fef2f2;border:1px solid #fecaca;
              border-radius:12px;padding:24px">
    <h2 style="color:#dc2626;margin:0 0 12px">⚠️ DCA Bot Error</h2>
    <p style="margin:0 0 8px"><strong>Context:</strong> {context}</p>
    <pre style="background:#fff;border-radius:8px;padding:12px;font-size:12px;
                overflow-x:auto;white-space:pre-wrap">{type(error).__name__}: {error}</pre>
    <p style="font-size:12px;color:#6b7280;margin:12px 0 0">
      Check <code>logs/dca_bot.log</code> for the full traceback.
    </p>
  </div>
</body></html>"""
        _send_email("⚠️ DCA Bot — Error notification", html)
        log.info(f"Error notification sent for: {context}")
    except Exception as e:
        log.error(f"Could not send error email: {e}")


# ─────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────

def execute_allocations(allocations: dict, dry_run: bool = False) -> list[dict]:
    """Place notional market orders. dry_run=True logs only, no orders sent."""
    receipts = []

    for symbol, dollar_amount in allocations.items():
        if dollar_amount < MIN_ORDER_USD:
            log.info(f"Skipping {symbol} — ${dollar_amount:.2f} below minimum")
            continue

        dollar_amount = min(dollar_amount, MAX_SINGLE_ORDER_USD)

        if dry_run:
            log.info(f"[DRY RUN] Would buy ${dollar_amount:.2f} of {symbol}")
            receipts.append({"symbol": symbol, "amount": dollar_amount, "status": "dry_run"})
        else:
            order = broker.submit_order(MarketOrderRequest(
                symbol=symbol,
                notional=round(dollar_amount, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"Order placed: ${dollar_amount:.2f} of {symbol} — {order.id}")
            receipts.append({
                "symbol":   symbol,
                "amount":   dollar_amount,
                "order_id": order.id,
                "status":   str(order.status),
            })

    return receipts


# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────

def write_audit_entry(event: str, data: dict):
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─────────────────────────────────────────────
# CONTRIBUTION HANDLER
# ─────────────────────────────────────────────

async def handle_contribution(new_cash: float, dry_run: bool = False):
    """
    Core flow for each contribution event.

    dry_run=True  → propose allocation, log it, return. No email, no orders.
    dry_run=False → propose allocation, send approval email, wait for click.
    """
    log.info(f"Contribution event: ${new_cash:.2f} | dry_run={dry_run}")

    try:
        portfolio = get_portfolio_state()
        write_audit_entry("portfolio_snapshot", portfolio)

        ai_response = ask_ai_for_allocation(portfolio, new_cash)
        allocations = ai_response["allocations"]
        reasoning   = ai_response["reasoning"]
        write_audit_entry("ai_allocation_proposed", {
            "allocations": allocations,
            "reasoning":   reasoning,
            "new_cash":    new_cash,
        })

        if dry_run:
            log.info("Dry run — skipping email and order execution")
            log.info(f"Proposed: {allocations}")
            return

        # Send approval email and persist the pending token
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

    except Exception as exc:
        log.exception(f"handle_contribution failed: {exc}")
        write_audit_entry("contribution_error", {"error": str(exc), "new_cash": new_cash})
        send_error_email(f"handle_contribution(${new_cash:.2f}, dry_run={dry_run})", exc)
        raise


# ─────────────────────────────────────────────
# APPROVAL ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/approve/{token}", response_class=HTMLResponse)
async def approve(token: str):
    """User clicks Approve in email → orders execute immediately."""
    pending = pending_approvals.pop(token, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Token not found or already used.")

    _save_pending(pending_approvals)

    if datetime.now(ET) > datetime.fromisoformat(pending["expires_at"]):
        return HTMLResponse(_result_page(
            "⏰ Expired",
            "This approval window has closed — market is near close.",
            "#f59e0b",
        ))

    if not is_market_open():
        return HTMLResponse(_result_page(
            "🚫 Market Closed",
            "Orders can only be placed during market hours (9:30am–4pm ET).",
            "#ef4444",
        ))

    receipts = execute_allocations(pending["allocations"], dry_run=False)
    write_audit_entry("orders_placed", {"receipts": receipts, "approved_by": "email_link"})

    rows = "".join(f"<li>${r['amount']:.2f} of {r['symbol']}</li>" for r in receipts)
    return HTMLResponse(_result_page(
        "✅ Orders Placed",
        f"<ul style='margin:8px 0;padding-left:20px'>{rows}</ul>",
        "#10b981",
    ))


@app.get("/deny/{token}", response_class=HTMLResponse)
async def deny(token: str):
    """User clicks Deny in email → allocation discarded."""
    pending = pending_approvals.pop(token, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Token not found or already used.")

    _save_pending(pending_approvals)

    write_audit_entry("allocation_rejected", {
        "allocations": pending["allocations"],
        "rejected_by": "email_link",
    })
    log.info(f"Allocation denied via email — token {token[:8]}…")
    return HTMLResponse(_result_page(
        "✗ Denied",
        "The allocation was discarded. No orders were placed.",
        "#6b7280",
    ))


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


# ─────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────

@scheduler.scheduled_job("cron", day="1,16", hour=10, minute=0)
async def scheduled_contribution():
    """Fires at 10am ET on the 1st and 16th (day after payday)."""
    today = datetime.now(ET).date()

    if not is_trading_day(today):
        log.info(f"Skipping contribution — {today} is a holiday or weekend")
        return

    account        = broker.get_account()
    available_cash = float(account.cash)

    if available_cash < CONTRIBUTION_AMOUNT:
        log.info(f"Insufficient cash (${available_cash:.2f}) — skipping cycle")
        send_error_email(
            "scheduled_contribution",
            RuntimeError(f"Insufficient cash: ${available_cash:.2f} < ${CONTRIBUTION_AMOUNT:.2f}"),
        )
        return

    await handle_contribution(new_cash=CONTRIBUTION_AMOUNT, dry_run=False)


@scheduler.scheduled_job("cron", day="1,16", hour=15, minute=30)
async def expire_pending_approvals():
    """Cleans up any tokens the user didn't act on before 3:30pm ET."""
    expired = [
        t for t, v in pending_approvals.items()
        if datetime.now(ET) > datetime.fromisoformat(v["expires_at"])
    ]
    for token in expired:
        data = pending_approvals.pop(token)
        write_audit_entry("approval_expired", {
            "token_prefix": token[:8],
            "allocations":  data["allocations"],
        })
        log.info(f"Approval expired — token {token[:8]}…")

    if expired:
        _save_pending(pending_approvals)


@scheduler.scheduled_job("cron", day="15,last", hour=9, minute=0)
def contribution_reminder():
    """Remind to fund Alpaca on the 15th and last day of the month."""
    today = datetime.now(ET).strftime("%B %d")
    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,sans-serif;padding:24px;color:#111827">
  <div style="max-width:520px;background:#eff6ff;border:1px solid #bfdbfe;
              border-radius:12px;padding:24px">
    <h2 style="color:#2563eb;margin:0 0 12px">💰 DCA Reminder — {today}</h2>
    <p style="margin:0 0 8px">
      Time to transfer <strong>$100</strong> into your Alpaca account so the
      bot can invest it on the next contribution day (1st or 16th).
    </p>
    <p style="font-size:13px;color:#6b7280;margin:12px 0 0">
      The bot will automatically invest once the cash is available.
    </p>
  </div>
</body></html>"""
    _send_email("💰 DCA Bot — Fund your account ($100)", html)
    log.info("Contribution reminder email sent")


@scheduler.scheduled_job("cron", day="1,16", hour=12, minute=0)
def dca_contribution_report():
    """Generate and email a portfolio report at noon ET on 1st/16th."""
    import base64, io, json as _json
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    account = broker.get_account()
    positions = broker.get_all_positions()

    total = float(account.portfolio_value)
    cash = float(account.cash)

    holdings = {}
    for p in positions:
        holdings[p.symbol] = {
            "market_value": float(p.market_value),
            "weight": float(p.market_value) / total if total > 0 else 0,
            "unrealized_pl": float(p.unrealized_pl),
        }

    symbols = list(TARGET_ALLOCATION.keys())
    colors = ["#4f46e5", "#06b6d4", "#10b981"]
    drift = {s: round(holdings.get(s, {}).get("weight", 0) - t, 4) for s, t in TARGET_ALLOCATION.items()}
    total_pl = sum(h["unrealized_pl"] for h in holdings.values())

    def fig_to_b64(fig):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor="white")
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

    # Chart 1: Side-by-side donuts
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 5))
    fig.patch.set_facecolor("white")
    current_weights = [holdings.get(s, {}).get("weight", 0) for s in symbols]
    target_weights = [TARGET_ALLOCATION[s] for s in symbols]

    def donut(ax, values, title, note=None):
        display = values if any(v > 0 for v in values) else target_weights
        wedges, texts, autotexts = ax.pie(
            display, labels=symbols, colors=colors, autopct="%1.0f%%",
            startangle=90, pctdistance=0.75,
            wedgeprops=dict(width=0.5, edgecolor="white", linewidth=2),
        )
        for t in texts: t.set_fontsize(11)
        for at in autotexts: at.set_fontsize(9); at.set_color("white"); at.set_fontweight("bold")
        ax.set_title(title, fontsize=13, fontweight="bold", pad=15)
        if note:
            ax.text(0, -1.35, note, ha="center", fontsize=9, color="#9ca3af", style="italic")

    note = "No positions yet" if not any(v > 0 for v in current_weights) else None
    donut(ax1, current_weights, "Current Allocation", note)
    donut(ax2, target_weights, "Target Allocation")
    chart1 = fig_to_b64(fig)
    plt.close(fig)

    # Chart 2: Drift bar chart
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor("white")
    drift_vals = [drift[s] * 100 for s in symbols]
    bar_colors = ["#ef4444" if d > 0.5 else "#3b82f6" if d < -0.5 else "#10b981" for d in drift_vals]
    bars = ax.barh(symbols, drift_vals, color=bar_colors, height=0.5, edgecolor="white")
    ax.axvline(0, color="#6b7280", linewidth=1.5, linestyle="--")
    ax.set_xlabel("Drift from Target (% points)", fontsize=11)
    ax.set_title("Portfolio Drift from Target", fontsize=13, fontweight="bold")
    ax.set_facecolor("#f9fafb")
    for bar, val in zip(bars, drift_vals):
        ha = "left" if val >= 0 else "right"
        offset = 0.3 if val >= 0 else -0.3
        ax.text(val + offset, bar.get_y() + bar.get_height() / 2,
                f"{val:+.1f}%", va="center", ha=ha, fontsize=10, fontweight="bold")
    legend = [mpatches.Patch(color="#ef4444", label="Overweight"),
              mpatches.Patch(color="#3b82f6", label="Underweight"),
              mpatches.Patch(color="#10b981", label="On target")]
    ax.legend(handles=legend, loc="lower right", fontsize=9)
    plt.tight_layout()
    chart2 = fig_to_b64(fig)
    plt.close(fig)

    # Read latest AI proposal from audit log
    ai_reasoning = "No AI proposal found for this cycle."
    ai_allocations = {}
    if AUDIT_LOG_PATH.exists():
        entries = [_json.loads(l) for l in AUDIT_LOG_PATH.read_text().splitlines() if l.strip()]
        proposals = [e for e in entries if e["event"] == "ai_allocation_proposed"]
        if proposals:
            latest = proposals[-1]
            ai_reasoning = latest["reasoning"]
            ai_allocations = latest["allocations"]

    # Build HTML email
    date_str = datetime.now(ET).strftime("%B %-d, %Y")
    pl_color = "#10b981" if total_pl >= 0 else "#ef4444"
    pl_sign = "+" if total_pl >= 0 else ""

    holdings_rows = ""
    for symbol in symbols:
        h = holdings.get(symbol, {})
        mv = h.get("market_value", 0)
        w = h.get("weight", 0) * 100
        upl = h.get("unrealized_pl", 0)
        d = drift[symbol] * 100
        pill = "red" if d > 0.5 else ("blue" if d < -0.5 else "green")
        upl_color = "#10b981" if upl >= 0 else "#ef4444"
        holdings_rows += f"""<tr>
          <td><strong>{symbol}</strong></td><td>${mv:,.2f}</td><td>{w:.1f}%</td>
          <td>{int(TARGET_ALLOCATION[symbol]*100)}%</td>
          <td><span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;
            {'background:#fee2e2;color:#991b1b' if pill == 'red' else 'background:#dbeafe;color:#1e40af' if pill == 'blue' else 'background:#d1fae5;color:#065f46'}">{d:+.1f}%</span></td>
          <td style="color:{upl_color}">${upl:+,.2f}</td></tr>"""

    alloc_rows = ""
    for sym, amt in ai_allocations.items():
        alloc_rows += f'<tr><td><strong>{sym}</strong></td><td>${float(amt):.2f}</td></tr>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f3f4f6;margin:0;padding:20px;color:#111827}}
  .wrap{{max-width:680px;margin:0 auto}}
  .card{{background:white;border-radius:12px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,0.08)}}
  .header{{background:linear-gradient(135deg,#4f46e5,#7c3aed);color:white;border-radius:12px;padding:28px;margin-bottom:16px}}
  .header h1{{margin:0 0 4px;font-size:22px;font-weight:700}}
  .header p{{margin:0;opacity:.85;font-size:14px}}
  .stat-row{{display:flex;gap:12px}}
  .stat{{flex:1;background:#f9fafb;border-radius:8px;padding:16px;text-align:center}}
  .stat .value{{font-size:20px;font-weight:700;color:#111827}}
  .stat .label{{font-size:12px;color:#6b7280;margin-top:4px}}
  h2{{margin:0 0 16px;font-size:16px;color:#111827}}
  table{{width:100%;border-collapse:collapse;font-size:14px}}
  th{{background:#f9fafb;padding:10px 12px;text-align:left;color:#6b7280;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
  td{{padding:10px 12px;border-bottom:1px solid #f3f4f6}}
  tr:last-child td{{border-bottom:none}}
  .ai-box{{background:#f5f3ff;border-left:4px solid #7c3aed;padding:16px;border-radius:0 8px 8px 0;font-size:14px;color:#374151;line-height:1.6;margin-bottom:16px}}
  .footer{{text-align:center;font-size:12px;color:#9ca3af;margin-top:8px;padding-bottom:20px}}
  img{{max-width:100%;border-radius:8px;display:block}}
</style></head>
<body>
<div class="wrap">
  <div class="header">
    <h1>📊 DCA Portfolio Report</h1>
    <p>{date_str}</p>
  </div>
  <div class="card">
    <div class="stat-row">
      <div class="stat"><div class="value">${total:,.2f}</div><div class="label">Portfolio Value</div></div>
      <div class="stat"><div class="value">${cash:,.2f}</div><div class="label">Cash Available</div></div>
      <div class="stat"><div class="value" style="color:{pl_color}">{pl_sign}${total_pl:,.2f}</div><div class="label">Unrealized P&amp;L</div></div>
    </div>
  </div>
  <div class="card">
    <h2>Allocation Charts</h2>
    <img src="data:image/png;base64,{chart1}" alt="Allocation donut charts"/>
    <img src="data:image/png;base64,{chart2}" alt="Drift chart" style="margin-top:12px"/>
  </div>
  <div class="card">
    <h2>Holdings</h2>
    <table>
      <tr><th>Symbol</th><th>Value</th><th>Current</th><th>Target</th><th>Drift</th><th>P&amp;L</th></tr>
      {holdings_rows}
    </table>
  </div>
  <div class="card">
    <h2>🤖 This Cycle's AI Allocation</h2>
    <div class="ai-box">{ai_reasoning}</div>
    <table>
      <tr><th>Symbol</th><th>Allocated</th></tr>
      {alloc_rows}
    </table>
  </div>
  <div class="footer">DCA Bot &nbsp;·&nbsp; Runs 1st &amp; 16th each month</div>
</div>
</body></html>"""

    _send_email(f"📊 DCA Bot Report — {date_str}", html)
    log.info("DCA contribution report email sent")


# ─────────────────────────────────────────────
# HEALTH ENDPOINT
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    """
    Quick status check. Useful for monitoring.

    Returns:
    - status: "ok" if Alpaca and AI are reachable
    - market_open: whether NYSE is currently open
    - trading_day: whether today is a trading day
    - pending_approvals: number of tokens awaiting click
    - next_contribution: next scheduled contribution datetime
    - account_value: current portfolio value in USD
    """
    errors = []
    account_value = None

    try:
        account = broker.get_account()
        account_value = float(account.portfolio_value)
    except Exception as e:
        errors.append(f"Alpaca: {e}")

    # Find next contribution job run time
    job = scheduler.get_job("scheduled_contribution")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None

    return JSONResponse({
        "status":             "ok" if not errors else "degraded",
        "errors":             errors,
        "market_open":        is_market_open(),
        "trading_day":        is_trading_day(),
        "pending_approvals":  len(pending_approvals),
        "next_contribution":  next_run,
        "account_value_usd":  account_value,
        "server_time_et":     datetime.now(ET).isoformat(),
    })


# ─────────────────────────────────────────────
# MANUAL / DEBUG ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/contribute")
async def manual_contribution(amount: float, dry_run: bool = True):
    """POST /contribute?amount=100&dry_run=true"""
    await handle_contribution(new_cash=amount, dry_run=dry_run)
    return {"status": "done", "dry_run": dry_run}


@app.get("/portfolio")
def portfolio_snapshot():
    return get_portfolio_state()


@app.get("/pending")
def list_pending():
    """See what approvals are currently waiting."""
    return {k[:8]: v for k, v in pending_approvals.items()}


@app.get("/audit")
def audit_log():
    """Return parsed audit log entries as JSON, newest first."""
    if not AUDIT_LOG_PATH.exists():
        return []
    entries = []
    for line in AUDIT_LOG_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return list(reversed(entries))


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Full-page portfolio dashboard — designed for phone/tablet viewing."""
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>DCA Portfolio</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #0f0f13;
      color: #e2e8f0;
      min-height: 100vh;
      padding: 16px;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;
    }
    header h1 { font-size: 18px; font-weight: 700; }
    #refresh-btn {
      background: #1e1e2e;
      border: 1px solid #2d2d3d;
      color: #a0aec0;
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 13px;
      cursor: pointer;
    }
    #refresh-btn:hover { background: #2d2d3d; }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
      padding: 3px 10px;
      border-radius: 99px;
      font-weight: 600;
    }
    .pill.green  { background: #064e3b; color: #34d399; }
    .pill.red    { background: #450a0a; color: #f87171; }
    .pill.yellow { background: #451a03; color: #fbbf24; }

    .card {
      background: #1a1a2e;
      border: 1px solid #2d2d3d;
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 14px;
    }
    .card-title {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: #64748b;
      margin-bottom: 10px;
    }

    .stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .stat { }
    .stat .label { font-size: 11px; color: #64748b; margin-bottom: 2px; }
    .stat .value { font-size: 22px; font-weight: 700; }
    .stat .value.green { color: #34d399; }
    .stat .value.red   { color: #f87171; }

    .allocation-row {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .alloc-symbol { font-weight: 700; width: 42px; font-size: 14px; }
    .alloc-bar-wrap { flex: 1; background: #0f0f13; border-radius: 99px; height: 8px; overflow: hidden; }
    .alloc-bar { height: 100%; border-radius: 99px; transition: width .5s; }
    .alloc-pct { font-size: 13px; color: #94a3b8; width: 36px; text-align: right; }
    .alloc-target { font-size: 11px; color: #475569; width: 42px; text-align: right; }
    .drift-badge {
      font-size: 11px; font-weight: 600; width: 48px; text-align: right;
    }
    .drift-badge.over  { color: #f87171; }
    .drift-badge.under { color: #60a5fa; }
    .drift-badge.on    { color: #34d399; }

    .chart-wrap { position: relative; height: 200px; }
    .chart-wrap-sm { position: relative; height: 160px; }

    .contribution-list { list-style: none; }
    .contribution-item {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding: 12px 0;
      border-bottom: 1px solid #1e2035;
      gap: 8px;
    }
    .contribution-item:last-child { border-bottom: none; }
    .contrib-left { flex: 1; }
    .contrib-date { font-size: 12px; color: #64748b; margin-bottom: 2px; }
    .contrib-alloc { font-size: 13px; }
    .contrib-alloc span { color: #a78bfa; font-weight: 600; }
    .contrib-reasoning {
      font-size: 11px;
      color: #475569;
      margin-top: 3px;
      line-height: 1.4;
    }
    .contrib-right { font-size: 14px; font-weight: 700; color: #e2e8f0; white-space: nowrap; }

    .event-list { list-style: none; }
    .event-item {
      display: flex;
      gap: 10px;
      padding: 9px 0;
      border-bottom: 1px solid #1e2035;
      font-size: 12px;
      align-items: flex-start;
    }
    .event-item:last-child { border-bottom: none; }
    .event-dot {
      width: 8px; height: 8px; border-radius: 50%;
      margin-top: 3px; flex-shrink: 0;
    }
    .event-dot.green  { background: #34d399; }
    .event-dot.red    { background: #f87171; }
    .event-dot.blue   { background: #60a5fa; }
    .event-dot.purple { background: #a78bfa; }
    .event-dot.gray   { background: #64748b; }
    .event-time { color: #475569; flex-shrink: 0; }
    .event-text { color: #94a3b8; line-height: 1.4; }

    .status-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }

    .loading { color: #475569; font-size: 14px; text-align: center; padding: 24px; }
    .error   { color: #f87171; font-size: 13px; text-align: center; padding: 16px; }

    #last-updated { font-size: 11px; color: #475569; text-align: center; margin-top: 8px; }
  </style>
</head>
<body>

<header>
  <div>
    <h1>📊 DCA Portfolio</h1>
  </div>
  <button id="refresh-btn" onclick="loadAll()">↻ Refresh</button>
</header>

<div class="status-bar" id="status-bar">
  <span class="loading">Loading…</span>
</div>

<!-- ── PORTFOLIO VALUE ── -->
<div class="card">
  <div class="card-title">Portfolio value</div>
  <div class="stat-grid" id="stats">
    <div class="loading">…</div>
  </div>
</div>

<!-- ── ALLOCATION ── -->
<div class="card">
  <div class="card-title">Current allocation vs target</div>
  <div id="allocation-rows"><div class="loading">…</div></div>
</div>

<!-- ── PORTFOLIO VALUE CHART ── -->
<div class="card">
  <div class="card-title">Portfolio value over time</div>
  <div class="chart-wrap"><canvas id="valueChart"></canvas></div>
</div>

<!-- ── ALLOCATION DRIFT CHART ── -->
<div class="card">
  <div class="card-title">Allocation drift history</div>
  <div class="chart-wrap-sm"><canvas id="driftChart"></canvas></div>
</div>

<!-- ── CONTRIBUTION HISTORY ── -->
<div class="card">
  <div class="card-title">Contributions</div>
  <ul class="contribution-list" id="contributions">
    <li class="loading">…</li>
  </ul>
</div>

<!-- ── EVENT LOG ── -->
<div class="card">
  <div class="card-title">Recent activity</div>
  <ul class="event-list" id="event-log">
    <li class="loading">…</li>
  </ul>
</div>

<div id="last-updated"></div>

<script>
// ── Palette ──────────────────────────────────────────────
const COLORS = {
  VTI:  '#818cf8',
  VXUS: '#34d399',
  BND:  '#fbbf24',
  VNQ:  '#f87171',
  default: ['#818cf8','#34d399','#fbbf24','#f87171','#60a5fa','#a78bfa'],
};
function colorFor(sym, i) {
  return COLORS[sym] || COLORS.default[i % COLORS.default.length];
}

// ── Charts ───────────────────────────────────────────────
let valueChart = null;
let driftChart = null;

Chart.defaults.color = '#64748b';
Chart.defaults.borderColor = '#1e2035';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

function mkValueChart(labels, values) {
  const ctx = document.getElementById('valueChart');
  if (valueChart) valueChart.destroy();

  const grad = ctx.getContext('2d').createLinearGradient(0,0,0,200);
  grad.addColorStop(0,  'rgba(129,140,248,0.3)');
  grad.addColorStop(1,  'rgba(129,140,248,0)');

  valueChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: '#818cf8',
        backgroundColor: grad,
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: values.length < 15 ? 4 : 0,
        pointBackgroundColor: '#818cf8',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 6, maxRotation: 0 } },
        y: {
          grid: { color: '#1e2035' },
          ticks: {
            callback: v => '$' + (v >= 1000 ? (v/1000).toFixed(0)+'k' : v.toLocaleString()),
          },
        },
      },
    },
  });
}

function mkDriftChart(labels, symbolData) {
  const ctx = document.getElementById('driftChart');
  if (driftChart) driftChart.destroy();

  const symbols = Object.keys(symbolData);
  driftChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: symbols.map((sym, i) => ({
        label: sym,
        data: symbolData[sym],
        borderColor: colorFor(sym, i),
        borderWidth: 2,
        fill: false,
        tension: 0.4,
        pointRadius: 0,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { boxWidth: 10, padding: 12, font: { size: 11 } },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 5, maxRotation: 0 } },
        y: {
          grid: { color: '#1e2035' },
          ticks: { callback: v => (v * 100).toFixed(0) + '%' },
        },
      },
    },
  });
}

// ── Helpers ──────────────────────────────────────────────
function fmt(n)  { return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtTs(ts) {
  const d = new Date(ts);
  return d.toLocaleDateString('en-US',{month:'short',day:'numeric'}) + ' '
       + d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});
}
function fmtDateShort(ts) {
  return new Date(ts).toLocaleDateString('en-US',{month:'short',day:'numeric'});
}

// ── Render ───────────────────────────────────────────────
function renderPortfolio(p, health) {
  // status bar
  const bar = document.getElementById('status-bar');
  const marketPill = health.market_open
    ? '<span class="pill green">● Market open</span>'
    : health.trading_day
      ? '<span class="pill yellow">● After hours</span>'
      : '<span class="pill red">● Market closed</span>';
  const modePill = '<span class="pill" style="background:#1e1e2e;color:#a0aec0">Live</span>';
  const nextPill = health.next_contribution
    ? `<span class="pill" style="background:#1e1e2e;color:#a0aec0">Next: ${fmtTs(health.next_contribution)}</span>`
    : '';
  bar.innerHTML = marketPill + modePill + nextPill;

  // stats
  const plClass = (p.total_value - p.cash_available) >= 0 ? 'green' : 'red';
  const invested = p.total_value - p.cash_available;
  document.getElementById('stats').innerHTML = `
    <div class="stat">
      <div class="label">Total value</div>
      <div class="value">${fmt(p.total_value)}</div>
    </div>
    <div class="stat">
      <div class="label">Cash available</div>
      <div class="value">${fmt(p.cash_available)}</div>
    </div>
    <div class="stat" style="margin-top:8px">
      <div class="label">Invested</div>
      <div class="value ${plClass}">${fmt(invested)}</div>
    </div>
    <div class="stat" style="margin-top:8px">
      <div class="label">Unrealised P&L</div>
      <div class="value ${plClass}">${fmt(Object.values(p.holdings).reduce((s,h)=>s+h.unrealized_pl,0))}</div>
    </div>
  `;

  // allocation rows
  const symbols = Object.keys(p.target_allocation);
  const rows = symbols.map((sym, i) => {
    const current = (p.holdings[sym]?.weight ?? 0);
    const target  = p.target_allocation[sym];
    const drift   = (current - target);
    const driftClass = Math.abs(drift) < 0.005 ? 'on' : drift > 0 ? 'over' : 'under';
    const driftSign  = drift > 0 ? '+' : '';
    const color = colorFor(sym, i);
    const barPct = Math.min(current * 100, 100);
    return `
      <div class="allocation-row">
        <div class="alloc-symbol">${sym}</div>
        <div class="alloc-bar-wrap">
          <div class="alloc-bar" style="width:${barPct}%;background:${color}"></div>
        </div>
        <div class="alloc-pct">${(current*100).toFixed(1)}%</div>
        <div class="alloc-target">/ ${(target*100).toFixed(0)}%</div>
        <div class="drift-badge ${driftClass}">${driftSign}${(drift*100).toFixed(1)}%</div>
      </div>`;
  }).join('');
  document.getElementById('allocation-rows').innerHTML = rows || '<div class="loading">No positions yet</div>';
}

function renderHistory(entries) {
  // ── Value over time (from portfolio_snapshot events) ──
  const snapshots = entries.filter(e => e.event === 'portfolio_snapshot' && e.total_value > 0);
  // deduplicate by day, keep last snapshot of each day
  const byDay = {};
  snapshots.forEach(s => {
    const day = s.timestamp.slice(0,10);
    byDay[day] = s;
  });
  const days = Object.values(byDay).sort((a,b) => a.timestamp.localeCompare(b.timestamp));

  if (days.length >= 2) {
    mkValueChart(
      days.map(d => fmtDateShort(d.timestamp)),
      days.map(d => d.total_value),
    );
    // drift over time
    const allSymbols = [...new Set(days.flatMap(d => Object.keys(d.drift_from_target || {})))];
    const driftData = {};
    allSymbols.forEach(sym => {
      driftData[sym] = days.map(d => d.drift_from_target?.[sym] ?? null);
    });
    mkDriftChart(days.map(d => fmtDateShort(d.timestamp)), driftData);
  } else {
    document.querySelector('.chart-wrap').innerHTML    = '<div class="loading">Not enough history yet — data builds after your first few cycles.</div>';
    document.querySelector('.chart-wrap-sm').innerHTML = '<div class="loading">Not enough history yet.</div>';
  }

  // ── Contribution history ──
  const proposals = entries.filter(e => e.event === 'ai_allocation_proposed').slice(0, 10);
  const ul = document.getElementById('contributions');
  if (!proposals.length) {
    ul.innerHTML = '<li class="loading">No contributions yet</li>';
  } else {
    ul.innerHTML = proposals.map(p => {
      const parts = Object.entries(p.allocations)
        .map(([sym, amt]) => `<span>${sym} ${fmt(amt)}</span>`).join('  ');
      return `<li class="contribution-item">
        <div class="contrib-left">
          <div class="contrib-date">${fmtTs(p.timestamp)}</div>
          <div class="contrib-alloc">${parts}</div>
          <div class="contrib-reasoning">${p.reasoning}</div>
        </div>
        <div class="contrib-right">${fmt(p.new_cash)}</div>
      </li>`;
    }).join('');
  }

  // ── Event log ──
  const recent = entries.slice(0, 20);
  const eventDot = {
    portfolio_snapshot:   'gray',
    ai_allocation_proposed: 'purple',
    approval_email_sent:  'blue',
    orders_placed:        'green',
    allocation_rejected:  'red',
    approval_expired:     'yellow',
    contribution_error:   'red',
  };
  const eventLabel = e => {
    switch(e.event) {
      case 'portfolio_snapshot':     return `Snapshot — ${fmt(e.total_value)}`;
      case 'ai_allocation_proposed': return `AI proposed ${Object.entries(e.allocations).map(([s,a])=>s+' '+fmt(a)).join(', ')}`;
      case 'approval_email_sent':    return `Approval email sent (token ${e.token_prefix}…)`;
      case 'orders_placed':          return `Orders placed — ${e.receipts?.map(r=>r.symbol).join(', ')}`;
      case 'allocation_rejected':    return 'Allocation denied';
      case 'approval_expired':       return `Approval expired (token ${e.token_prefix}…)`;
      case 'contribution_error':     return `Error: ${e.error}`;
      default:                       return e.event.replace(/_/g,' ');
    }
  };
  document.getElementById('event-log').innerHTML = recent.map(e => `
    <li class="event-item">
      <div class="event-dot ${eventDot[e.event] || 'gray'}"></div>
      <div class="event-time">${fmtTs(e.timestamp)}</div>
      <div class="event-text">${eventLabel(e)}</div>
    </li>`).join('');
}

// ── Load ─────────────────────────────────────────────────
async function loadAll() {
  document.getElementById('refresh-btn').textContent = '↻ …';
  try {
    const [portfolio, health, audit] = await Promise.all([
      fetch('/portfolio').then(r => r.json()),
      fetch('/health').then(r => r.json()),
      fetch('/audit').then(r => r.json()),
    ]);
    renderPortfolio(portfolio, health);
    renderHistory(audit);
    document.getElementById('last-updated').textContent =
      'Updated ' + new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'});
  } catch(err) {
    document.getElementById('status-bar').innerHTML =
      `<span class="pill red">⚠ Failed to load: ${err.message}</span>`;
  }
  document.getElementById('refresh-btn').textContent = '↻ Refresh';
}

loadAll();
// Auto-refresh every 60 seconds
setInterval(loadAll, 60_000);
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
