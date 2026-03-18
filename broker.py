"""
broker.py — Alpaca client, portfolio state, order execution, and market hours.
"""

from datetime import date, datetime

import anthropic
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest

from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ANTHROPIC_API_KEY,
    ET,
    MAX_SINGLE_ORDER_USD,
    MIN_ORDER_USD,
    TARGET_ALLOCATION,
    log,
)

# ─────────────────────────────────────────────
# CLIENTS
# ─────────────────────────────────────────────

broker    = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)  # paper=False for live
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
    9:30am-4:00pm ET, holidays excluded.
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
