#!/usr/bin/env python3
"""
Charon Filter Backtest — Apply Charon-style filters to historical signal data
================================================================================

Instead of cross-referencing current Charon data (survivor bias), this backtest
applies Charon-style metrics that already exist in signals_log.jsonl:

  - sniper_count      → Charon's sniperCount
  - top_10_holder_rate → Charon's topHoldersPercent

Runs two strategies side-by-side:
  1. Baseline (tightened GMGN strategy)
  2. Baseline + Charon filters (sniper, top_10_holder, etc.)

Usage:
  python3 backtest_charon_filter.py                    # Full comparison
  python3 backtest_charon_filter.py --quick            # Smaller sample
  python3 backtest_charon_filter.py --gates-only       # Only test filter thresholds
"""

import json
import os
import sys
import time
from collections import defaultdict, Counter
from pathlib import Path

DATA_DIR = Path(__file__).parent / 'data'
SIGNALS_LOG = DATA_DIR / 'signals_log.jsonl'

# Strategy defaults
MCAP_MIN = 5000
MCAP_MAX = 100000
BET_SIZE = 100
STOP_LOSS_PCT = 25
MAX_DEV_DEPLOYS = 5

# Charon-style gates
SNIPER_MAX = 50
TOP10_MAX = 50.0

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
    if pct >= 50: return 'moon (+50%)'
    if pct >= 20: return 'good (+20%)'
    if pct >= 5:  return 'modest (+5%)'
    if pct >= 0:  return 'flat (0%)'
    if pct >= -20: return 'down (-5%)'
    if pct >= -50: return 'crashed (-20%)'
    return 'rugged (-50%)'

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
                except:
                    continue
    return signals

# ── Single Strategy Run ──────────────────────────────────────────────────

def run_strategy(signals: list[dict],
                 name: str = 'Strategy',
                 mcap_min: float = MCAP_MIN,
                 mcap_max: float = MCAP_MAX,
                 bet_size: float = BET_SIZE,
                 stop_loss_pct: float = STOP_LOSS_PCT,
                 max_dev_deploys: int = MAX_DEV_DEPLOYS,
                 use_sniper_filter: bool = False,
                 use_top10_filter: bool = False,
                 use_bonding_filter: bool = False,
                 sniper_max: int = SNIPER_MAX,
                 top10_max: float = TOP10_MAX,
                 ) -> dict:
    """Run a single strategy pass and return stats."""

    sorted_sigs = sorted(signals, key=lambda s: s.get('timestamp', 0))
    token_signals = defaultdict(list)
    positions = {}
    trades_closed = []

    entries_total = 0
    entries_conc = 0
    entries_confluence = 0
    filtered_dev = 0
    filtered_mcap = 0
    filtered_sniper = 0
    filtered_top10 = 0
    filtered_bonding = 0
    sl_hits = 0
    exit_signals = 0

    for s in sorted_sigs:
        stype = s.get('type', '')
        addr = s.get('token_address', '')
        ts = s.get('timestamp', 0)
        price = to_float(s.get('price', 0))
        mcap = to_float(s.get('mcap', 0))
        confidence = to_int(s.get('confidence', 70))

        dev_status = s.get('dev_status', 'unknown')
        dev_deploys = to_int(s.get('dev_deploys', 0))
        honeypot = s.get('honeypot', False)

        # Charon-style metrics from signal data
        sniper_cnt = to_int(s.get('sniper_count', 0))
        top10_rate = to_float(s.get('top_10_holder_rate', 0))

        if stype in ('concentration', 'accumulation', 'large_buy', 'smart_exit'):
            token_signals[addr].append((stype, ts, s))

        if mcap < mcap_min or mcap > mcap_max:
            filtered_mcap += 1
            continue
        if price <= 0:
            continue

        # ── CHARON FILTERS (applied to all signal types) ──
        if use_sniper_filter and sniper_cnt > sniper_max:
            filtered_sniper += 1
            continue

        if use_top10_filter and top10_rate > top10_max:
            filtered_top10 += 1
            continue

        # ── EXIT ──
        if stype == 'smart_exit' and addr in positions:
            pos = positions[addr]
            pnl_pct = (price - pos['entry_price']) / max(pos['entry_price'], 1e-12) * 100
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
                'sniper_count': pos.get('sniper_count', 0),
                'top10_rate': pos.get('top10_rate', 0),
            })
            del positions[addr]
            exit_signals += 1
            continue

        # ── ENTRY ──
        if addr in positions:
            continue

        if dev_status not in ('clean', 'unknown'):
            filtered_dev += 1
            continue
        if dev_deploys >= max_dev_deploys:
            filtered_dev += 1
            continue
        if honeypot:
            filtered_dev += 1
            continue

        # Entry condition 1: Concentration
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
                'sniper_count': sniper_cnt,
                'top10_rate': top10_rate,
            }
            entries_total += 1
            entries_conc += 1
            continue

        # Entry condition 2: Confluence
        if stype in ('accumulation', 'large_buy') and confidence >= 60:
            other_type = 'large_buy' if stype == 'accumulation' else 'accumulation'
            recent_signals = token_signals[addr]
            for sig_type, sig_ts, sig_data in recent_signals:
                if sig_type == other_type and abs(ts - sig_ts) < 3600:
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
                        'sniper_count': sniper_cnt,
                        'top10_rate': top10_rate,
                    }
                    entries_total += 1
                    entries_confluence += 1
                    break

    # Stop-loss simulation for open positions
    for addr, pos in list(positions.items()):
        token_sigs = token_signals[addr]
        sl_price = pos['sl_price']
        sl_triggered = False

        for sig_type, sig_ts, sig_data in token_sigs:
            if sig_ts > pos['entry_time']:
                sig_price = to_float(sig_data.get('price', 0))
                if sig_price > 0 and sig_price <= sl_price:
                    pnl_pct = (sig_price - pos['entry_price']) / max(pos['entry_price'], 1e-12) * 100
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
                        'sniper_count': pos.get('sniper_count', 0),
                        'top10_rate': pos.get('top10_rate', 0),
                    })
                    sl_triggered = True
                    sl_hits += 1
                    break

    # Compute results
    wins = [t for t in trades_closed if t['pnl_pct'] > 0]
    losses = [t for t in trades_closed if t['pnl_pct'] <= 0]

    total_pnl_usd = sum(t['pnl_usd'] for t in trades_closed)
    win_rate = len(wins) / max(len(trades_closed), 1) * 100
    avg_win = sum(t['pnl_pct'] for t in wins) / max(len(wins), 1)
    avg_loss = sum(t['pnl_pct'] for t in losses) / max(len(losses), 1)

    gross_profit = sum(t['pnl_pct'] for t in wins)
    gross_loss = abs(sum(t['pnl_pct'] for t in losses))
    profit_factor = gross_profit / max(gross_loss, 0.01)

    # Outcome distribution
    outcomes = defaultdict(int)
    for t in trades_closed:
        outcomes[classify_outcome(t['pnl_pct'])] += 1

    # By sniper bracket
    sniper_brackets = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0.0})
    for t in trades_closed:
        sc = t.get('sniper_count', 0)
        if sc == 0: bracket = '0'
        elif sc <= 10: bracket = '1-10'
        elif sc <= 25: bracket = '11-25'
        elif sc <= 50: bracket = '26-50'
        elif sc <= 100: bracket = '51-100'
        else: bracket = '101+'
        sniper_brackets[bracket]['total'] += 1
        if t['pnl_pct'] > 0:
            sniper_brackets[bracket]['wins'] += 1
        sniper_brackets[bracket]['pnl'] += t['pnl_pct']

    return {
        'name': name,
        'trades': len(trades_closed),
        'entries': entries_total,
        'open_positions': len(positions) - sl_hits,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': win_rate,
        'total_pnl_usd': total_pnl_usd,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'sl_hits': sl_hits,
        'exit_signals': exit_signals,
        'filtered_mcap': filtered_mcap,
        'filtered_dev': filtered_dev,
        'filtered_sniper': filtered_sniper,
        'filtered_top10': filtered_top10,
        'outcomes': dict(outcomes),
        'sniper_brackets': dict(sniper_brackets),
        'trades_data': trades_closed,
    }

# ── Threshold Sweep ──────────────────────────────────────────────────────

def sweep_threshold(signals: list[dict],
                    gate_name: str,
                    values: list) -> list[dict]:
    """Sweep a single Charon gate and report win rate at each threshold."""
    results = []

    for v in values:
        use_sniper = gate_name == 'sniper'
        use_top10 = gate_name == 'top10'
        sniper_max = v if gate_name == 'sniper' else SNIPER_MAX
        top10_max = v if gate_name == 'top10' else TOP10_MAX

        # When sweeping one gate, disable the other
        r = run_strategy(
            signals,
            name=f'{gate_name} <= {v}',
            use_sniper_filter=use_sniper,
            use_top10_filter=use_top10,
            sniper_max=sniper_max,
            top10_max=top10_max,
        )

        results.append({
            'threshold': v,
            'trades': r['trades'],
            'win_rate': r['win_rate'],
            'profit_factor': r['profit_factor'],
            'total_pnl': r['total_pnl_usd'],
            'wins': r['wins'],
            'losses': r['losses'],
        })

    return results

# ── Results Printer ──────────────────────────────────────────────────────

def print_results(r: dict):
    print(f'\n  ### {r["name"]} ###')
    print(f'  Entries: {r["entries"]} | Trades closed: {r["trades"]} | Open: {r["open_positions"]}')
    print(f'  Win rate: {r["win_rate"]:.1f}% ({r["wins"]}/{r["trades"]})')
    print(f'  PnL: ${r["total_pnl_usd"]:+.2f} | Profit factor: {r["profit_factor"]:.2f}x')
    print(f'  Avg win: {r["avg_win"]:+.1f}% | Avg loss: {r["avg_loss"]:+.1f}%')
    print(f'  Exits: {r["exit_signals"]} smart_exit / {r["sl_hits"]} stop-loss')

    if r.get('filtered_sniper') or r.get('filtered_top10'):
        print(f'  Filtered: {r["filtered_mcap"]} MCAP | {r["filtered_dev"]} dev | {r["filtered_sniper"]} sniper | {r["filtered_top10"]} top10')

    print(f'\n  Outcome distribution:')
    for label, count in sorted(r['outcomes'].items(), key=lambda x: -x[1]):
        print(f'    {label:15s}: {count}')

    if r.get('sniper_brackets') and any(b['total'] > 0 for b in r['sniper_brackets'].values()):
        print(f'\n  By sniper bracket:')
        for bracket in ['0', '1-10', '11-25', '26-50', '51-100', '101+']:
            b = r['sniper_brackets'].get(bracket)
            if b and b['total'] > 0:
                wr = b['wins'] / b['total'] * 100
                print(f'    snipers {bracket:>5s}: {b["total"]:3d} trades | WR {wr:5.1f}% | PnL {b["pnl"]:+.1f}%')

# ── Main ─────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    quick = '--quick' in args
    gates_only = '--gates-only' in args

    print(f'🤖 Charon Filter Backtest — Apply historical proxy metrics')
    print(f'  Signal source: {SIGNALS_LOG}')
    print(f'  MCAP: ${MCAP_MIN:,} - ${MCAP_MAX:,}')
    print(f'  Stop-loss: -{STOP_LOSS_PCT}%')
    print()

    signals = load_signals()
    if not signals:
        print('No signals loaded. Exiting.')
        return

    print(f'Loaded {len(signals)} signals total')

    # Optionally subsample for speed
    if quick:
        signals = signals[:5000]
        print(f'Quick mode: using first {len(signals)} signals')

    print()

    if gates_only:
        # Just sweep thresholds
        print('═══ THRESHOLD SWEEP ═══')
        print()

        print('\n── Sniper Count ──')
        sniper_vals = [0, 5, 10, 25, 50, 100, 200, 500, 999999]
        s_results = sweep_threshold(signals, 'sniper', sniper_vals)
        print(f'  {"sniper≤":>10s} | {"trades":>6s} | {"wins":>4s} | {"loss":>4s} | {"WR":>5s} | {"PF":>4s} | {"PnL":>8s}')
        print(f'  {"-"*10} | {"-"*6} | {"-"*4} | {"-"*4} | {"-"*5} | {"-"*4} | {"-"*8}')
        for r in s_results:
            print(f'  {r["threshold"]:>10d} | {r["trades"]:>6d} | {r["wins"]:>4d} | {r["losses"]:>4d} | {r["win_rate"]:>5.1f}% | {r["profit_factor"]:>4.1f}x | ${r["total_pnl"]:>+7.2f}')

        print('\n── Top 10 Holder % ──')
        top10_vals = [0, 10, 25, 50, 75, 100]
        t_results = sweep_threshold(signals, 'top10', top10_vals)
        print(f'  {"top10≤":>10s} | {"trades":>6s} | {"wins":>4s} | {"loss":>4s} | {"WR":>5s} | {"PF":>4s} | {"PnL":>8s}')
        print(f'  {"-"*10} | {"-"*6} | {"-"*4} | {"-"*4} | {"-"*5} | {"-"*4} | {"-"*8}')
        for r in t_results:
            print(f'  {r["threshold"]:>10d} | {r["trades"]:>6d} | {r["wins"]:>4d} | {r["losses"]:>4d} | {r["win_rate"]:>5.1f}% | {r["profit_factor"]:>4.1f}x | ${r["total_pnl"]:>+7.2f}')

        return

    # Run baseline
    print('═══ BASELINE (no Charon filters) ═══')
    baseline = run_strategy(
        signals,
        name='Baseline (tightened GMGN)',
        use_sniper_filter=False,
        use_top10_filter=False,
    )
    print_results(baseline)

    # Run with Charon gates applied at entry AND entry signal level
    print(f'\n{"="*60}')
    print(f'═══ CHARON FILTERS ACTIVE ═══')
    print(f'  sniper_count ≤ {SNIPER_MAX}')
    print(f'  top_10_holder_rate ≤ {TOP10_MAX}%')
    print(f'{"="*60}')

    filtered = run_strategy(
        signals,
        name='Baseline + Charon filters',
        use_sniper_filter=True,
        use_top10_filter=True,
        sniper_max=SNIPER_MAX,
        top10_max=TOP10_MAX,
    )
    print_results(filtered)

    # Comparison table
    print(f'\n{"="*60}')
    print(f'═══ COMPARISON ═══')
    print(f'{"="*60}')

    b = baseline
    f = filtered

    print(f'  {"Metric":25s} | {"Baseline":>12s} | {"+Charon":>12s} | {"Δ":>10s}')
    print(f'  {"-"*25} | {"-"*12} | {"-"*12} | {"-"*10}')
    print(f'  {"Trades":25s} | {b["trades"]:>12d} | {f["trades"]:>12d} | {f["trades"] - b["trades"]:>+10d}')
    print(f'  {"Win rate":25s} | {b["win_rate"]:>11.1f}% | {f["win_rate"]:>11.1f}% | {f["win_rate"] - b["win_rate"]:>+9.1f}pp')
    print(f'  {"Wins":25s} | {b["wins"]:>12d} | {f["wins"]:>12d} | {f["wins"] - b["wins"]:>+10d}')
    print(f'  {"Losses":25s} | {b["losses"]:>12d} | {f["losses"]:>12d} | {f["losses"] - b["losses"]:>+10d}')
    print(f'  {"Profit factor":25s} | {b["profit_factor"]:>11.2f}x | {f["profit_factor"]:>11.2f}x | {f["profit_factor"] - b["profit_factor"]:>+9.2f}x')
    print(f'  {"Total PnL":25s} | ${b["total_pnl_usd"]:>+9.2f} | ${f["total_pnl_usd"]:>+9.2f} | ${f["total_pnl_usd"] - b["total_pnl_usd"]:>+8.2f}')
    print(f'  {"Avg win":25s} | {b["avg_win"]:>+11.1f}% | {f["avg_win"]:>+11.1f}% | {f["avg_win"] - b["avg_win"]:>+9.1f}pp')
    print(f'  {"Avg loss":25s} | {b["avg_loss"]:>+11.1f}% | {f["avg_loss"]:>+11.1f}% | {f["avg_loss"] - b["avg_loss"]:>+9.1f}pp')
    print(f'  {"Smart exits":25s} | {b["exit_signals"]:>12d} | {f["exit_signals"]:>12d} | {f["exit_signals"] - b["exit_signals"]:>+10d}')
    print(f'  {"Stop-loss hits":25s} | {b["sl_hits"]:>12d} | {f["sl_hits"]:>12d} | {f["sl_hits"] - b["sl_hits"]:>+10d}')
    print(f'  {"Filtered (sniper)":25s} | {"n/a":>12s} | {f["filtered_sniper"]:>12d} | {"":>10s}')
    print(f'  {"Filtered (top10)":25s} | {"n/a":>12s} | {f["filtered_top10"]:>12d} | {"":>10s}')

    # Verdict
    print(f'\n═══ VERDICT ═══')
    wr_improvement = f['win_rate'] - b['win_rate']
    pf_improvement = f['profit_factor'] - b['profit_factor']

    if wr_improvement > 5:
        print(f'✅  Charon filters significantly IMPROVE win rate (+{wr_improvement:.1f}pp)')
    elif wr_improvement > 1:
        print(f'🔸 Charon filters modestly increase win rate (+{wr_improvement:.1f}pp)')
    elif wr_improvement > -1:
        print(f'⚪ Charon filters roughly neutral on win rate ({wr_improvement:+.1f}pp)')
    else:
        print(f'🔻 Charon filters REDUCE win rate ({wr_improvement:.1f}pp) — suggest different thresholds')

    if pf_improvement > 0.5:
        print(f'✅ Profit factor improved significantly ({b["profit_factor"]:.2f}x → {f["profit_factor"]:.2f}x)')
    elif pf_improvement > 0:
        print(f'🔸 Profit factor slightly improved ({b["profit_factor"]:.2f}x → {f["profit_factor"]:.2f}x)')
    else:
        print(f'⚪ Profit factor similar ({b["profit_factor"]:.2f}x → {f["profit_factor"]:.2f}x)')

    if f['filtered_sniper'] > 0 or f['filtered_top10'] > 0:
        print(f'\n📊 Filters removed {f["filtered_sniper"]} signals (sniper) + {f["filtered_top10"]} signals (top10)')
        print(f'   = {f["filtered_sniper"] + f["filtered_top10"]} total entry candidates filtered')
        print(f'   → Allowing those would mean {f["trades"] + f["filtered_sniper"] + f["filtered_top10"]} hypothetical trades vs {f["trades"]} actual')

    # Guidance
    print(f'\n📋 Recommended settings:')
    print(f'   CHARON_FILTER_SNIPER_MAX = {SNIPER_MAX}')
    print(f'   CHARON_FILTER_TOP10_MAX  = {TOP10_MAX}%')
    print(f'   Run --gates-only to find optimal thresholds')

    # Also run threshold sweep
    print(f'\n{"="*60}')
    print(f'═══ QUICK THRESHOLD COMPARISON ═══')
    print(f'{"="*60}')

    for gate, vals in [('sniper', [0, 10, 25, 50, 100, 999999]),
                        ('top10', [0, 10, 25, 50, 75, 100])]:
        print(f'\n── {gate} sweep ──')
        results = sweep_threshold(signals, gate, vals)
        for r in results:
            print(f'  ≤{r["threshold"]:>5d} | trades {r["trades"]:>4d} | WR {r["win_rate"]:>5.1f}% | PF {r["profit_factor"]:.1f}x | PnL ${r["total_pnl"]:>+8.2f}')


if __name__ == '__main__':
    main()
