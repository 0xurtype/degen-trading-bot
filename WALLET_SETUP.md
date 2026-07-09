# DeGen Trading Bot - Wallet Setup

## Quick Start

### 1. Create Fresh Hot Wallet

```bash
# Install Solana CLI
sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"

# Generate new wallet
solana-keygen new --outfile ~/.config/bot/wallet.json

# View wallet address
solana-keygen pubkey ~/.config/bot/wallet.json
```

**Save the seed phrase somewhere safe!** This is your backup.

### 2. Fund Wallet

Send SOL to the wallet address:
- Minimum: 2 SOL (for ~4 trades at 0.5 SOL each + gas)
- Recommended: 5-10 SOL

Check balance:
```bash
solana balance ~/.config/bot/wallet.json
```

### 3. Set Environment Variable

```bash
# Add to ~/.bashrc or run before bot
export PRIVATE_KEY=$(cat ~/.config/bot/wallet.json)
```

Or for better security, use the seed phrase:
```bash
export SEED_PHRASE="your twelve word seed phrase here"
```

### 4. Run Bot

```bash
# Dry-run first (paper trading, no real swaps)
cd /tmp/GMGN-scanner
python3 bot.py --dry-run

# When ready for live trading
python3 bot.py --live
```

---

## Wallet Security

**DO:**
- Use a fresh wallet (never used before)
- Keep only trading funds in this wallet
- Store seed phrase offline (paper, metal backup)
- Use hardware wallet for large amounts

**DON'T:**
- Use your main wallet
- Store seed phrase in cloud/notes app
- Share wallet private key with anyone
- Keep more SOL than needed for trading

---

## Monitoring

**Track wallet on Solscan:**
```
https://solscan.io/account/YOUR_WALLET_ADDRESS
```

**Check bot status:**
```bash
python3 bot.py --status
```

---

## Live Trading Setup (Jupiter Swap)

For live trading, the bot needs to sign transactions. Two options:

### Option A: Use `solders` library (recommended)

```bash
pip install solders solana

# Bot will automatically use wallet.json for signing
```

### Option B: Use private key directly

The bot reads `PRIVATE_KEY` env var or `~/.config/bot/wallet.json`.

---

## Recommended Settings

| Parameter | Value | Reason |
|-----------|-------|--------|
| Bet size | 0.5 SOL | ~$70 per trade |
| Stop-loss | -20% | Cut losses early |
| Trailing stop | -10% | Lock in gains on pumps |
| Max hold | 2 hours | Don't baghold |
| MCAP range | $10K-150K | Sweet spot for concentration signals |
| Wallet count | >= 10 | Strong signal |
| Conviction | < 10% | Not already pumped |

---

## Troubleshooting

**"Insufficient balance"**
- Add more SOL to wallet

**"Swap failed"**
- Token might be frozen/honeypot
- Slippage too low (increase SLIPPAGE_BPS)

**"Rate limited"**
- Wait and retry

**"No signals"**
- Scanner might be down
- Check GMGN-scanner logs
