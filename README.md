# FarmTown Auto-Farm Bot

Browser automation bot for [farmtown.online](https://www.farmtown.online/) — a Solana multiplayer farming game.

## Setup

```bash
pip install playwright
python -m playwright install chromium
```

## Usage

```bash
# Default: auto-select best crop, headed browser
python farmbot.py

# Farm specific crop
python farmbot.py --crop potato
python farmbot.py --crop cucumber

# Slower actions (safer)
python farmbot.py --delay 3

# Headless mode (after first setup)
python farmbot.py --headless
```

## First Run

1. Script opens Chromium browser
2. **Solve Cloudflare CAPTCHA** manually
3. Click **"Connect Phantom"** → approve in Phantom extension
4. Enter farm name → click **"Start My Farm"**
5. Bot takes over from there

Browser profile is saved in `~/.farmtown-bot-profile/` — wallet connection persists across runs.

## Crop Strategy

| Crop | Grow Time | Level | Gold/XP | Best For |
|------|-----------|-------|---------|----------|
| Potato | 2min | 1 | 60/4 | Active AFK |
| Carrot | 2min | 1 | 40/4 | Active AFK |
| Corn | 5min | 1 | 95/7 | Semi-AFK |
| Tomato | 8min | 5 | 200/14 | Semi-AFK |
| Onion | 12min | 5 | 330/22 | Passive |
| Wheat | 18min | 5 | 560/32 | Passive |
| Pumpkin | 30min | 10 | 1050/55 | AFK |
| Cucumber | 60min | 10 | 2400/105 | AFK |
| Blueberry | 3h | 15 | 8800/280 | Deep AFK |
| Dragonfruit | 8h | 25 | 28000/500 | Sleep cycle |
| Starfruit | 18h | 30 | 100000/1200 | Full AFK |

**`--crop auto`** picks highest XP/second crop you can afford.

## How It Works

- Uses **Playwright** (real Chromium browser) — no protocol hacking
- Intercepts **Socket.IO** WebSocket events to read game state
- Emits game events through the same WebSocket to perform actions
- Random delays between actions for human-like behavior
- Persistent browser profile keeps Phantom wallet session

## Safety

- Real browser, real network — identical to manual play
- No wallet private keys in the script
- No exploits or protocol manipulation
- Random action delays
- Rate-limited actions

## Architecture

```
farmbot.py
├── GameState      — tracks farm state from WS events
├── FarmBot        — main bot engine
│   ├── _setup_ws_interceptor()  — injects JS to hook Socket.IO
│   ├── _wait_for_auth()         — waits for user auth
│   ├── _farm_loop()             — main cycle
│   │   ├── _tick()              — one farming cycle
│   │   │   ├── harvest ready crops
│   │   │   ├── plant on empty soil
│   │   │   ├── buy seeds if low
│   │   │   ├── complete ready orders
│   │   │   └── claim Farmer's Pool
│   │   └── _process_events()    — parse WS events
│   └── _emit()                  — send game actions
└── WebSocket interceptor JS    — hooks Socket.IO emit/on
```

## Discord

If FarmTown Discord alerts wanted, add webhook integration similar to GMGN-scanner.

## Known Limits

- Cloudflare Turnstile must be solved manually (first session only)
- Phantom wallet connection must be approved manually (first session only)
- Server full → waits in queue automatically
- Bot stops if WebSocket disconnects (reconnects on restart)
