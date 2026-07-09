#!/usr/bin/env python3
import json
import subprocess
import time
from collections import Counter

def to_float(v, default=0.0):
    try: return float(v) if v is not None else default
    except: return default

def gmgn_api(args):
    try:
        result = subprocess.run(
            ['/usr/bin/gmgn-cli'] + args,
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except:
        return None

signals = []
with open('data/signals_retro.jsonl') as f:
    for line in f:
        if line.strip():
            try:
                signals.append(json.loads(line))
            except:
                pass

tokens = list(set(s.get('token_address', '') for s in signals if s.get('token_address')))
print(f'Unique retro tokens: {len(tokens)}', flush=True)

conc_signals = [s for s in signals if s.get('type') == 'concentration']
conc_tokens = list(set(s.get('token_address', '') for s in conc_signals if s.get('token_address')))
print(f'Concentration tokens: {len(conc_tokens)}', flush=True)

limit = min(200, len(conc_tokens))
sample_tokens = conc_tokens[:limit]

print(f'\nChecking {limit} concentration tokens...', flush=True)
results = []
errors = 0

for i, addr in enumerate(sample_tokens):
    if i % 20 == 0 and i > 0:
        print(f'  [{i}/{limit}] errors: {errors}', flush=True)
    
    info = gmgn_api(['token', 'info', '--chain', 'sol', '--address', addr])
    
    current_price = 0
    current_mcap = 0
    has_liq = False
    
    if info:
        data = info.get('data', info)
        if isinstance(data, dict):
            price_obj = data.get('price', {})
            if isinstance(price_obj, dict):
                current_price = to_float(price_obj.get('price', 0))
            else:
                current_price = to_float(price_obj, 0)
            current_mcap = to_float(data.get('market_cap', 0))
            has_liq = to_float(data.get('liquidity', 0)) > 100
    else:
        errors += 1
        results.append({'address': addr, 'status': 'API_FAIL'})
        continue
    
    if current_price > 0 and has_liq:
        results.append({'address': addr, 'status': 'ALIVE', 'mcap': current_mcap, 'price': current_price})
    elif has_liq:
        results.append({'address': addr, 'status': 'ALIVE_ZERO_PRICE', 'mcap': current_mcap})
    else:
        results.append({'address': addr, 'status': 'DEAD'})

print(f'\nResults for {len(results)} tokens:', flush=True)
statuses = Counter(r['status'] for r in results)
for s, c in statuses.most_common():
    print(f'  {s}: {c} ({c/len(results)*100:.1f}%)', flush=True)

alive = [r for r in results if r['status'] == 'ALIVE']
if alive:
    alive.sort(key=lambda r: r['mcap'], reverse=True)
    print(f'\nAlive token mcap range:', flush=True)
    print(f'  Max: ${max(r["mcap"] for r in alive):.0f}', flush=True)
    print(f'  Min: ${min(r["mcap"] for r in alive):.0f}', flush=True)
    print(f'  Med: ${alive[len(alive)//2]["mcap"]:.0f}', flush=True)
