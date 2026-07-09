#!/usr/bin/env python3
"""
GMGN Scanner Backtest v3 — Tightened DeGen Strategy

Strategy rules:
  - MCAP filter: $5K - $100K (focus on micro-caps)
  - Entry: concentration signal OR (accumulation + large_buy on same token)
  - Dev safety: dev_status in ('clean', 'unknown') AND dev_deploys < 5
  - Stop-loss: -25% from entry (simulated)
  - Exit: smart_exit signal OR stop-loss hit
  - Max 1 position per token

Usage:
  python3 backtest.py tight                    # tightened strategy
  python3 backtest.py tight --sl 30            # custom stop-loss
  python3 backtest.py tight --mcap-max 50000   # lower MCAP cap
  python3 backtest.py wallet --skip-api        # legacy wallet PnL mode
"""

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'
SIGNALS_LOG = DATA_DIR / 'signals_log.jsonl'
GMGN_CLI = '/usr/bin/gmgn-cli'

# Defaults
MCAP_MIN = 5000
MCAP_MAX = 100000  # tightened from 200K
BET_SIZE = 100
STOP_LOSS_PCT = 25  # -25%
MAX_DEV_DEPLOYS = 5

# ── Helpers ──────────────────────────────────────────────────────────────

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

def classify_outcome(pct: float) -> str:
    if pct >= 50: return '🟢 moon (+50%+)'
    if pct >= 20: return '🟢 good (+20-50%)'
    if pct >= 5:  return '🟡 modest (+5-20%)'
    if pct >= 0:  return '🟡 flat (0-5%)'
    if pct >= -20: return '🔴 down (-5 to -20%)'
    if pct >= -50: return '🔴 crashed (-20 to -50%)'
    return '💀 rugged (-50%+)'

# ── Data Loading ─────────────────────────────────────────────────────────

def load_signals() -> list[dict]:
    if not SIGNALS_LOG.exists():
        print(f'❌ {SIGNALS_LOG} not found')
        return []
    signals = []
    with open(SIGNALS_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return signals

# ── Tightened Strategy Backtest ──────────────────────────────────────────

def run_tight_strategy(signals: list[dict],
                       mcap_min: float = MCAP_MIN,
                       mcap_max: float = MCAP_MAX,
                       bet_size: float = BET_SIZE,
                       stop_loss_pct: float = STOP_LOSS_PCT,
                       max_dev_deploys: int = MAX_DEV_DEPLOYS):
    """
    Tightened degen strategy:
      - Entry on concentration OR (accumulation + large_buy on same token within 1h)
      - Dev safety: dev_status clean/unknown, dev_deploys < 5
      - Stop-loss at -X%
      - Exit on smart_exit OR stop-loss
    """
    print(f'📊 === TIGHTENED STRATEGY BACKTEST ===')
    print(f'MCAP range: ${mcap_min:,.0f} - ${mcap_max:,.0f}')
    print(f'Bet size: ${bet_size:.0f} per entry')
    print(f'Stop-loss: -{stop_loss_pct:.0f}%')
    print(f'Dev safety: status in (clean, unknown), deploys < {max_dev_deploys}')
    print(f'Entry triggers: concentration OR (accumulation + large_buy within 1h)')
    print()
    
    # Sort by timestamp
    sorted_sigs = sorted(signals, key=lambda s: s.get('timestamp', 0))
    
    # Track signal types per token for confluence
    token_signals = defaultdict(list)  # {token: [(type, ts, signal_data), ...]}
    
    # Track open positions
    positions = {}  # {token: {entry_price, entry_time, bet_size, entry_signal, ...}}
    trades_closed = []
    
    # Stats
    entries_total = 0
    entries_conc = 0
    entries_confluence = 0
    filtered_dev = 0
    filtered_mcap = 0
    sl_hits = 0
    exit_signals = 0
    
    for s in sorted_sigs:
        stype = s.get('type', '')
        addr = s.get('token_address', '')
        ts = s.get('timestamp', 0)
        price = to_float(s.get('price', 0))
        mcap = to_float(s.get('mcap', 0))
        confidence = s.get('confidence', 70)
        
        # Dev safety fields
        dev_status = s.get('dev_status', 'unknown')
        dev_deploys = to_int(s.get('dev_deploys', 0))
        honeypot = s.get('honeypot', False)
        
        # Record signal for confluence tracking
        if stype in ('concentration', 'accumulation', 'large_buy', 'smart_exit'):
            token_signals[addr].append((stype, ts, s))
        
        # MCAP filter
        if mcap < mcap_min or mcap > mcap_max:
            filtered_mcap += 1
            continue
        
        # Skip if no price
        if price <= 0:
            continue
        
        # ── EXIT CHECK ──
        if stype == 'smart_exit' and addr in positions:
            pos = positions[addr]
            pnl_pct = (price - pos['entry_price']) / pos['entry_price'] * 100 if pos['entry_price'] > 0 else 0
            pnl_usd = pnl_pct / 100 * pos['bet_size']
            hold_time = ts - pos['entry_time']
            
            trades_closed.append({
                'token': addr,
                'symbol': pos.get('symbol', addr[:8]),
                'entry_price': pos['entry_price'],
                'exit_price': price,
                'pnl_pct': pnl_pct,
                'pnl_usd': pnl_usd,
                'hold_time': hold_time,
                'entry_signal': pos['entry_signal'],
                'exit_reason': 'smart_exit',
                'mcap': pos['mcap'],
                'dev_status': pos.get('dev_status', 'unknown'),
            })
            del positions[addr]
            exit_signals += 1
            continue
        
        # ── ENTRY CHECK ──
        if addr in positions:
            continue  # Already holding
        
        # Dev safety check
        if dev_status not in ('clean', 'unknown'):
            filtered_dev += 1
            continue
        if dev_deploys >= max_dev_deploys:
            filtered_dev += 1
            continue
        if honeypot:
            filtered_dev += 1
            continue
        
        # Entry condition 1: Concentration signal
        if stype == 'concentration' and confidence >= 60:
            positions[addr] = {
                'entry_price': price,
                'entry_time': ts,
                'bet_size': bet_size,
                'entry_signal': 'concentration',
                'confidence': confidence,
                'mcap': mcap,
                'symbol': s.get('token', addr[:8]),
                'dev_status': dev_status,
                'dev_deploys': dev_deploys,
                'sl_price': price * (1 - stop_loss_pct / 100),
            }
            entries_total += 1
            entries_conc += 1
            continue
        
        # Entry condition 2: Confluence (accumulation + large_buy on same token within 1h)
        if stype in ('accumulation', 'large_buy') and confidence >= 60:
            # Check if other signal type fired within 1h
            other_type = 'large_buy' if stype == 'accumulation' else 'accumulation'
            recent_signals = token_signals[addr]
            
            for sig_type, sig_ts, sig_data in recent_signals:
                if sig_type == other_type and abs(ts - sig_ts) < 3600:  # within 1h
                    # Found confluence!
                    positions[addr] = {
                        'entry_price': price,
                        'entry_time': ts,
                        'bet_size': bet_size,
                        'entry_signal': f'confluence:{stype}+{other_type}',
                        'confidence': max(confidence, sig_data.get('confidence', 60)),
                        'mcap': mcap,
                        'symbol': s.get('token', addr[:8]),
                        'dev_status': dev_status,
                        'dev_deploys': dev_deploys,
                        'sl_price': price * (1 - stop_loss_pct / 100),
                    }
                    entries_total += 1
                    entries_confluence += 1
                    break
    
    # ── STOP-LOSS CHECK for remaining open positions ──
    # We need to simulate price movement. Since we don't have intraday data,
    # we'll check if the token's later signals would have triggered SL.
    # For now, close remaining positions at "current" price simulation.
    
    # Actually, let's check if any token had a smart_exit later that we missed
    # or simulate SL based on worst-case within hold period
    for addr, pos in list(positions.items()):
        # Check if any signal on this token shows price dropped below SL
        token_sigs = token_signals[addr]
        sl_triggered = False
        sl_price = pos['sl_price']
        
        for sig_type, sig_ts, sig_data in token_sigs:
            if sig_ts > pos['entry_time']:
                sig_price = to_float(sig_data.get('price', 0))
                if sig_price > 0 and sig_price <= sl_price:
                    # Stop-loss would have triggered
                    pnl_pct = (sig_price - pos['entry_price']) / pos['entry_price'] * 100
                    pnl_usd = pnl_pct / 100 * pos['bet_size']
                    hold_time = sig_ts - pos['entry_time']
                    
                    trades_closed.append({
                        'token': addr,
                        'symbol': pos.get('symbol', addr[:8]),
                        'entry_price': pos['entry_price'],
                        'exit_price': sig_price,
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl_usd,
                        'hold_time': hold_time,
                        'entry_signal': pos['entry_signal'],
                        'exit_reason': 'stop_loss',
                        'mcap': pos['mcap'],
                        'dev_status': pos.get('dev_status', 'unknown'),
                    })
                    sl_triggered = True
                    sl_hits += 1
                    break
        
        if not sl_triggered:
            # Position still open — mark as such
            pass
    
    # ── RESULTS ──
    if not trades_closed:
        print('No trades closed. Try adjusting filters.')
        print(f'Filtered by MCAP: {filtered_mcap}')
        print(f'Filtered by dev safety: {filtered_dev}')
        return
    
    wins = [t for t in trades_closed if t['pnl_pct'] > 0]
    losses = [t for t in trades_closed if t['pnl_pct'] <= 0]
    sl_trades = [t for t in trades_closed if t['exit_reason'] == 'stop_loss']
    exit_trades = [t for t in trades_closed if t['exit_reason'] == 'smart_exit']
    
    total_pnl_usd = sum(t['pnl_usd'] for t in trades_closed)
    win_rate = len(wins) / len(trades_closed) * 100
    avg_win = sum(t['pnl_pct'] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t['pnl_pct'] for t in losses) / max(len(losses), 1)
    
    gross_profit = sum(t['pnl_pct'] for t in wins)
    gross_loss = abs(sum(t['pnl_pct'] for t in losses))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    print(f'══════════════════════════════════════════════════════════════════')
    print('RESULTS')
    print(f'══════════════════════════════════════════════════════════════════')
    print(f'Entries taken:        {entries_total}')
    print(f'  ├─ Concentration:   {entries_conc}')
    print(f'  └─ Confluence:      {entries_confluence}')
    print(f'Trades closed:        {len(trades_closed)}')
    print(f'  ├─ Smart exit:      {len(exit_trades)}')
    print(f'  └─ Stop-loss:       {len(sl_trades)}')
    print(f'Open positions:       {len(positions) - len(sl_trades)}')
    print(f'Filtered by MCAP:     {filtered_mcap}')
    print(f'Filtered by dev:      {filtered_dev}')
    print()
    print(f'▸ Win rate:           {win_rate:5.1f}%  ({len(wins)}/{len(trades_closed)})')
    print(f'▸ Avg win:            {avg_win:+.2f}%')
    print(f'▸ Avg loss:           {avg_loss:+.2f}%')
    print(f'▸ Profit factor:      {profit_factor:.2f}x')
    print(f'▸ Total $ PnL:        ${total_pnl_usd:+.2f}')
    print(f'▸ Best trade:         {max(trades_closed, key=lambda t: t["pnl_pct"])["pnl_pct"]:+.2f}%')
    print(f'▸ Worst trade:        {min(trades_closed, key=lambda t: t["pnl_pct"])["pnl_pct"]:+.2f}%')
    
    # Hold time
    hold_times = [t['hold_time'] for t in trades_closed if t['hold_time'] > 0]
    if hold_times:
        hold_times.sort()
        med_hold = hold_times[len(hold_times)//2]
        print(f'▸ Median hold:        {format_duration(med_hold)}')
    
    # ── OUTCOME DISTRIBUTION ──
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('OUTCOME DISTRIBUTION')
    print(f'══════════════════════════════════════════════════════════════════')
    outcomes = defaultdict(int)
    for t in trades_closed:
        outcomes[classify_outcome(t['pnl_pct'])] += 1
    for outcome, count in sorted(outcomes.items(), key=lambda x: -x[1]):
        pct = count / len(trades_closed) * 100
        print(f'  {outcome}: {count} ({pct:.1f}%)')
    
    # ── BY ENTRY SIGNAL ──
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('BY ENTRY SIGNAL TYPE')
    print(f'══════════════════════════════════════════════════════════════════')
    signal_stats = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
    for t in trades_closed:
        sig = t['entry_signal'].split(':')[0] if ':' in t['entry_signal'] else t['entry_signal']
        signal_stats[sig]['total'] += 1
        signal_stats[sig]['pnl_sum'] += t['pnl_pct']
        if t['pnl_pct'] > 0:
            signal_stats[sig]['wins'] += 1
    for sig, data in sorted(signal_stats.items(), key=lambda x: -x[1]['total']):
        wr = data['wins'] / max(data['total'], 1) * 100
        avg = data['pnl_sum'] / max(data['total'], 1)
        print(f'  {sig:20s} | {data["total"]:4d} trades | 🟢 {data["wins"]:3d} ({wr:5.1f}%) | avg Δ {avg:+.1f}%')
    
    # ── BY EXIT REASON ──
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('BY EXIT REASON')
    print(f'══════════════════════════════════════════════════════════════════')
    exit_stats = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
    for t in trades_closed:
        exit_stats[t['exit_reason']]['total'] += 1
        exit_stats[t['exit_reason']]['pnl_sum'] += t['pnl_pct']
        if t['pnl_pct'] > 0:
            exit_stats[t['exit_reason']]['wins'] += 1
    for reason, data in sorted(exit_stats.items(), key=lambda x: -x[1]['total']):
        wr = data['wins'] / max(data['total'], 1) * 100
        avg = data['pnl_sum'] / max(data['total'], 1)
        print(f'  {reason:15s} | {data["total"]:4d} trades | 🟢 {data["wins"]:3d} ({wr:5.1f}%) | avg Δ {avg:+.1f}%')
    
    # ── BY DEV STATUS ──
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('BY DEV STATUS')
    print(f'══════════════════════════════════════════════════════════════════')
    dev_stats = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl_sum': 0.0})
    for t in trades_closed:
        dev_stats[t['dev_status']]['total'] += 1
        dev_stats[t['dev_status']]['pnl_sum'] += t['pnl_pct']
        if t['pnl_pct'] > 0:
            dev_stats[t['dev_status']]['wins'] += 1
    for status, data in sorted(dev_stats.items(), key=lambda x: -x[1]['total']):
        wr = data['wins'] / max(data['total'], 1) * 100
        avg = data['pnl_sum'] / max(data['total'], 1)
        print(f'  {status:15s} | {data["total"]:4d} trades | 🟢 {data["wins"]:3d} ({wr:5.1f}%) | avg Δ {avg:+.1f}%')
    
    # ── TOP TRADES ──
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('TOP 5 WINNERS')
    print(f'══════════════════════════════════════════════════════════════════')
    for t in sorted(trades_closed, key=lambda x: -x['pnl_pct'])[:5]:
        print(f'  🟢 {t["symbol"]:10s} {t["pnl_pct"]:+7.1f}% | {t["entry_signal"]:15s} | hold {format_duration(t["hold_time"])}')
    
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('WORST 5 LOSERS')
    print(f'══════════════════════════════════════════════════════════════════')
    for t in sorted(trades_closed, key=lambda x: x['pnl_pct'])[:5]:
        print(f'  🔴 {t["symbol"]:10s} {t["pnl_pct"]:+7.1f}% | {t["entry_signal"]:15s} | exit {t["exit_reason"]}')
    
    print()
    print(f'══════════════════════════════════════════════════════════════════')
    print('SUMMARY')
    print(f'══════════════════════════════════════════════════════════════════')
    print(f'Win Rate:     {win_rate:.1f}%')
    print(f'Profit Factor: {profit_factor:.2f}x')
    print(f'Total $ PnL:  ${total_pnl_usd:+.2f}')
    print(f'Stop-loss hit rate: {len(sl_trades)}/{len(trades_closed)} ({len(sl_trades)/len(trades_closed)*100:.1f}%)')
    print(f'Data span:    2 days — preliminary results')

# ── Legacy Wallet PnL Mode (kept from v2) ────────────────────────────────

def analyze_wallet_pnl(wallet_idx: dict, skip_api: bool = False,
                       mcap_min: float = MCAP_MIN, mcap_max: float = MCAP_MAX,
                       bet_size: float = BET_SIZE, max_positions: int = 5,
                       api_sample: int = 0):
    """Legacy wallet PnL tracking from v2."""
    print(f'📊 === WALLET PNL TRACKING ===')
    print(f'MCAP range: ${mcap_min:,.0f} - ${mcap_max:,.0f}')
    print(f'Bet size: ${bet_size:.0f} per signal')
    print(f'GMGN API: {"OFF" if skip_api else "ON (fallback)"}')
    print()
    
    all_trades = []
    unresolved = []
    api_calls = 0
    
    total_sells = sum(len(t['sells']) for w_data in wallet_idx.values() for t in w_data.values())
    print(f'Unique wallets: {len(wallet_idx)}')
    print(f'Total sell events: {total_sells}')
    print()
    
    for wallet, tokens in wallet_idx.items():
        for token, data in tokens.items():
            if not data['sells']:
                continue
            
            for sell in data['sells']:
                mcap = sell['mcap']
                if mcap < mcap_min or mcap > mcap_max:
                    continue
                
                sell_ts = sell['timestamp']
                sell_price = sell['price']
                sell_usd = sell['sell_usd']
                
                entry = None
                if data['buys']:
                    prior_buys = [b for b in data['buys'] if b['timestamp'] < sell_ts and b['timestamp'] > 0]
                    if prior_buys:
                        prior_buys.sort(key=lambda b: b['timestamp'])
                        recent_buys = [b for b in prior_buys if sell_ts - b['timestamp'] < 86400]
                        if recent_buys:
                            entry_buy = recent_buys[-1]
                            entry_price = entry_buy['price']
                            entry_amount = entry_buy['amount_usd']
                            
                            if entry_price > 0 and sell_price > 0:
                                pnl_pct = (sell_price - entry_price) / entry_price * 100
                            else:
                                pnl_pct = 0
                            
                            entry = {
                                'entry_price': entry_price,
                                'entry_cost': entry_amount,
                                'exit_price': sell_price,
                                'exit_usd': sell_usd,
                                'pnl_pct': pnl_pct,
                                'hold_time': sell_ts - entry_buy['timestamp'],
                                'source': 'signal',
                                'entry_ts': entry_buy['timestamp'],
                            }
                
                trade = {
                    'wallet': wallet,
                    'token': token,
                    'sell_ts': sell_ts,
                    'sell_price': sell_price,
                    'sell_usd': sell_usd,
                    'mcap': mcap,
                    'confidence': sell.get('confidence', 70),
                }
                
                if entry:
                    trade.update(entry)
                    all_trades.append(trade)
                else:
                    unresolved.append(trade)
    
    if not all_trades:
        print('No trades resolved.')
        return
    
    all_trades.sort(key=lambda t: t['pnl_pct'], reverse=True)
    
    wins = [t for t in all_trades if t['pnl_pct'] > 0]
    losses = [t for t in all_trades if t['pnl_pct'] <= 0]
    
    win_rate = len(wins) / len(all_trades) * 100
    avg_pnl = sum(t['pnl_pct'] for t in all_trades) / len(all_trades)
    avg_win = sum(t['pnl_pct'] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t['pnl_pct'] for t in losses) / max(len(losses), 1)
    
    gross_profit = sum(t['pnl_pct'] for t in wins)
    gross_loss = abs(sum(t['pnl_pct'] for t in losses))
    profit_factor = gross_profit / max(gross_loss, 0.01)
    
    dollar_pnl = sum(t['pnl_pct'] / 100 * bet_size for t in all_trades)
    
    print(f'══════════════════════════════════════════════════════════════════')
    print('RESULTS')
    print(f'══════════════════════════════════════════════════════════════════')
    print(f'Trades resolved: {len(all_trades)}')
    print(f'Unresolved:      {len(unresolved)}')
    print(f'Win rate:        {win_rate:.1f}%')
    print(f'Avg PnL:         {avg_pnl:+.2f}%')
    print(f'Profit factor:   {profit_factor:.2f}x')
    print(f'Total $ PnL:     ${dollar_pnl:+.2f}')

def build_wallet_index(signals: list[dict]) -> dict:
    idx = defaultdict(lambda: defaultdict(lambda: {'buys': [], 'sells': []}))
    
    for s in signals:
        stype = s.get('type', '')
        addr = s.get('token_address', '')
        ts = s.get('timestamp', 0)
        price = to_float(s.get('price', 0))
        mcap = to_float(s.get('mcap', 0))
        
        if stype == 'large_buy':
            wallet = s.get('wallet_address', '')
            if wallet:
                idx[wallet][addr]['buys'].append({
                    'timestamp': ts,
                    'price': price,
                    'amount_usd': to_float(s.get('amount', 0)),
                    'mcap': mcap,
                })
        
        elif stype == 'smart_exit':
            wallet = s.get('wallet_address', '')
            if wallet:
                idx[wallet][addr]['sells'].append({
                    'timestamp': ts,
                    'price': price,
                    'sell_usd': to_float(s.get('sell_usd', 0)),
                    'mcap': mcap,
                })
        
        elif stype == 'accumulation':
            wallets = s.get('wallets', [])
            for w in wallets:
                wallet = w.get('address', '')
                if wallet:
                    idx[wallet][addr]['buys'].append({
                        'timestamp': ts,
                        'price': price,
                        'amount_usd': to_float(w.get('inflow', 0)),
                        'mcap': mcap,
                    })
    
    return idx

# ── Entry ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = sys.argv[1:]
    
    mode = 'tight'
    
    if args and args[0] in ('tight', 'wallet', 'help'):
        mode = args[0]
        args = args[1:]
    
    # Parse options
    mcap_min = MCAP_MIN
    mcap_max = MCAP_MAX
    bet_size = BET_SIZE
    stop_loss = STOP_LOSS_PCT
    max_dev = MAX_DEV_DEPLOYS
    skip_api = False
    
    i = 0
    while i < len(args):
        if args[i] == '--mcap-min' and i + 1 < len(args):
            mcap_min = float(args[i + 1])
            i += 2
        elif args[i] == '--mcap-max' and i + 1 < len(args):
            mcap_max = float(args[i + 1])
            i += 2
        elif args[i] == '--bet' and i + 1 < len(args):
            bet_size = float(args[i + 1])
            i += 2
        elif args[i] == '--sl' and i + 1 < len(args):
            stop_loss = float(args[i + 1])
            i += 2
        elif args[i] == '--max-dev' and i + 1 < len(args):
            max_dev = int(args[i + 1])
            i += 2
        elif args[i] == '--skip-api':
            skip_api = True
            i += 1
        else:
            i += 1
    
    if mode == 'help':
        print(__doc__)
        sys.exit(0)
    
    signals = load_signals()
    if not signals:
        print('No signal data.')
        sys.exit(1)
    
    print(f'📥 Loaded {len(signals)} signals')
    print()
    
    if mode == 'tight':
        run_tight_strategy(
            signals,
            mcap_min=mcap_min, mcap_max=mcap_max,
            bet_size=bet_size,
            stop_loss_pct=stop_loss,
            max_dev_deploys=max_dev,
        )
    elif mode == 'wallet':
        wallet_idx = build_wallet_index(signals)
        analyze_wallet_pnl(
            wallet_idx,
            skip_api=skip_api,
            mcap_min=mcap_min, mcap_max=mcap_max,
            bet_size=bet_size,
        )
