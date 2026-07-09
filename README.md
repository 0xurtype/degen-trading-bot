# 🚀 DeGen Trading Bot

**Automated Solana degen token trading** — tracks smart money wallets, scans trending pools, executes trades via Jupiter Aggregator, and sends real-time Discord alerts.

```
tag: smart-money · pump.fun · solana · jupiter · charon · trading-bot · degen
```

---

## 🏗️ Architecture

```
degen-trading-bot/
├── bot.py                      # Main trading engine (Charon signals + Jupiter swaps)
├── backend.py                  # GMGN signal scanner — FastAPI + SSE streaming
├── forward_scan.py             # Real-time token scanner + Discord webhook alerts
├── retro_check.py             # Retroactive signal checker (historical replay)
├── analyze_trades.py           # Trade log analyzer + PnL reporter
├── analyze_session3.py        # Session-level trade analysis
├── backtest.py                # Generic backtest framework
├── backtest_charon.py         # Charon signal backtester
├── backtest_charon_filter.py  # Filter-level backtest analytics
├── proxy_server.py            # Request proxy / rate-limit helper
├── WALLET_SETUP.md            # Wallet creation + security guide
├── public/
│   └── index.html             # Scanner dashboard (holder analysis UI)
├── docker-compose.yaml         # 3-service stack: FastAPI + Nginx + frontend
├── Dockerfile                  # Nginx image build
└── nginx.conf                  # Security headers, gzip, caching
```

---

## ⚡ Quick Start

### Prerequisites

- Python 3.10+
- Solana CLI (`sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"`)
- SOL balance on a fresh wallet
- Optional: Discord webhook URL for alerts

### 1 — Clone & Install

```bash
git clone https://github.com/0xurtype/degen-trading-bot.git
cd degen-trading-bot

# Create venv
python3 -m venv .venv
source .venv/bin/activate

# Install deps
pip install -r requirements.txt
```

### 2 — Wallet Setup

See [`WALLET_SETUP.md`](WALLET_SETUP.md) for full instructions.

```bash
# Generate fresh wallet
solana-keygen new --outfile ~/.config/bot/wallet.json

# Fund with SOL (2–10 SOL recommended)
solana balance ~/.config/bot/wallet.json
```

Set your key as an environment variable:

```bash
export PRIVATE_KEY=$(cat ~/.config/bot/wallet.json)
export CHARON_API_KEY="your_charon_api_key"   # optional — Charon signal feed
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."  # optional
```

### 3 — Run

```bash
# Paper trading (safe — no real swaps)
python3 bot.py --dry-run

# Live trading
python3 bot.py --live

# Check open positions
python3 bot.py --status

# Check wallet balance
python3 bot.py --balance
```

### 4 — Run the Scanner Dashboard

```bash
# Start the backend (FastAPI + SSE)
python3 backend.py &

# Start the frontend (any static server)
npx serve public -l 3000 --cors &

# Or via Docker
docker compose up --build
```

Dashboard → `http://your-server:3000`

---

## 📐 Strategy

### Entry Signals (Charon)

The bot ingests real-time signals from the Charon API and applies a layered filter gate:

| Filter | Threshold | Rationale |
|---|---|---|
| `organicScore` | ≥ 50 | Reject low-organic / wash-traded tokens |
| `sourceCount` | ≥ 2 | Need multi-source consensus |
| `sniperCount` | ≤ 10 | High sniping = poor odds (backtest-verified) |
| `topHoldersPercent` | ≤ 50% | Avoid concentrated bags |
| `feeClaim` | ≥ 0.5 SOL | Confirms real demand |
| `momentum` | 5–50% | Building momentum, not yet pumped |
| `mcap` | $20K–$80K | Sweet spot before wider distribution |

### Exit Logic

```
Entry → arm trailing stop after +20%
       → trail 15% below peak once armed
       → hard cap exit at +500%
       → OR stop-loss at -20%
       → OR max hold 2 hours
       → OR smart_exit signal from Charon
```

### Position Limits

- Max **5 concurrent positions**
- Max **5 new entries per poll cycle** (30s)
- Bet size: **0.1 SOL** per trade (configurable)

---

## 🔧 Configuration

Key parameters in `bot.py`:

```python
MCAP_MIN            = 20_000    # $20K minimum market cap
MCAP_MAX            = 80_000    # $80K maximum market cap
WALLET_COUNT_MIN    = 10       # Smart wallets that must hold token
STOP_LOSS_PCT       = 20.0      # Hard stop-loss %
TRAILING_STOP_PCT   = 10.0      # Trailing stop %
MAX_HOLD_SECONDS    = 7200      # 2 hour max hold
BET_SIZE_SOL        = 0.1       # SOL per trade
SLIPPAGE_BPS        = 500       # 5% slippage tolerance
TP_ARM_PCT          = 20.0      # Arm trailing TP after +20%
TP_TRAIL_PCT        = 15.0     # Trail 15% below peak
TP_HARD_CAP_PCT     = 500.0    # Hard exit at +500%
```

---

## 🧪 Backtesting

```bash
# Generic token backtest
python3 backtest.py

# Charon signal backtest (with filters)
python3 backtest_charon.py

# Filter-level breakdown (which filter combination wins)
python3 backtest_charon_filter.py
```

All backtest results are written to `data/signals_retro.jsonl`.

---

## 🔍 Analysis

```bash
# Analyze a specific trading session
python3 analyze_session3.py

# Full PnL report from trade log
python3 analyze_trades.py
```

---

## 🌐 API Reference (Backend)

`backend.py` exposes a FastAPI REST + SSE interface.

### REST Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/tokens` | All scanned tokens |
| `GET` | `/api/tokens/{address}` | Single token detail |
| `GET` | `/api/smart-wallets` | Tracked smart wallets |
| `GET` | `/api/smart-tokens` | Tokens held by smart money |
| `GET` | `/api/signals` | Detected signals (last 4h) |
| `GET` | `/api/wallet/{address}` | Wallet portfolio + trades |
| `GET` | `/api/wallet/{address}/trades` | Wallet trade history |

### SSE Stream

```
GET /api/stream
```

SSE stream of real-time events:
- `signal` — new trade signal detected
- `token_update` — token metadata updated
- `wallet_update` — smart wallet position change

---

## 📁 Data Files

| File | Purpose |
|---|---|
| `data/signals_log.jsonl` | All detected signals (append-only log) |
| `data/signals_retro.jsonl` | Backtest replay results |
| `data/bot_state.json` | Open positions + bot state |
| `data/signal_keys.json` | Dedup cache (4h TTL) |
| `scanner_data/seen.json` | Scanned token cache |

---

## 🔒 Security

- **Never** use your main wallet — create a fresh hot wallet with only trading funds
- Store seed phrase **offline** (paper or metal backup)
- Use environment variables for keys, **never** hardcode them
- Scanner backend should run behind a VPN or firewall — it exposes a public API

---

## 📜 License

MIT — use at your own risk. Not financial advice.
