#!/usr/bin/env python3
"""
GMGN Forward Scanner v2 — sampled. Check current status of recently signaled tokens.
Samples up to 200 unique tokens from signals 12h-24h old.
"""
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict

SIGNALS_LOG = 'data/signals_log.jsonl'
GMGN_CLI = '/usr/bin/gmgn-cli'
MIN_AGE_H = 12
MAX_AGE_H = 24
MAX_TOKENS = 200

def to_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except: return default

def gmgn_token_info(address):
    try:
        r = subprocess.run(
            [GMGN_CLI, 'token', 'info', '--chain', 'sol', '--address', address],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
        if 'data' in data and isinstance(data['data'], dict):
            data = data['data']
        return data
    except Exception as e:
        return None

def get_current_price(data):
    if not data:
        return 0, 0, False, 0
    price_obj = data.get('price', {})
    if isinstance(price_obj, dict):
        price = to_float(price_obj.get('price', 0))
    else:
        price = to_float(price_obj, 0)
    mcap = to_float(data.get('market_cap', 0))
    if mcap == 0:
        mcap = to_float(data.get('usd_market_cap', 0))
    liquidity = to_float(data.get('liquidity', 0))
    holders = to_float(data.get('holder_count', 0))
    return price, mcap, liquidity > 10, holders

def main():
    now = time.time()
    
    signals_raw = []
    with open(SIGNALS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                ts = to_float(s.get('timestamp', 0))
                age_h = (now - ts) / 3600
                if MIN_AGE_H <= age_h <= MAX_AGE_H:
                    signals_raw.append(s)
            except:
                continue
    
    print(f'Signals 12h-24h old: {len(signals_raw)}')
    
    # Group by token
    token_data = defaultdict(lambda: {'count': 0, 'types': set(), 'prices': []})
    for s in signals_raw:
        addr = s.get('token_address', '')
        if not addr:
            continue
        token_data[addr]['count'] += 1
        token_data[addr]['types'].add(s.get('type', ''))
        p = to_float(s.get('price', 0))
        if p > 0:
            ts = to_float(s.get('timestamp', 0))
            token_data[addr]['prices'].append((ts, p))
    
    tokens = list(token_data.keys())
    print(f'Unique tokens: {len(tokens)}')
    
    # Sample: prefer tokens with multiple signal types (more interesting)
    def sort_key(addr):
        td = token_data[addr]
        return len(td['types']) * 10 + td['count']
    
    tokens.sort(key=sort_key, reverse=True)
    sample = tokens[:MAX_TOKENS]
    print(f'Sampling {len(sample)} tokens\n')
    
    results = []
    errors = 0
    
    for i, addr in enumerate(sample):
        if i % 20 == 0 and i > 0:
            print(f'  [{i}/{len(sample)}] errors: {errors}', flush=True)
        
        td = token_data[addr]
        info = gmgn_token_info(addr)
        current_price, current_mcap, has_liq, holders = get_current_price(info)
        
        if td['prices']:
            signal_price = max(td['prices'], key=lambda x: x[0])[1]
        else:
            signal_price = 0
        
        if info is None:
            status = 'API_FAIL'
            pct_change = 0
        elif current_price > 0 and has_liq:
            pct_change = (current_price - signal_price) / signal_price * 100 if signal_price > 0 else 0
            if pct_change > 200:
                status = 'MOON'
            elif pct_change > 0:
                status = 'UP'
            elif pct_change >= -25:
                status = 'DOWN'
            else:
                status = 'CRASHED'
        elif holders > 0:
            pct_change = 0
            status = 'DEAD_NO_LIQ'
        else:
            pct_change = 0
            status = 'DEAD_RUGGED'
        
        results.append({
            'address': addr,
            'symbol': info.get('symbol', addr[:8]) if info else addr[:8],
            'signal_types': list(td['types']),
            'signal_price': signal_price,
            'current_price': current_price,
            'current_mcap': current_mcap,
            'pct_change': pct_change,
            'status': status,
            'has_liquidity': has_liq,
            'holders': int(holders),
        })
        
        if status == 'API_FAIL':
            errors += 1
    
    # ── REPORT ──
    print(f'\n{"="*70}')
    print(f'FORWARD SCAN — {len(results)} tokens from 12-24h window')
    print(f'{"="*70}')
    
    status_counts = Counter(r['status'] for r in results)
    for s in ['MOON', 'UP', 'DOWN', 'CRASHED', 'DEAD_NO_LIQ', 'DEAD_RUGGED', 'API_FAIL']:
        c = status_counts.get(s, 0)
        pct = c / len(results) * 100 if results else 0
        icon = {'MOON':'🚀','UP':'🟢','DOWN':'🟡','CRASHED':'🔴','DEAD_NO_LIQ':'💀','DEAD_RUGGED':'☠️','API_FAIL':'⚠️'}.get(s,'')
        print(f'  {icon} {s:15s}: {c:4d} ({pct:5.1f}%)')
    
    has_liq_count = sum(1 for r in results if r['has_liquidity'])
    dead_count = sum(1 for r in results if r['status'] in ('DEAD_NO_LIQ','DEAD_RUGGED'))
    alive_with_price = [r for r in results if r['current_price'] > 0 and r['has_liquidity']]
    winners = [r for r in alive_with_price if r['pct_change'] > 0]
    losers = [r for r in alive_with_price if r['pct_change'] <= 0]
    
    print(f'\n--- Key Metrics ---')
    print(f'Tokens checked:          {len(results)}')
    print(f'Tradeable (liq + price): {len(alive_with_price)}')
    print(f'Dead/Rugged:             {dead_count} ({dead_count/len(results)*100:.1f}%)')
    print(f'Up:  {len(winners)} ({len(winners)/max(len(alive_with_price),1)*100:.1f}% of tradeable)')
    print(f'Down: {len(losers)} ({len(losers)/max(len(alive_with_price),1)*100:.1f}% of tradeable)')
    if winners:
        print(f'Avg winner: +{sum(r["pct_change"] for r in winners)/len(winners):.1f}%')
    if losers:
        print(f'Avg loser: -{sum(abs(r["pct_change"]) for r in losers)/len(losers):.1f}%')
    
    # Per signal type
    print(f'\n--- Per Signal Type ---')
    signal_type_tokens = defaultdict(list)
    for r in results:
        for st in r['signal_types']:
            signal_type_tokens[st].append(r)
    for st in ['accumulation','large_buy','concentration','smart_exit']:
        if st not in signal_type_tokens:
            continue
        tokens_with = signal_type_tokens[st]
        alive = [t for t in tokens_with if t['current_price'] > 0 and t['has_liquidity']]
        dead = [t for t in tokens_with if t['status'] in ('DEAD_NO_LIQ','DEAD_RUGGED')]
        w = [t for t in alive if t['pct_change'] > 0]
        print(f'  {st:20s}: {len(tokens_with):4d} tok | 🟢{len(w):3d} up, 🔴{len(alive)-len(w):3d} dn, 💀{len(dead):3d} dead | win {len(w)/max(len(alive),1)*100:5.1f}%')
    
    # Top 5
    if alive_with_price:
        sorted_up = sorted(alive_with_price, key=lambda r: -r['pct_change'])[:5]
        sorted_down = sorted(alive_with_price, key=lambda r: r['pct_change'])[:5]
        print(f'\n--- Top 5 Best ---')
        for r in sorted_up:
            print(f'  🚀 {r["symbol"]:12s} {r["pct_change"]:+8.1f}% | mcap ${r["current_mcap"]:,.0f} | {",".join(r["signal_types"])}')
        print(f'\n--- Top 5 Worst ---')
        for r in sorted_down:
            print(f'  🔴 {r["symbol"]:12s} {r["pct_change"]:+8.1f}% | mcap ${r["current_mcap"]:,.0f} | {",".join(r["signal_types"])}')
    
    # Confidence analysis
    print(f'\n--- By Confidence Level ---')
    conf_buckets = [('low (50-69)', 50, 70), ('med (70-79)', 70, 80), ('high (80-89)', 80, 90), ('vhigh (90+)', 90, 200)]
    # Re-load signals to get confidence per token
    token_confidence = {}
    with open(SIGNALS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                addr = s.get('token_address', '')
                if addr in sample:
                    conf = to_float(s.get('confidence', 70))
                    if addr not in token_confidence or conf > token_confidence[addr]:
                        token_confidence[addr] = conf
            except:
                pass
    
    for label, lo, hi in conf_buckets:
        conf_tokens = [r for r in results if token_confidence.get(r['address'], 70) >= lo and token_confidence.get(r['address'], 70) < hi]
        if not conf_tokens:
            continue
        alive_ct = [t for t in conf_tokens if t['current_price'] > 0 and t['has_liquidity']]
        win_ct = [t for t in alive_ct if t['pct_change'] > 0]
        dead_ct = [t for t in conf_tokens if t['status'] in ('DEAD_NO_LIQ','DEAD_RUGGED')]
        print(f'  {label:15s}: {len(conf_tokens):3d} tok | 🟢{len(win_ct):2d} 🤍{len(alive_ct)-len(win_ct):2d} 💀{len(dead_ct):2d} | win {len(win_ct)/max(len(alive_ct),1)*100:5.1f}%')
    
    if dead_count > 0:
        print(f'\n--- Rugged/Dead Tokens (sample) ---')
        for r in [r for r in results if r['status'] in ('DEAD_NO_LIQ','DEAD_RUGGED')][:8]:
            print(f'  ☠️ {r["symbol"]:12s} | types: {",".join(r["signal_types"])} | holders: {r["holders"]}')
    
    print(f'\nAPI errors: {errors}/{len(results)}')

if __name__ == '__main__':
    main()
