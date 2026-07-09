#!/usr/bin/env python3
"""
DeGen Trading Bot v2.0 — Charon Integration
===========================================

Strategy: Charon real-time signals + smart money concentration
- Entry: Charon signal server (Pump.fun fee claims + graduation + trending)
- Rich filters: organicScore, sourceCount, sniperCount, topHoldersPercent, feeClaim
- Exit: smart_exit signal OR stop-loss -20% OR trailing stop -10% OR max hold 2h
- Swap: Jupiter Aggregator for graduated tokens

Usage:
  python3 bot.py --dry-run          # Paper trading (no real swaps)
  python3 bot.py --live             # Live trading
  python3 bot.py --status           # Show open positions
  python3 bot.py --balance          # Show wallet balance

Env:
  CHARON_API_URL=https://api.thecharon.xyz/api
  CHARON_API_KEY=your_key_here
"""

import json
import os
import sys
import time
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / 'data'
SIGNALS_LOG = DATA_DIR / 'signals_log.jsonl'
BOT_STATE = DATA_DIR / 'bot_state.json'

# Charon signal server
CHARON_API_URL = os.getenv('CHARON_API_URL', 'https://api.thecharon.xyz/api')
CHARON_API_KEY = os.getenv('CHARON_API_KEY', 'bb1eba8198941bfbac811d6e49b06a700419ec45471918ff')
CHARON_POLL_SEC = 30

# Strategy params — basic
MCAP_MIN = 20_000
MCAP_MAX = 80_000
WALLET_COUNT_MIN = 10
SMART_CONVICTION_MAX = 10.0
STOP_LOSS_PCT = 20.0
TRAILING_STOP_PCT = 10.0
MAX_HOLD_SECONDS = 2 * 3600
BET_SIZE_SOL = 0.1
SLIPPAGE_BPS = 500

# Trailing take-profit: once price rises ≥TP_ARM_PCT from entry, arm trailing stop.
# Exits at peak×(1-TP_TRAIL_PCT) — locks in gains while letting winners run.
TP_ARM_PCT = 20.0       # Arm trailing TP after +20% from entry
TP_TRAIL_PCT = 15.0     # Trail 15% below peak once armed
TP_HARD_CAP_PCT = 500.0 # Hard exit at +500% regardless (let mooners run but cap risk)

# Charon-specific strategy gates (tuned via backtest)
ORGANIC_SCORE_MIN = 50.0        # Skip tokens with low organic volume
SOURCE_COUNT_MIN = 2            # Need at least 2 sources seeing token
MAX_SNIPER_COUNT = 10           # Backtest: snipers=0 → WR 10.5%, snipers>10 → WR 2-4%
MAX_TOP_HOLDERS_PCT = 50.0      # Backtest: ≤50% → WR 5.5% (best), PnL +$2,085
REQUIRE_FEE_CLAIM = False       # Optional: require fee claim data
MIN_FEE_CLAIM_SOL = 0.5         # Minimum distributed SOL if fee claim exists
MAX_TOKEN_AGE_MIN = 30          # Max token age in minutes — catch early before hype
REQUIRE_GRADUATED = False       # Optional: only trade graduated tokens
REQUIRE_TRENDING = True         # Require trending data (on by default)
TRENDING_VOLUME_MIN = 0         # Minimum 5m volume in USD
MOMENTUM_MIN = 5.0              # Min 5m price change % — must be building momentum
MOMENTUM_MAX = 50.0             # Max 5m price change % — skip already-pumped tokens

# Position management
MAX_ENTRIES_PER_POLL = 5        # Max new positions per Charon poll
MAX_CONCURRENT_POSITIONS = 5    # Max simultaneous open positions

# Jupiter API
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_PRICE_API = "https://price.jup.ag/v6/price"
DEXSCREENER_TOKEN_API = "https://api.dexscreener.com/latest/dex/tokens"

# ─────────────────────────────────────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    token: str
    token_name: str
    entry_price: float
    entry_ts: float
    entry_mcap: float
    amount_sol: float
    wallet_count: int = 0
    smart_conviction: float = 0.0
    peak_price: float = 0.0
    exit_price: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = ""
    pnl_pct: float = 0.0
    pnl_sol: float = 0.0
    status: str = "open"
    # Charon metadata
    organic_score: float = 0.0
    source_count: int = 0
    # Trailing TP state
    tp_armed: bool = False
    sniper_count: int = 0
    top_holders_pct: float = 0.0
    liquidity_usd: float = 0.0
    holders: int = 0

@dataclass
class CharonSignal:
    """Signal from Charon API."""
    mint: str
    name: str
    symbol: str
    price_usd: float
    market_cap_usd: float
    liquidity_usd: float
    holders: int
    age_ms: int
    bonding_complete: bool
    source_count: int
    sources: list
    volume_24h: float
    volume_5m: float

    # Trending data
    organic_score: float = 0.0
    organic_label: str = "unknown"
    change_5m: float = 0.0
    buy_volume_5m: float = 0.0
    sells_5m: int = 0
    buys_5m: int = 0

    # Graduation data
    graduated: bool = False
    dev: str = ""
    sniper_count: int = 0
    top_holders_pct: float = 0.0
    dev_holdings_pct: float = 0.0
    pool_address: str = ""
    twitter: str = ""
    website: str = ""
    telegram: str = ""

    # Fee claim data
    has_fee_claim: bool = False
    fee_distributed_sol: float = 0.0
    fee_shareholders: int = 0
    fee_solo_recipient: bool = False  # If single shareholder gets all

    # 24h stats
    price_change_24h: float = 0.0
    volume_change_5m: float = 0.0

    def age_minutes(self) -> float:
        return self.age_ms / 60_000

    def is_suspicious(self) -> bool:
        """Quick heuristic for rug/trash tokens."""
        if self.organic_score < 30:
            return True
        if self.top_holders_pct > 70:
            return True
        if self.fee_solo_recipient and self.fee_distributed_sol > 5:
            return True
        if self.graduated and self.sniper_count > 80:
            return True
        return False

@dataclass
class Signal:
    type: str
    token: str
    token_name: str
    token_address: str
    timestamp: float
    price: float
    mcap: float
    wallet_count: int = 0
    smart_conviction: float = 0.0
    dev_status: str = "unknown"

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def to_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except: return default

def to_int(v, default=0):
    try: return int(v) if v is not None else default
    except: return default

def format_duration(sec: float) -> str:
    if sec <= 0: return '?'
    if sec < 60: return f'{sec:.0f}s'
    if sec < 3600: return f'{sec/60:.0f}m'
    if sec < 86400: return f'{sec/3600:.1f}h'
    return f'{sec/86400:.1f}d'

def ts_to_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def short_addr(addr: str) -> str:
    return addr[:8] + '..' + addr[-4:] if len(addr) > 12 else addr

# ─────────────────────────────────────────────────────────────────────────────
# CHARON CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class CharonClient:
    """Polls Charon signal server for real-time Pump.fun signals."""

    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.seen_mints: set[str] = set()
        self.session: Optional[aiohttp.ClientSession] = None
        self._stats = {'total_polls': 0, 'new_signals': 0, 'errors': 0}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def poll(self) -> list[CharonSignal]:
        """Fetch latest signals from Charon server. Returns only NEW signals."""
        if not self.session:
            return []

        self._stats['total_polls'] += 1

        try:
            url = f'{self.api_url}/signals?limit=100&minSources=2'
            headers = {'x-api-key': self.api_key}

            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f'[charon] HTTP {resp.status}')
                    self._stats['errors'] += 1
                    return []

                data = await resp.json()
                raw_signals = data.get('signals', [])
        except asyncio.TimeoutError:
            print('[charon] timeout')
            self._stats['errors'] += 1
            return []
        except Exception as e:
            print(f'[charon] error: {e}')
            self._stats['errors'] += 1
            return []

        new_signals = []
        for raw in raw_signals:
            mint = raw.get('mint', '')
            if not mint or mint in self.seen_mints:
                continue

            self.seen_mints.add(mint)
            signal = self._parse_signal(raw)
            if signal:
                new_signals.append(signal)

        if new_signals:
            self._stats['new_signals'] += len(new_signals)
            print(f'[charon] {len(new_signals)} new / {len(raw_signals)} total')

        return new_signals

    def _parse_signal(self, raw: dict) -> Optional[CharonSignal]:
        """Parse raw JSON into CharonSignal."""
        try:
            trending = raw.get('trending') or {}
            graduated = raw.get('graduated') or {}
            fee_claim = raw.get('feeClaim')

            # Fee claim analysis
            has_fee = bool(fee_claim)
            fee_sol = to_float(fee_claim.get('distributedSol') if fee_claim else 0)
            shareholders = (fee_claim.get('shareholders') or []) if fee_claim else []
            solo_recipient = len(shareholders) == 1 if shareholders else False

            # Top holder percent
            top_pct = to_float(graduated.get('topHoldersPercent'))

            return CharonSignal(
                mint=raw.get('mint', ''),
                name=raw.get('name', ''),
                symbol=raw.get('symbol', ''),
                price_usd=to_float(raw.get('priceUsd')),
                market_cap_usd=to_float(raw.get('marketCapUsd')),
                liquidity_usd=to_float(raw.get('liquidityUsd')),
                holders=to_int(raw.get('holders')),
                age_ms=to_int(raw.get('ageMs')),
                bonding_complete=raw.get('bondingComplete', False),
                source_count=to_int(raw.get('sourceCount', 1)),
                sources=raw.get('sources', []),
                volume_24h=to_float(raw.get('volume24h')),
                volume_5m=to_float(raw.get('volume5m')),

                # Trending
                organic_score=to_float(trending.get('organicScore')),
                organic_label=trending.get('organicScoreLabel', 'unknown'),
                change_5m=to_float(trending.get('change5m')),
                buy_volume_5m=to_float(trending.get('buyVolume')),
                buys_5m=to_int(trending.get('buys')),
                sells_5m=to_int(trending.get('sells')),

                # Graduation
                graduated=bool(graduated),
                dev=graduated.get('dev', ''),
                sniper_count=to_int(graduated.get('sniperCount')),
                top_holders_pct=top_pct,
                dev_holdings_pct=to_float(graduated.get('devHoldingsPercent')),
                pool_address=graduated.get('poolAddress', ''),
                twitter=graduated.get('twitter', ''),
                website=graduated.get('website', ''),
                telegram=graduated.get('telegram', ''),

                # Fee claim
                has_fee_claim=has_fee,
                fee_distributed_sol=fee_sol,
                fee_shareholders=len(shareholders),
                fee_solo_recipient=solo_recipient,

                # Stats
                price_change_24h=to_float(trending.get('stats24h', {}).get('priceChange')),
                volume_change_5m=to_float(trending.get('stats5m', {}).get('volumeChange')),
            )
        except Exception as e:
            print(f'[charon] parse error: {e}')
            return None

    def print_stats(self):
        print(f'[charon] polls: {self._stats["total_polls"]}, new: {self._stats["new_signals"]}, errors: {self._stats["errors"]}, tracked: {len(self.seen_mints)}')

# ─────────────────────────────────────────────────────────────────────────────
# FILE SIGNAL LOADER (fallback)
# ─────────────────────────────────────────────────────────────────────────────

def load_file_signals() -> list[Signal]:
    """Load signals from log file (existing format)."""
    if not SIGNALS_LOG.exists():
        return []

    signals = []
    with open(SIGNALS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                sig = Signal(
                    type=data.get('type', ''),
                    token=data.get('token', ''),
                    token_name=data.get('token', data.get('token_address', '')[:8]),
                    token_address=data.get('token_address', ''),
                    timestamp=data.get('timestamp', 0),
                    price=to_float(data.get('price', 0) or data.get('current_price', 0)),
                    mcap=to_float(data.get('mcap', 0)),
                    wallet_count=to_int(data.get('wallet_count', 0)),
                    smart_conviction=to_float(data.get('smart_conviction_pct', 0)),
                    dev_status=data.get('dev_status', 'unknown'),
                )
                signals.append(sig)
            except json.JSONDecodeError:
                continue

    return signals

# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[Position] = []
        self.last_signal_ts: float = time.time()  # Skip stale historical signals on fresh start
        self.load()

    def load(self):
        if BOT_STATE.exists():
            try:
                data = json.loads(BOT_STATE.read_text())
                for addr, pos_data in data.get('positions', {}).items():
                    self.positions[addr] = Position(**pos_data)
                for pos_data in data.get('closed_trades', []):
                    self.closed_trades.append(Position(**pos_data))
                self.last_signal_ts = data.get('last_signal_ts', 0)
            except Exception as e:
                print(f'Failed to load state: {e}')

    def save(self):
        data = {
            'positions': {addr: pos.__dict__ for addr, pos in self.positions.items()},
            'closed_trades': [pos.__dict__ for pos in self.closed_trades[-100:]],
            'last_signal_ts': self.last_signal_ts,
        }
        BOT_STATE.write_text(json.dumps(data, indent=2))

    def add_position(self, pos: Position):
        self.positions[pos.token] = pos
        self.save()

    def close_position(self, token: str, exit_price: float, exit_ts: float, reason: str):
        if token not in self.positions:
            return

        pos = self.positions[token]
        pos.exit_price = exit_price
        pos.exit_ts = exit_ts
        pos.exit_reason = reason
        pos.status = 'closed'

        if pos.entry_price > 0:
            pos.pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        pos.pnl_sol = pos.pnl_pct / 100 * pos.amount_sol

        del self.positions[token]
        self.closed_trades.append(pos)
        self.save()

        return pos

# ─────────────────────────────────────────────────────────────────────────────
# JUPITER SWAP (simulated for now)
# ─────────────────────────────────────────────────────────────────────────────

class JupiterSwap:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def get_quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[dict]:
        params = {
            'inputMint': input_mint,
            'outputMint': output_mint,
            'amount': amount,
            'slippageBps': SLIPPAGE_BPS,
        }
        try:
            async with self.session.get(JUPITER_QUOTE_API, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    print(f'Quote failed: {resp.status}')
                    return None
        except Exception as e:
            print(f'Quote error: {e}')
            return None

    async def execute_swap(self, input_mint: str, output_mint: str, amount_sol: float) -> tuple[bool, float]:
        if self.dry_run:
            print(f'[DRY-RUN] Would swap {amount_sol} SOL for {output_mint[:8]}...')
            await asyncio.sleep(0.3)
            return True, amount_sol * 0.95

        print('[LIVE] Swap not implemented yet - use --dry-run')
        return False, 0.0

# ─────────────────────────────────────────────────────────────────────────────
# TRADING BOT
# ─────────────────────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.state = State()
        self.jupiter = None
        self.charon: Optional[CharonClient] = None
        self.charon_price_map: dict[str, float] = {}  # mint -> latest price from Charon
        self._entries_this_poll = 0

    async def run(self):
        print('🤖 DeGen Trading Bot v2.0 — Charon')
        print(f'Mode: {"DRY-RUN (paper trading)" if self.dry_run else "LIVE"}')
        print(f'Charon API: {CHARON_API_URL}')
        print(f'Entry gates: organic>={ORGANIC_SCORE_MIN}, sources>={SOURCE_COUNT_MIN}, snipers<={MAX_SNIPER_COUNT}, topHolders<={MAX_TOP_HOLDERS_PCT}%')
        print(f'MCAP: ${MCAP_MIN:,} - ${MCAP_MAX:,}')
        print(f'Age: <= {MAX_TOKEN_AGE_MIN}min | Momentum: {MOMENTUM_MIN}-{MOMENTUM_MAX}% 5m')
        print(f'Bet size: {BET_SIZE_SOL} SOL')
        print(f'Stop-loss: -{STOP_LOSS_PCT}% | Trailing: -{TRAILING_STOP_PCT}%')
        print(f'Trailing TP: arms at +{TP_ARM_PCT}% | trails at -{TP_TRAIL_PCT}% from peak | hard cap +{TP_HARD_CAP_PCT}%')
        print()

        # Dedicated HTTP session for Jupiter on-chain price fetches (exit monitoring)
        self.session = aiohttp.ClientSession()
        try:
            async with JupiterSwap(dry_run=self.dry_run) as jupiter:
                self.jupiter = jupiter
                async with CharonClient(CHARON_API_URL, CHARON_API_KEY) as charon:
                    self.charon = charon

                    while True:
                        try:
                            await self.tick()
                            await asyncio.sleep(5)
                        except KeyboardInterrupt:
                            print('\nStopping...')
                            self.charon.print_stats()
                            break
                        except Exception as e:
                            print(f'Error: {e}')
                            await asyncio.sleep(10)
        finally:
            if self.session:
                await self.session.close()

    async def tick(self):
        """Check for new signals and manage positions."""
        # 1. Poll Charon API every N seconds
        if self.charon and int(time.time()) % CHARON_POLL_SEC < 5:
            self._entries_this_poll = 0
            charon_signals = await self.charon.poll()

            # Update price map with Charon data for exit monitoring
            for cs in charon_signals:
                if cs.price_usd > 0:
                    self.charon_price_map[cs.mint] = cs.price_usd

            # Log all Charon signals for backtesting
            for cs in charon_signals:
                self.log_charon_signal(cs)

            for cs in charon_signals:
                await self.process_charon_signal(cs)

        # 2. Load file signals for exits (smart_exit)
        file_signals = load_file_signals()
        if file_signals:
            file_signals.sort(key=lambda s: s.timestamp)
            new_file = [s for s in file_signals if s.timestamp > self.state.last_signal_ts]

            # Skip stale file signals — only process signals from last 30min
            cutoff = time.time() - (MAX_TOKEN_AGE_MIN * 60) if MAX_TOKEN_AGE_MIN > 0 else 0
            fresh_file = [s for s in new_file if s.timestamp >= cutoff]

            for sig in fresh_file:
                await self.process_file_signal(sig)
                self.state.last_signal_ts = sig.timestamp

        # 3. Check open positions for exit conditions
        await self.check_exits(file_signals)

    # ── Charon Entry Logic ──────────────────────────────────────────────

    async def process_charon_signal(self, cs: CharonSignal):
        """Evaluate a Charon signal for entry."""
        addr = cs.mint
        if addr in self.state.positions:
            return

        # Cap: max concurrent positions
        if len(self.state.positions) >= MAX_CONCURRENT_POSITIONS:
            return

        # Cap: max entries per single poll (avoids batch flood)
        if self._entries_this_poll >= MAX_ENTRIES_PER_POLL:
            return

        if not self.check_charon_entry(cs):
            return

        # Rich entry info
        print(f'')
        print(f'🟢 CHARON ENTRY: {cs.name} ({cs.symbol})')
        print(f'   Token: {cs.mint}')
        print(f'   Price: ${cs.price_usd:.10f}')
        print(f'   MCAP: ${cs.market_cap_usd:,.0f}')
        print(f'   Liq: ${cs.liquidity_usd:,.0f}')
        print(f'   Sources: {cs.source_count} ({", ".join(cs.sources)})')
        print(f'   Organic: {cs.organic_score:.0f}/100 ({cs.organic_label})')
        print(f'   Holders: {cs.holders} | Top10: {cs.top_holders_pct:.1f}%')

        if cs.graduated:
            print(f'   Snipers: {cs.sniper_count} | Dev: {short_addr(cs.dev)}')
        if cs.has_fee_claim:
            print(f'   Fee claim: {cs.fee_distributed_sol:.2f} SOL ({cs.fee_shareholders} recipients)')
        if cs.twitter:
            print(f'   Twitter: {cs.twitter}')

        await self.enter_charon_position(cs)

    def check_charon_entry(self, cs: CharonSignal) -> bool:
        """Charon-specific entry criteria. Returns True if all gates pass."""
        gates = []

        # Basic filters
        mcap_ok = MCAP_MIN <= cs.market_cap_usd <= MCAP_MAX
        gates.append(('MCAP range', mcap_ok, f'{cs.market_cap_usd:,.0f}'))

        # Organic score
        organic_ok = cs.organic_score >= ORGANIC_SCORE_MIN
        gates.append(('organicScore', organic_ok, f'{cs.organic_score:.0f}'))

        # Source count
        sources_ok = cs.source_count >= SOURCE_COUNT_MIN
        gates.append(('sources', sources_ok, f'{cs.source_count}'))

        # Sniper count (if graduated)
        if cs.graduated and cs.sniper_count > 0:
            sniper_ok = cs.sniper_count <= MAX_SNIPER_COUNT
            gates.append(('snipers', sniper_ok, f'{cs.sniper_count}'))
            if not sniper_ok:
                pass  # Will print failure

        # Top holder concentration
        if cs.top_holders_pct > 0:
            holder_ok = cs.top_holders_pct <= MAX_TOP_HOLDERS_PCT
            gates.append(('topHolders%', holder_ok, f'{cs.top_holders_pct:.1f}'))

        # Price sanity
        price_ok = cs.price_usd > 0
        gates.append(('price>0', price_ok, f'{cs.price_usd}'))

        # Liquidity sanity
        liq_ok = cs.liquidity_usd >= 1000
        gates.append(('liq>=1K', liq_ok, f'{cs.liquidity_usd:,.0f}'))

        # Token age (optional)
        if MAX_TOKEN_AGE_MIN > 0:
            age_ok = cs.age_minutes() <= MAX_TOKEN_AGE_MIN
            gates.append(('maxAge', age_ok, f'{cs.age_minutes():.0f}m'))

        # Require trending data
        if REQUIRE_TRENDING:
            trending_ok = cs.buys_5m > 0 or cs.sells_5m > 0 or cs.volume_5m > 0
            gates.append(('hasTrending', trending_ok, f'v={cs.volume_5m:.0f}'))

        # Trending volume filter
        if TRENDING_VOLUME_MIN > 0:
            vol_ok = cs.volume_5m >= TRENDING_VOLUME_MIN
            gates.append(('trendingVol', vol_ok, f'{cs.volume_5m:.0f}'))

        # Momentum filter — catch building pump, skip exhausted/already-pumped
        mom_ok = MOMENTUM_MIN <= cs.change_5m <= MOMENTUM_MAX
        gates.append(('momentum5m', mom_ok, f'{cs.change_5m:.1f}%'))

        # Fee claim (optional)
        if REQUIRE_FEE_CLAIM:
            if cs.has_fee_claim:
                fee_ok = cs.fee_distributed_sol >= MIN_FEE_CLAIM_SOL
                gates.append(('feeClaim', fee_ok, f'{cs.fee_distributed_sol:.2f} SOL'))
            else:
                gates.append(('feeClaim', False, 'missing'))

        # Require graduation
        if REQUIRE_GRADUATED:
            grad_ok = cs.graduated
            gates.append(('graduated', grad_ok, str(grad_ok)))

        # Suspicious heuristic
        if cs.is_suspicious():
            gates.append(('isSuspicious', False, 'heuristic fail'))

        # Log failures
        failed = [(name, val) for name, ok, val in gates if not ok]
        if failed:
            # Print ONE-line rejection — keeps output readable
            reasons = ' | '.join(f'{n}: {v}' for n, v in failed)
            print(f'   ✖ {cs.symbol:12s} | {reasons}')
            return False

        return True

    async def enter_charon_position(self, cs: CharonSignal):
        """Enter a position from Charon signal."""
        success, output_amount = await self.jupiter.execute_swap(
            input_mint=SOL_MINT,
            output_mint=cs.mint,
            amount_sol=BET_SIZE_SOL
        )

        if success:
            pos = Position(
                token=cs.mint,
                token_name=cs.symbol or cs.name[:8],
                entry_price=cs.price_usd,
                entry_ts=time.time(),
                entry_mcap=cs.market_cap_usd,
                amount_sol=BET_SIZE_SOL,
                peak_price=cs.price_usd,
                organic_score=cs.organic_score,
                source_count=cs.source_count,
                sniper_count=cs.sniper_count,
                top_holders_pct=cs.top_holders_pct,
                liquidity_usd=cs.liquidity_usd,
                holders=cs.holders,
            )
            self.state.add_position(pos)
            self._entries_this_poll += 1
            print(f'   ✅ Bought ~${BET_SIZE_SOL * 140:,.0f} worth ({BET_SIZE_SOL} SOL)')
        else:
            print(f'   ❌ Swap failed')

    # ── File Signal Processing (exits) ─────────────────────────────────

    async def process_file_signal(self, sig: Signal):
        """Process file-based signals (used for exit signals)."""
        addr = sig.token_address

        # ENTRY: Concentration signal (fallback to file-based)
        if sig.type == 'concentration' and addr not in self.state.positions:
            if self.check_file_entry(sig):
                # Check if Charon saw this token too — if so, skip (already processed)
                if self.charon and addr in self.charon.seen_mints:
                    return
                await self.enter_file_position(sig)

        # EXIT: Smart exit signal — skip if TP is armed (let winners run)
        elif sig.type == 'smart_exit' and addr in self.state.positions:
            pos = self.state.positions[addr]
            if pos.tp_armed:
                return  # TP armed — smart_exit would kill a winning position
            if sig.timestamp > pos.entry_ts:
                # Fetch LIVE price from DexScreener — file signal price is often stale
                live_price = await self.fetch_jupiter_price(addr)
                exit_price = live_price if live_price and live_price > 0 else sig.price
                self.state.close_position(
                    addr,
                    exit_price=exit_price,
                    exit_ts=sig.timestamp,
                    reason='smart_exit'
                )
                print(f'🔴 EXIT {sig.token_name} via smart_exit at {exit_price:.10f}')

    def check_file_entry(self, sig: Signal) -> bool:
        # Conviction dead-zone filter: skip 5-7% range (WR 4%, avg -0.055 SOL)
        # Low conv (<5%) = early/contrarian, High conv (>=7%) = strong consensus
        CONVICTION_DEAD_ZONE_MIN = 5.0
        CONVICTION_DEAD_ZONE_MAX = 7.0
        if CONVICTION_DEAD_ZONE_MIN <= sig.smart_conviction < CONVICTION_DEAD_ZONE_MAX:
            return False

        return all([
            sig.wallet_count >= WALLET_COUNT_MIN,
            sig.smart_conviction < SMART_CONVICTION_MAX,
            MCAP_MIN <= sig.mcap <= MCAP_MAX,
            sig.price > 0,
        ])

    async def enter_file_position(self, sig: Signal):
        print(f'')
        print(f'🟢 FILE ENTRY: {sig.token_name}')
        print(f'   Token: {sig.token_address}')
        print(f'   Price: {sig.price:.10f}')
        print(f'   MCAP: ${sig.mcap:,.0f}')
        print(f'   Wallets: {sig.wallet_count} | Conviction: {sig.smart_conviction:.1f}%')

        success, output_amount = await self.jupiter.execute_swap(
            input_mint=SOL_MINT,
            output_mint=sig.token_address,
            amount_sol=BET_SIZE_SOL
        )

        if success:
            pos = Position(
                token=sig.token_address,
                token_name=sig.token_name,
                entry_price=sig.price,
                entry_ts=sig.timestamp,
                entry_mcap=sig.mcap,
                amount_sol=BET_SIZE_SOL,
                wallet_count=sig.wallet_count,
                smart_conviction=sig.smart_conviction,
                peak_price=sig.price,
            )
            self.state.add_position(pos)
            print(f'   ✅ Bought ~${BET_SIZE_SOL * 140:,.0f} worth ({BET_SIZE_SOL} SOL)')
        else:
            print(f'   ❌ Swap failed')

    # ── Price Fetch ────────────────────────────────────────────────────

    async def fetch_jupiter_price(self, mint: str) -> Optional[float]:
        """Fetch real-time on-chain price (USD) for a token mint.

        Primary: DexScreener (works from this host; Jupiter Price API is unreachable).
        Falls back to Jupiter Price API if DexScreener fails.
        Returns highest-liquidity pair priceUsd, or None on failure.
        """
        if not self.session:
            return None
        # ── Primary: DexScreener ──
        try:
            async with self.session.get(
                f'{DEXSCREENER_TOKEN_API}/{mint}',
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get('pairs') or []
                    if pairs:
                        # Pick the most liquid pair for the most reliable price
                        pairs_sorted = sorted(
                            pairs,
                            key=lambda p: to_float((p.get('liquidity') or {}).get('usd', 0)),
                            reverse=True
                        )
                        return to_float(pairs_sorted[0].get('priceUsd', 0))
        except Exception as e:
            print(f'[price] dexscreener error: {e}')

        # ── Fallback: Jupiter Price API ──
        try:
            async with self.session.get(
                JUPITER_PRICE_API,
                params={'ids': mint},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price_data = data.get('data', {}).get(mint)
                    if price_data:
                        return to_float(price_data.get('price', 0))
        except Exception:
            pass
        return None

    # ── Charon Signal Logger ───────────────────────────────────────────

    def log_charon_signal(self, cs: CharonSignal):
        """Log Charon signal to signals_log.jsonl for backtesting."""
        record = {
            'type': 'charon_signal',
            'source': 'charon',
            'token': cs.symbol or cs.name[:12],
            'token_name': cs.name,
            'token_address': cs.mint,
            'price': cs.price_usd,
            'current_price': cs.price_usd,
            'mcap': cs.market_cap_usd,
            'volume_24h': cs.volume_24h,
            'liquidity': cs.liquidity_usd,
            'holders': cs.holders,
            'age_ms': cs.age_ms,
            'timestamp': int(time.time()),
            'confidence': 70,
            # Charon-specific
            'organic_score': cs.organic_score,
            'organic_label': cs.organic_label,
            'source_count': cs.source_count,
            'sources': cs.sources,
            'sniper_count': cs.sniper_count,
            'top_holders_pct': cs.top_holders_pct,
            'dev_holdings_pct': cs.dev_holdings_pct,
            'graduated': cs.graduated,
            'has_fee_claim': cs.has_fee_claim,
            'fee_distributed_sol': cs.fee_distributed_sol,
            'fee_shareholders': cs.fee_shareholders,
            'fee_solo_recipient': cs.fee_solo_recipient,
            'volume_5m': cs.volume_5m,
            'change_5m': cs.change_5m,
            'buy_volume_5m': cs.buy_volume_5m,
            'buys_5m': cs.buys_5m,
            'sells_5m': cs.sells_5m,
            'price_change_24h': cs.price_change_24h,
            'volume_change_5m': cs.volume_change_5m,
            'twitter': cs.twitter,
            'website': cs.website,
            'telegram': cs.telegram,
            '_logged_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        try:
            with open(SIGNALS_LOG, 'a') as f:
                f.write(json.dumps(record) + '\n')
        except Exception as e:
            print(f'[log_charon] write error: {e}')

    # ── Exit Management ────────────────────────────────────────────────

    async def check_exits(self, signals: list[Signal]):
        """Check exit conditions for open positions."""
        now = time.time()

        # Build price map: Charon prices preferred (more current), file signals as fallback
        price_map = {}
        price_map.update(self.charon_price_map)
        for sig in reversed(signals[-500:]):
            if sig.token_address not in price_map and sig.price > 0:
                price_map[sig.token_address] = sig.price

        # 3. Jupiter on-chain price for any remaining positions not in price_map
        for addr in list(self.state.positions):
            if addr not in price_map:
                jup_price = await self.fetch_jupiter_price(addr)
                if jup_price and jup_price > 0:
                    price_map[addr] = jup_price
                    # Also update charon_price_map for future ticks
                    self.charon_price_map[addr] = jup_price

        for addr, pos in list(self.state.positions.items()):
            current_price = price_map.get(addr)
            hold_time = now - pos.entry_ts
            if current_price is None or current_price <= 0:
                # Last-resort: fetch live price directly for this position
                live = await self.fetch_jupiter_price(addr)
                if live and live > 0:
                    current_price = live
                    price_map[addr] = live
                    self.charon_price_map[addr] = live
                else:
                    # No price anywhere = token rugged/delisted → exit at -100%
                    if hold_time > MAX_HOLD_SECONDS:
                        closed = self.state.close_position(addr, exit_price=0.0, exit_ts=now, reason='max_hold_rug')
                        if closed:
                            print(f'🔴 EXIT {pos.token_name} via max_hold_rug (no price — token dead)')
                            print(f'   PnL: -100.0% (-{pos.amount_sol:.4f} SOL)')
                    continue

            # Update peak
            if current_price > pos.peak_price:
                pos.peak_price = current_price
                self.state.save()

            exit_reason = None
            gain_pct = (current_price - pos.entry_price) / pos.entry_price * 100

            # Arm TP once gain threshold hit
            if not pos.tp_armed and gain_pct >= TP_ARM_PCT:
                pos.tp_armed = True
                self.state.save()
                print(f'🎯 TP ARMED {pos.token_name} at +{gain_pct:.1f}% (peak={pos.peak_price:.2e})')

            # 1. Stop-loss (hard floor — always active)
            if gain_pct <= -STOP_LOSS_PCT:
                exit_reason = 'stop_loss'

            # 2. Hard cap take-profit (mooners exit at +500%)
            elif gain_pct >= TP_HARD_CAP_PCT:
                exit_reason = 'tp_hard_cap'

            # 3. Trailing take-profit (only when armed)
            elif pos.tp_armed and current_price <= pos.peak_price * (1 - TP_TRAIL_PCT / 100):
                exit_reason = 'trailing_tp'

            # 4. Trailing stop (pre-TP, tighter)
            elif not pos.tp_armed and current_price <= pos.peak_price * (1 - TRAILING_STOP_PCT / 100):
                exit_reason = 'trailing_stop'

            # 5. Max hold
            elif hold_time > MAX_HOLD_SECONDS:
                exit_reason = 'max_hold'

            if exit_reason:
                closed = self.state.close_position(addr, exit_price=current_price, exit_ts=now, reason=exit_reason)
                if closed:
                    pnl_str = f'{closed.pnl_pct:+.1f}%' if closed.pnl_pct != 0 else '0%'
                    print(f'🔴 EXIT {pos.token_name} via {exit_reason}')
                    print(f'   PnL: {pnl_str} ({closed.pnl_sol:+.4f} SOL)')

    # ── Status ─────────────────────────────────────────────────────────

    def print_status(self):
        print('📊 === CURRENT STATUS ===')
        print()

        if self.state.positions:
            print('Open positions:')
            for addr, pos in self.state.positions.items():
                hold_time = time.time() - pos.entry_ts
                meta = ''
                if pos.organic_score:
                    meta = f' | organic {pos.organic_score:.0f} | {pos.source_count}sources'
                print(f'  {pos.token_name:12s} | entry ${pos.entry_price:.10f} | hold {format_duration(hold_time)}{meta}')
        else:
            print('No open positions')
        print()

        if self.state.closed_trades:
            wins = [t for t in self.state.closed_trades if t.pnl_pct > 0]
            total_pnl = sum(t.pnl_sol for t in self.state.closed_trades)

            print(f'Closed trades: {len(self.state.closed_trades)}')
            print(f'Win rate: {len(wins)}/{len(self.state.closed_trades)} ({len(wins)/len(self.state.closed_trades)*100:.1f}%)')
            print(f'Total PnL: {total_pnl:+.4f} SOL')

            print()
            print('Recent trades:')
            for t in self.state.closed_trades[-5:]:
                pnl_str = f'{t.pnl_pct:+.1f}%' if t.pnl_pct != 0 else '0%'
                print(f'  {t.token_name:12s} | {pnl_str:8s} | {t.exit_reason}')
        else:
            print('No closed trades yet')

        # Charon stats
        if self.charon:
            print()
            self.charon.print_stats()

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] == '--help':
        print(__doc__)
        sys.exit(0)

    if args[0] == '--status':
        bot = TradingBot(dry_run=True)
        bot.print_status()
        sys.exit(0)

    if args[0] == '--balance':
        print('Balance check not implemented - check via Solscan')
        sys.exit(0)

    dry_run = '--live' not in args

    bot = TradingBot(dry_run=dry_run)
    asyncio.run(bot.run())

if __name__ == '__main__':
    main()
