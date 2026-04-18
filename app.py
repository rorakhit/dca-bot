"""
app.py — FastAPI app, lifespan, scheduler, and entrypoint.

⚠️ RETIRED — LIVE TRADING MOVED TO dca-bot-dynamic
   All scheduler jobs below are commented out. The DISABLED=True flag in
   config.py short-circuits handle_contribution() as a backup. Dashboards,
   /portfolio, and /audit remain functional as a read-only historical view.
   To re-activate: flip config.DISABLED to False AND uncomment the
   scheduler.add_job(...) blocks below.

Portfolio (four-fund + small-cap value tilt):
  VTI  50% — Total US market
  VXUS 35% — International
  AVUV 10% — US small-cap value (factor tilt)
  BND   5% — US aggregate bonds
"""

import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from config import ET, log
from routes import router
from scheduler_jobs import (
    contribution_reminder,
    dca_contribution_report,
    expire_pending,
    scheduled_contribution,
)

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

scheduler = AsyncIOScheduler(timezone=ET)

# ⚠️ DISABLED — bot retired. All automatic jobs commented out.
# Live trading moved to dca-bot-dynamic.
#
# scheduler.add_job(
#     scheduled_contribution,
#     "cron", day="1,16", hour=10, minute=0,
#     id="scheduled_contribution",
# )
# scheduler.add_job(
#     expire_pending,
#     "cron", day="1,16", hour=15, minute=30,
#     id="expire_pending_approvals",
# )
# scheduler.add_job(
#     contribution_reminder,
#     "cron", day="15,last", hour=9, minute=0,
#     id="contribution_reminder",
# )
# scheduler.add_job(
#     dca_contribution_report,
#     "cron", day="1,16", hour=12, minute=0,
#     id="dca_contribution_report",
# )


# ─────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    log.warning(
        "⚠️ dca-bot is DISABLED (retired). Live trading runs in dca-bot-dynamic. "
        "Scheduler started with no jobs. Dashboards remain read-only."
    )
    yield
    scheduler.shutdown()


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

# Inject scheduler into health endpoint via app state
app.state.scheduler = scheduler

# Override health endpoint to pass scheduler
from fastapi import Request
from fastapi.responses import JSONResponse
from datetime import datetime
from config import ET
from broker import broker, is_market_open, is_trading_day
from approval import pending_approvals


@app.get("/health")
def health_with_scheduler():
    errors = []
    account_value = None

    try:
        account = broker.get_account()
        account_value = float(account.portfolio_value)
    except Exception as e:
        errors.append(f"Alpaca: {e}")

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


# Include all other routes (health is already registered above, so exclude it from router)
app.include_router(router)


# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
