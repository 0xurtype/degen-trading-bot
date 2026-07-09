#!/usr/bin/env python3
"""
Charon Signal Backtest — Validate new filters against historical data
=====================================================================

Strategy:
  1. Poll Charon API for current token snapshots (repeated polls)
  2. Cross-reference with historical signals_log.jsonl
  3. For each overlapping token, record its historical outcome
  4. Compare win rate: Charon-filtered vs unfiltered
  5. Find optimal gate thresholds via grid search

Usage:
  python3 backtest_charon.py              # Full analysis
  python3 backtest_charon.py --quick       # Single poll, faster
  python3 backtest_charon.py --grid        # Grid search over thresholds
  python3 backtest_charon.py --bare        # Minimal output (Discord-friendly)

Output:
  - Overlap table showing token-by-token comparison
  - Win rate breakout by Charon field
  - Optimal threshold recommendations
"""

import json
import os
import sys
import time
import asyncio
import aiohttp
from pathlib import Path
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / 'data'
SIGNALS_LOG = DATA_DIR / 'signals_log.jsonl'

CHARON_API_URL = os.getenv('CHARON_API_URL', 'https://api.thecharon.xyz/api')
CHARON_API_KEY = os.getenv('CHARON_API_KEY', 'bb1eba8198941bfbac811d6e49b06a700419ec45471918ff')
CHARON_POLLS = 5  # Number of polls to accumulate tokens

# Gate ranges for grid search
SEARCH_GATES = {
    'organicScore': [0, 20, 40, 50, 60, 70, 80],
    'sourceCount': [1, 2, 3, 4],
    'sniperCount': [10, 25, 50, 75, 100, 200],
    'topHoldersPct': [30, 40, 50, 60, 70, 100],
}

# ─────────────────────────────────────────────────────────────────────────────
# CHARON CLIENT (lightweight, no dedup)
# ─────────────────────────────────────────────────────────────────────────────

class CharonClient:
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def poll(self) -> list[dict]:
        if not self.session:
            return []
        try:
            url = f'{self.api_url}/signals?limit=100&minSources=2'
            headers = {'x-api-key': self.api_key}
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get('signals', [])
        except:
            return []

# ─────────────────────────────────────────────────────────────────────────────
# HISTORICAL SIGNAL LOADER
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TokenHistory:
    address: str
    name: str = ''
    entries: list = field(default_factory=list)
    exits: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    max_price: float = 0.0
    min_price: float = float('inf')
    first_seen: float = 0.0
    last_seen: float = 0.0
    outcome_pct: float = 0.0  # Max gain after concentration signal
    outcome_label: str = 'unknown'
    smart_wallet_action: str = 'unknown'

def load_historical_signals() -> dict[str, TokenHistory]:
    """Load signals_log.jsonl and group by token address."""
    if not SIGNALS_LOG.exists():
        print(f'[ERROR] {SIGNALS_LOG} not found')
        sys.exit(1)

    tokens: dict[str, TokenHistory] = {}

    with open(SIGNALS_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except:
                continue

            addr = d.get('token_address', d.get('token', ''))
            if not addr:
                continue

            if addr not in tokens:
                tokens[addr] = TokenHistory(address=addr)

            t = tokens[addr]
            t.signals.append(d)

            ts = d.get('timestamp', 0)
            t.last_seen = max(t.last_seen, ts)
            if t.first_seen == 0 or ts < t.first_seen:
                t.first_seen = ts

            price = d.get('price', 0) or d.get('current_price', 0)
            mcap = d.get('mcap', 0)

            if price > 0:
                t.max_price = max(t.max_price, price)
                t.min_price = min(t.min_price, price)

            stype = d.get('type', '')
            if stype == 'concentration' or stype == 'accumulation':
                t.entries.append(d)
            elif stype == 'smart_exit':
                t.exits.append(d)

    return tokens

# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def compute_outcomes(tokens: dict[str, TokenHistory]) -> dict[str, TokenHistory]:
    """Compute outcomes for each token based on signal data."""
    for addr, t in tokens.items():
        if not t.entries:
            t.outcome_label = 'no_entry_signal'
            continue

        # Look at entry → exit: did smart wallets profit?
        if t.exits:
            # Net position change: what happened after entry
            last_entry = max(t.entries, key=lambda e: e.get('timestamp', 0))
            last_exit = max(t.exits, key=lambda e: e.get('timestamp', 0))

            # If we have price data, compute PnL
            if t.min_price < float('inf') and t.max_price > 0:
                entry_price = last_entry.get('price', 0) or last_entry.get('current_price', 0)
                exit_price = last_exit.get('price', 0) or last_exit.get('current_price', 0)

                if entry_price > 0 and exit_price > 0:
                    t.outcome_pct = (exit_price - entry_price) / entry_price * 100

            # Classify based on smart wallet action
            sell_usd = last_exit.get('sell_usd', 0)
            remaining = last_exit.get('remaining_balance', 0)

            if sell_usd > 0 and remaining == 0:
                t.smart_wallet_action = 'full_exit'
                if t.outcome_pct >= 5:
                    t.outcome_label = 'win'
                elif t.outcome_pct <= -5:
                    t.outcome_label = 'loss'
                else:
                    t.outcome_label = 'flat'
            else:
                t.smart_wallet_action = 'partial_exit'
                t.outcome_label = 'mixed'

        # No exit signal — either still holding or rugged without signal
        if not t.exits:
            # If we have price history and max is significantly above entry
            if t.entries and t.max_price > 0:
                first_entry = min(t.entries, key=lambda e: e.get('timestamp', 0))
                entry_price = first_entry.get('price', 0) or first_entry.get('current_price', 0)
                if entry_price > 0:
                    gain = (t.max_price - entry_price) / entry_price * 100
                    t.outcome_pct = gain
                    if gain >= 20:
                        t.outcome_label = 'likely_win (20%+ peak)'
                    elif gain >= 5:
                        t.outcome_label = 'modest (5-20% peak)'
                    else:
                        t.outcome_label = 'no_exit_seen'

    return tokens

# ─────────────────────────────────────────────────────────────────────────────
# CHARON MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def extract_charon_metrics(raw: dict) -> dict:
    """Extract key metrics from a Charon signal dict."""
    trending = raw.get('trending') or {}
    graduated = raw.get('graduated') or {}
    fee_claim = raw.get('feeClaim')

    return {
        'mint': raw.get('mint', ''),
        'name': raw.get('name', ''),
        'symbol': raw.get('symbol', ''),
        'price_usd': raw.get('priceUsd', 0),
        'market_cap_usd': raw.get('marketCapUsd', 0),
        'liquidity_usd': raw.get('liquidityUsd', 0),
        'holders': raw.get('holders', 0),
        'age_ms': raw.get('ageMs', 0),
        'bonding_complete': raw.get('bondingComplete', False),
        'source_count': raw.get('sourceCount', 1),
        'sources': raw.get('sources', []),
        'organic_score': trending.get('organicScore', 0),
        'organic_label': trending.get('organicScoreLabel', 'unknown'),
        'volume_24h': raw.get('volume24h', 0),
        'volume_5m': raw.get('volume5m', 0),
        'graduated': bool(graduated),
        'sniper_count': graduated.get('sniperCount', 0),
        'top_holders_pct': graduated.get('topHoldersPercent', 0),
        'dev_holdings_pct': graduated.get('devHoldingsPercent', 0),
        'has_fee_claim': bool(fee_claim),
        'fee_distributed_sol': (fee_claim.get('distributedSol', 0) if fee_claim else 0),
        'buys_5m': trending.get('buys', 0),
        'sells_5m': trending.get('sells', 0),
        'buy_volume_5m': trending.get('buyVolume', 0),
    }

def passes_gates(metrics: dict, gates: dict) -> tuple[bool, list[str]]:
    """Check if a Charon signal passes specified gates. Returns (pass, failures)."""
    failures = []

    # Organic score
    min_organic = gates.get('organicScore', 0)
    score = metrics.get('organic_score', 0)
    if score < min_organic:
        failures.append(f'organic {score:.0f} < {min_organic}')

    # Source count
    min_sources = gates.get('sourceCount', 0)
    sc = metrics.get('source_count', 0)
    if sc < min_sources:
        failures.append(f'sources {sc} < {min_sources}')

    # Sniper count
    max_snipers = gates.get('sniperCount', 999)
    sniper = metrics.get('sniper_count', 0)
    if sniper > max_snipers:
        failures.append(f'snipers {sniper} > {max_snipers}')

    # Top holders %
    max_holders = gates.get('topHoldersPct', 100)
    top = metrics.get('top_holders_pct', 0)
    if top > max_holders:
        failures.append(f'topHolders {top:.0f}% > {max_holders}%')

    return len(failures) == 0, failures

# ─────────────────────────────────────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────────────────────────────────────

def fmt(v):
    if v is None or v == 0:
        return '0'
    if abs(v) >= 1_000_000:
        return f'{v/1_000_000:.1f}M'
    if abs(v) >= 1_000:
        return f'{v/1_000:.0f}K'
    if abs(v) >= 1:
        return f'{v:.2f}'
    return f'{v:.8f}'

def fmt_pct(v):
    if v >= 0: return f'+{v:.1f}%'
    return f'{v:.1f}%'

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def collect_charon_data(polls: int) -> dict[str, dict]:
    """Collect Charon data from multiple polls."""
    all_signals: dict[str, dict] = {}

    async with CharonClient(CHARON_API_URL, CHARON_API_KEY) as cc:
        for i in range(polls):
            signals = await cc.poll()
            for s in signals:
                mint = s.get('mint', '')
                if mint:
                    all_signals[mint] = s
            if i < polls - 1:
                await asyncio.sleep(3)  # Small gap between polls

    return all_signals

def print_overlap_table(charon_metrics: list, token_outcomes: dict):
    """Print detailed overlap table."""
    print(f'\n{"="*80}')
    print(f'  TOKEN OVERLAP TABLE — Charon vs Historical')
    print(f'{"="*80}')
    print(f'  {"Symbol":16s} {"MCAP":>10s} {"Organic":>7s} {"Src":>4s} {"Snip":>5s} {"Top10":>6s} {"Liq":>8s} {"History":>20s}')
    print(f'  {"-"*16} {"-"*10} {"-"*7} {"-"*4} {"-"*5} {"-"*6} {"-"*8} {"-"*20}')

    for m in sorted(charon_metrics, key=lambda x: x.get('organic_score', 0), reverse=True):
        addr = m['mint']
        outcome = token_outcomes.get(addr)
        label = outcome.outcome_label if outcome else 'not_in_historical'

        print(f'  {m["symbol"]:16s} {fmt(m["market_cap_usd"]):>10s} {m["organic_score"]:>6.0f}% {m["source_count"]:>4d} {m["sniper_count"]:>5d} {m["top_holders_pct"]:>5.1f}% {fmt(m["liquidity_usd"]):>8s} {label:>20s}')

def analyze_gate_performance(charon_metrics: list[dict], token_outcomes: dict, default_gates: dict):
    """Analyze how each gate affects win/loss ratio."""
    print(f'\n{"="*80}')
    print(f'  GATE PERFORMANCE ANALYSIS')
    print(f'{"="*80}')

    total = len(charon_metrics)
    overlap_count = sum(1 for m in charon_metrics if m['mint'] in token_outcomes)
    print(f'  Charon tokens: {total}')
    print(f'  In historical data: {overlap_count}')

    # Baseline: no gates
    baseline_wins = 0
    baseline_losses = 0
    baseline_flat = 0
    baseline_none = 0
    for m in charon_metrics:
        addr = m['mint']
        outcome = token_outcomes.get(addr)
        if outcome:
            if outcome.outcome_label == 'win':
                baseline_wins += 1
            elif outcome.outcome_label == 'loss':
                baseline_losses += 1
            elif outcome.outcome_label in ('flat', 'mixed', 'modest (5-20% peak)'):
                baseline_flat += 1
            else:
                baseline_none += 1

    total_known = baseline_wins + baseline_losses + baseline_flat
    baseline_winrate = (baseline_wins / total_known * 100) if total_known > 0 else 0

    print(f'\n  BASELINE (no Charon gates)')
    print(f'  Wins: {baseline_wins} | Losses: {baseline_losses} | Flat/Mixed: {baseline_flat} | Unknown: {baseline_none}')
    print(f'  Win rate: {baseline_winrate:.1f}% ({baseline_wins}/{total_known})')

    # Apply current default gates
    current_wins = 0
    current_losses = 0
    current_flat = 0
    current_none = 0
    current_total = 0

    for m in charon_metrics:
        passes, fails = passes_gates(m, default_gates)
        if not passes:
            continue
        current_total += 1
        addr = m['mint']
        outcome = token_outcomes.get(addr)
        if outcome:
            if outcome.outcome_label == 'win':
                current_wins += 1
            elif outcome.outcome_label == 'loss':
                current_losses += 1
            elif outcome.outcome_label in ('flat', 'mixed', 'modest (5-20% peak)'):
                current_flat += 1
            else:
                current_none += 1

    current_known = current_wins + current_losses + current_flat
    current_winrate = (current_wins / current_known * 100) if current_known > 0 else 0

    print(f'\n  ✅ CURRENT GATES: organic≥{default_gates["organicScore"]} | sources≥{default_gates["sourceCount"]} | snipers≤{default_gates["sniperCount"]} | topHolders≤{default_gates["topHoldersPct"]}%')
    print(f'  Passed: {current_total}/{total}')
    print(f'  Wins: {current_wins} | Losses: {current_losses} | Flat/Mixed: {current_flat} | Unknown: {current_none}')
    print(f'  Win rate: {current_winrate:.1f}% ({current_wins}/{current_known})')
    print(f'  vs baseline: {current_winrate - baseline_winrate:+.1f}pp')

    # Per-gate analysis
    print(f'\n  ── Per-Gate Impact ──')
    for gate_name, values in SEARCH_GATES.items():
        print(f'\n  {gate_name}:')
        for v in values:
            test_gates = dict(default_gates)
            if gate_name == 'organicScore':
                test_gates['organicScore'] = v
            elif gate_name == 'sourceCount':
                test_gates['sourceCount'] = v
            elif gate_name == 'sniperCount':
                test_gates['sniperCount'] = v
            elif gate_name == 'topHoldersPct':
                test_gates['topHoldersPct'] = v

            gw = 0
            gl = 0
            gf = 0
            pt = 0
            for m in charon_metrics:
                passes, _ = passes_gates(m, test_gates)
                if not passes:
                    continue
                pt += 1
                outcome = token_outcomes.get(m['mint'])
                if outcome:
                    if outcome.outcome_label == 'win':
                        gw += 1
                    elif outcome.outcome_label == 'loss':
                        gl += 1
                    elif outcome.outcome_label in ('flat', 'mixed', 'modest (5-20% peak)'):
                        gf += 1

            known = gw + gl + gf
            wr = (gw / known * 100) if known > 0 else 0
            print(f'    >= {v:5s} → pass {pt:3d}/{total} | win {gw:2d} loss {gl:2d} flat {gf:2d} | WR {wr:5.1f}% ({gw}/{known})')

    return baseline_winrate, current_winrate

def grid_search(charon_metrics: list[dict], token_outcomes: dict):
    """Full grid search over gate combinations."""
    print(f'\n{"="*80}')
    print(f'  GRID SEARCH — Optimal Gate Combinations')
    print(f'{"="*80}')

    from itertools import product

    results = []

    # Sample the grid: reduce granularity to keep it fast
    organic_vals = [0, 30, 50, 70]
    source_vals = [1, 2, 3]
    sniper_vals = [25, 50, 100, 200]
    holder_vals = [30, 50, 70, 100]

    total_combos = len(organic_vals) * len(source_vals) * len(sniper_vals) * len(holder_vals)
    checked = 0

    for organic, sources, snipers, holders in product(organic_vals, source_vals, sniper_vals, holder_vals):
        gates = {
            'organicScore': organic,
            'sourceCount': sources,
            'sniperCount': snipers,
            'topHoldersPct': holders,
        }

        passed_tokens = []
        for m in charon_metrics:
            passes, _ = passes_gates(m, gates)
            if passes:
                passed_tokens.append(m)

        if not passed_tokens:
            continue

        wins = 0
        losses = 0
        flat = 0
        for m in passed_tokens:
            outcome = token_outcomes.get(m['mint'])
            if outcome:
                if outcome.outcome_label == 'win':
                    wins += 1
                elif outcome.outcome_label == 'loss':
                    losses += 1
                elif outcome.outcome_label in ('flat', 'mixed', 'modest (5-20% peak)'):
                    flat += 1

        known = wins + losses + flat
        if known < 3:  # Skip if too few data points
            continue

        winrate = wins / known * 100
        pass_rate = len(passed_tokens) / len(charon_metrics) * 100

        results.append({
            'gates': gates,
            'passed': len(passed_tokens),
            'pass_rate': pass_rate,
            'wins': wins,
            'losses': losses,
            'flat': flat,
            'winrate': winrate,
        })

        checked += 1

    # Sort by win rate (desc)
    results.sort(key=lambda r: r['winrate'], reverse=True)

    print(f'\n  Top 10 configurations (by win rate, min 3 known outcomes):')
    print(f'  {"#":>3s} | Organic | Sources | Snipers | Top10% | Pass | Pass%% | Wins | Loss | Flat | WR')
    print(f'  {"-"*3} | {"-"*7} | {"-"*7} | {"-"*7} | {"-"*7} | {"-"*4} | {"-"*6} | {"-"*4} | {"-"*4} | {"-"*4} | {"-"*4}')

    for i, r in enumerate(results[:15]):
        g = r['gates']
        print(f'  {i+1:3d} | {g["organicScore"]:>7d} | {g["sourceCount"]:>7d} | {g["sniperCount"]:>7d} | {g["topHoldersPct"]:>7d} | {r["passed"]:>4d} | {r["pass_rate"]:>5.1f}% | {r["wins"]:>4d} | {r["losses"]:>4d} | {r["flat"]:>4d} | {r["winrate"]:>5.1f}%')

    # Best configs by different objectives
    print(f'\n  ── Best configurations by objective ──')

    # Highest win rate (min 10 passed)
    high_wr = [r for r in results if r['wins'] + r['losses'] + r['flat'] >= 10]
    if high_wr:
        best = high_wr[0]
        g = best['gates']
        print(f'  Best WR (≥10 known): {best["winrate"]:.1f}% — organic≥{g["organicScore"]} sources≥{g["sourceCount"]} snipers≤{g["sniperCount"]} topHolders≤{g["topHoldersPct"]}%')

    # Best expected value (win rate × pass rate) — balance of selectivity and opportunity
    for r in results:
        r['ev'] = r['winrate'] * r['pass_rate'] / 100
    results.sort(key=lambda r: r['ev'], reverse=True)
    if high_wr:
        best_ev = results[0]
        g = best_ev['gates']
        print(f'  Best EV (WR×Pass): {best_ev["ev"]:.1f} — organic≥{g["organicScore"]} sources≥{g["sourceCount"]} snipers≤{g["sniperCount"]} topHolders≤{g["topHoldersPct"]}%')

    # Highest improvement over baseline
    return results

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    args = sys.argv[1:]

    bare_mode = '--bare' in args
    grid_mode = '--grid' in args
    quick_mode = '--quick' in args

    polls = 2 if quick_mode else CHARON_POLLS

    if not bare_mode:
        print(f'🤖 Charon Backtest v1.0')
        print(f'  Polls: {polls}')
        print(f'  Historical: {SIGNALS_LOG}')
        print()

    # Step 1: Load historical data
    if not bare_mode:
        print('📂 Loading historical signals...', end=' ', flush=True)
    token_outcomes = load_historical_signals()
    compute_outcomes(token_outcomes)
    if not bare_mode:
        print(f'{len(token_outcomes)} tokens')

        # Quick stats
        wins = sum(1 for t in token_outcomes.values() if t.outcome_label == 'win')
        losses = sum(1 for t in token_outcomes.values() if t.outcome_label == 'loss')
        flat = sum(1 for t in token_outcomes.values() if t.outcome_label in ('flat', 'mixed', 'modest (5-20% peak)'))
        print(f'  Historical: {wins} wins, {losses} losses, {flat} flat/mixed')

    # Step 2: Poll Charon
    if not bare_mode:
        print('📡 Polling Charon API...', end=' ', flush=True)
    charon_data = await collect_charon_data(polls)
    if not bare_mode:
        print(f'{len(charon_data)} unique tokens collected')

    # Step 3: Build Charon metrics list
    charon_metrics = [extract_charon_metrics(s) for s in charon_data.values()]

    # Step 4: Find overlap
    overlap = [m for m in charon_metrics if m['mint'] in token_outcomes]
    if not bare_mode:
        print(f'🔗 Overlap: {len(overlap)} tokens (appear in both Charon + historical)')

    if len(overlap) < 5:
        print(f'\n⚠️  Only {len(overlap)} overlapping tokens — results may be noisy')
        print(f'   Try running again in ~30s to capture more tokens')

    # Current default gates
    default_gates = {
        'organicScore': 50,
        'sourceCount': 2,
        'sniperCount': 50,
        'topHoldersPct': 50,
    }

    # Analyze
    baseline_wr, current_wr = analyze_gate_performance(charon_metrics, token_outcomes, default_gates)

    # Overlap table (skip if bare)
    if not bare_mode and overlap:
        print_overlap_table(overlap, token_outcomes)

    # Grid search
    if grid_mode:
        grid_results = grid_search(charon_metrics, token_outcomes)
    elif bare_mode:
        pass  # Minimal output
    else:
        # Offer grid search guidance
        print(f'\n  Tip: Run with --grid for full threshold optimization')

    # Final summary
    print(f'\n{"="*80}')
    print(f'  SUMMARY')
    print(f'{"="*80}')

    total = len(charon_metrics)
    ov_count = len(overlap)
    current_level = current_wr

    win_known = sum(1 for m in charon_metrics if m['mint'] in token_outcomes and token_outcomes[m['mint']].outcome_label in ('win', 'loss', 'flat', 'mixed', 'modest (5-20% peak)'))
    win_wins = sum(1 for m in charon_metrics if m['mint'] in token_outcomes and token_outcomes[m['mint']].outcome_label == 'win')

    projection_msg = f'Current WR: {baseline_wr:.1f}% → Charon filtered: {current_wr:.1f}% ({"+" if current_wr > baseline_wr else ""}{current_wr - baseline_wr:.1f}pp)' if baseline_wr > 0 else ''

    print(f'  Charon sample size:     {total} tokens')
    print(f'  Historical overlap:     {ov_count} tokens')
    print(f'  Baseline win rate:      {baseline_wr:.1f}%')
    print(f'  Charon-filtered WR:     {current_wr:.1f}%')
    print(f'  Improvement:            {current_wr - baseline_wr:+.1f}pp')
    print()
    print(f'  Current gates:          organic≥{default_gates["organicScore"]} sources≥{default_gates["sourceCount"]} snipers≤{default_gates["sniperCount"]} topHolders≤{default_gates["topHoldersPct"]}%')
    print()
    print(f'  Recommendation:')

    # Give practical advice based on results
    if current_wr > baseline_wr + 10:
        print(f'  ✅ Charon gates show significant improvement potential')
    elif current_wr > baseline_wr:
        print(f'  ✅ Charon gates modestly improve win rate')
    elif current_wr >= baseline_wr - 5:
        print(f'  ⚠️  Charon gates roughly neutral — benefit is in additional data not reflected in historical overlap')
    else:
        print(f'  ⚠️  Overlap too small for meaningful comparison. Run again with more polls.')

    if grid_mode:
        print(f'  Run grid search results above to find optimal thresholds.')

if __name__ == '__main__':
    asyncio.run(main())
