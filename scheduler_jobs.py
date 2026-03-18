"""
scheduler_jobs.py — All scheduled jobs and the main contribution handler.
"""

import base64
import io
import json as _json
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

from ai import ask_ai_for_allocation
from approval import (
    _save_pending,
    create_pending_approval,
    pending_approvals,
)
from audit import write_audit_entry
from broker import broker, get_portfolio_state, is_trading_day
from config import (
    AUDIT_LOG_PATH,
    CONTRIBUTION_AMOUNT,
    ET,
    TARGET_ALLOCATION,
    log,
)
from email_service import _send_email, send_error_email


# ─────────────────────────────────────────────
# CONTRIBUTION HANDLER
# ─────────────────────────────────────────────

async def handle_contribution(new_cash: float, dry_run: bool = False):
    """
    Core flow for each contribution event.

    dry_run=True  -> propose allocation, log it, return. No email, no orders.
    dry_run=False -> propose allocation, send approval email, wait for click.
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
        create_pending_approval(allocations, reasoning, new_cash)

    except Exception as exc:
        log.exception(f"handle_contribution failed: {exc}")
        write_audit_entry("contribution_error", {"error": str(exc), "new_cash": new_cash})
        send_error_email(f"handle_contribution(${new_cash:.2f}, dry_run={dry_run})", exc)
        raise


# ─────────────────────────────────────────────
# SCHEDULED JOBS
# ─────────────────────────────────────────────

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


async def expire_pending():
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


def dca_contribution_report():
    """Generate and email a portfolio report at noon ET on 1st/16th."""
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
    colors = ["#4f46e5", "#06b6d4", "#10b981", "#f59e0b"]
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
