# DCA Bot 🤖📊

A personal dollar-cost averaging bot that uses AI to allocate bi-monthly contributions across a target portfolio. Runs as a persistent background server, sends email approval requests before placing any trades, and exposes a mobile-friendly dashboard.

> **Note:** This is a personal tool for managing a single brokerage account. It is not financial advice and does not manage anyone else's money.

## Features

- **Scheduled contributions** — fires at 10am ET on the 1st and 16th of each month
- **AI allocation** — asks Claude to decide how to split the contribution to minimise drift from target weights
- **Email approval flow** — sends a one-click approve/deny email before any orders are placed
- **Holiday detection** — skips contribution days that fall on market holidays via Alpaca's calendar API
- **Mobile dashboard** — live portfolio view with historical charts at `/dashboard`
- **Audit log** — every event (snapshot, proposal, approval, order) is written to `audit_log.jsonl`
- **Log rotation** — rotating file logs in `logs/dca_bot.log`
- **Error notifications** — emails you if the bot hits an unexpected error
- **Health endpoint** — `GET /health` for quick status checks

## Portfolio

Three-fund portfolio with a small-cap value tilt, targeting long-term wealth building:

| Symbol | Target | Description                    |
|--------|--------|--------------------------------|
| VTI    | 50%    | Total US market                |
| VXUS   | 35%    | International                  |
| AVUV   | 15%    | US small-cap value (factor tilt) |

## Setup

### 1. Install dependencies

```bash
pip install alpaca-py anthropic apscheduler fastapi uvicorn python-dotenv --break-system-packages
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Alpaca, Anthropic, and Gmail credentials
```

### 3. Run with pm2

```bash
pm2 start dca_bot.py --name dca-bot --interpreter python3
pm2 save
```

### 4. Access the dashboard

```
http://localhost:8000/dashboard
```

Use [Tailscale](https://tailscale.com) to access it from your phone anywhere.

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /dashboard` | Mobile-friendly portfolio dashboard |
| `GET /portfolio` | Current holdings and allocation JSON |
| `GET /health` | Server status, next run time, account value |
| `GET /audit` | Full audit log as JSON |
| `GET /pending` | Pending approval tokens |
| `POST /contribute?amount=100&dry_run=true` | Manually trigger a contribution cycle |
| `GET /approve/{token}` | Approve a pending allocation (email link) |
| `GET /deny/{token}` | Deny a pending allocation (email link) |

## Dry run mode

The bot runs in paper trading mode by default (`paper=True`). To switch to live trading, change the `TradingClient` init in `dca_bot.py`:

```python
broker = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=False)
```

## Disclaimer

This software is for personal use only. It is not financial advice. Past performance does not guarantee future results.
