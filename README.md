# DCA Bot ⚡🤖📈

A personal dollar-cost averaging bot that uses AI to allocate bi-monthly contributions across a four-fund portfolio. Deployed on Railway as an always-on service — no laptop required.

**Live dashboard:** [dca-bot.up.railway.app](https://dca-bot.up.railway.app)

> **Note:** This is a personal tool for managing a single brokerage account. It is not financial advice and does not manage anyone else's money.

## How it works

1. **Scheduler fires** at 10am ET on the 1st and 16th (day after payday)
2. **Checks Alpaca calendar** — skips if the market is closed or it's a holiday
3. **Fetches portfolio state** from Alpaca (holdings, cash, drift from targets)
4. **Asks Claude** how to allocate the $100 contribution to minimise drift
5. **Emails an approve/deny link** to your inbox via Resend
6. **You click approve** → market orders execute immediately on Alpaca
7. Pending approvals **expire at 3:30pm ET** if not acted on

## Portfolio

Four-fund portfolio with a small-cap value tilt and bond allocation:

| Symbol | Target | Description |
|--------|--------|-------------|
| VTI | 50% | Total US market |
| VXUS | 35% | International |
| AVUV | 10% | US small-cap value (factor tilt) |
| BND | 5% | US aggregate bonds |

## Features

- **Desktop dashboard** at `/` — portfolio value, allocation bars, drift charts, contribution history, event log
- **Mobile dashboard** at `/dashboard` — dark theme, optimised for phone viewing
- **AI allocation** — Claude decides how to split contributions to minimise drift from target weights
- **Email approval flow** — one-click approve/deny before any orders are placed
- **Contribution reports** — auto-generated at noon on 1st/16th with charts and AI reasoning
- **Funding reminders** — emails on the 15th and last day of the month to transfer cash
- **Holiday detection** — skips days when NYSE is closed via Alpaca's calendar API
- **Audit log** — every event written to `audit_log.jsonl`
- **Error notifications** — emails you if the bot hits an unexpected error

## Scheduled jobs

| Job | Schedule | Description |
|-----|----------|-------------|
| `scheduled_contribution` | 10:00 AM ET, 1st & 16th | Run the contribution flow |
| `expire_pending_approvals` | 3:30 PM ET, 1st & 16th | Clean up unapproved tokens |
| `dca_contribution_report` | 12:00 PM ET, 1st & 16th | Email portfolio report with charts |
| `contribution_reminder` | 9:00 AM ET, 15th & last day | Email reminder to fund Alpaca |

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Desktop portfolio dashboard |
| `GET /dashboard` | Mobile portfolio dashboard |
| `GET /portfolio` | Current holdings and allocation JSON |
| `GET /health` | Server status, next run time, account value |
| `GET /audit` | Full audit log as JSON |
| `GET /pending` | Pending approval tokens |
| `POST /contribute?amount=100&dry_run=false` | Manually trigger a contribution |
| `GET /approve/{token}` | Approve a pending allocation (email link) |
| `GET /deny/{token}` | Deny a pending allocation (email link) |

## Disclaimer

This software is for personal use only. It is not financial advice. Past performance does not guarantee future results.
