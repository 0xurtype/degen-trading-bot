#!/usr/bin/env python3
"""Full trade analysis — Session 3 dry-run results."""
import json
from datetime import datetime, timezone
from collections import Counter

with open('/tmp/GMGN-scanner/data/bot_state.json') as f:
    state = json.load(f)

positions = state.get('positions', {})
closed = state.get('closed_trades', [])

# ── Separate by era ──
# Pre-fix: organic=0 AND source=0 (old phantom trades)
# Post-fix: has Charon data (organic>0 OR source>0)
pre = [t for t in closed if t.get('organic_score',0)==0 and t.get('source_count',0)==0]
post = [t for t in closed if t.get('organic_score',0)>0 or t.get('source_count',0)>0]

print("=" * 70)
print("SESSION 3 DRY-RUN ANALYSIS")
print("=" * 70)

# ── Open positions ──
print(f"\n📊 OPEN POSITIONS ({len(positions)})")
for p in positions.values():
    print(f"  {p['token_name']:12s} | entry={p['entry_price']:.2e} | peak={p['peak_price']:.2e} | org={p.get('organic_score',0):.0f} | src={p.get('source_count',0)}")

def analyze(trades, label):
    if not trades:
        print(f"\n  {label}: 0 trades"); return
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] < 0]
    be = [t for t in trades if t['pnl_pct'] == 0]
    total_pnl = sum(t['pnl_sol'] for t in trades)
    wr = len(wins)/len(trades)*100
    
    print(f"\n{'─'*50}")
    print(f"  {label} ({len(trades)} trades)")
    print(f"{'─'*50}")
    print(f"  W/L/BE:    {len(wins)}/{len(losses)}/{len(be)}")
    print(f"  Win rate:  {wr:.1f}%")
    print(f"  Total PnL: {total_pnl:+.4f} SOL")
    
    if wins:
        avg_win = sum(t['pnl_pct'] for t in wins)/len(wins)
        print(f"  Avg win:   {avg_win:+.1f}% ({sum(t['pnl_sol'] for t in wins)/len(wins):+.4f} SOL)")
    if losses:
        avg_loss = sum(t['pnl_pct'] for t in losses)/len(losses)
        print(f"  Avg loss:  {avg_loss:+.1f}% ({sum(t['pnl_sol'] for t in losses)/len(losses):+.4f} SOL)")
    
    # Risk:Reward
    if wins and losses:
        avg_w = sum(t['pnl_pct'] for t in wins)/len(wins)
        avg_l = abs(sum(t['pnl_pct'] for t in losses)/len(losses))
        rr = avg_w / avg_l if avg_l > 0 else float('inf')
        print(f"  Risk:Reward: 1:{rr:.1f}")
    
    # Exit reasons
    reasons = Counter(t.get('exit_reason','?') for t in trades)
    print(f"  Exits: {dict(reasons)}")
    
    # Hold times
    ht = [(t['exit_ts']-t['entry_ts'])/60 for t in trades if t['exit_ts']>0]
    if ht:
        print(f"  Hold: avg={sum(ht)/len(ht):.0f}m min={min(ht):.0f}m max={max(ht):.0f}m")
    
    # MCAP
    mcaps = [t['entry_mcap'] for t in trades if t['entry_mcap']>0]
    if mcaps:
        print(f"  MCAP: ${min(mcaps):,.0f}-${max(mcaps):,.0f} avg=${sum(mcaps)/len(mcaps):,.0f}")
    
    # Zero-PnL phantom trades
    zero = [t for t in trades if t['pnl_pct']==0 and t['entry_price']==t.get('exit_price',0) and t['exit_price']>0]
    if zero:
        pct = len(zero)/len(trades)*100
        print(f"  ⚠ Phantom 0% trades: {len(zero)}/{len(trades)} ({pct:.0f}%)")
    else:
        print(f"  ✅ Phantom 0% trades: 0")

    # Per-trade detail
    st = sorted(trades, key=lambda t: t['pnl_pct'])
    print(f"\n  All trades (sorted by PnL):")
    for t in st:
        print(f"    {t['token_name']:12s} | pnl={t['pnl_pct']:+.1f}% ({t['pnl_sol']:+.4f}SOL) | {t.get('exit_reason',''):12s} | mcap=${t['entry_mcap']:,.0f} | org={t.get('organic_score',0):.0f}")

print("\n")
analyze(pre, "PRE-FIX (phantom trades, entry_price fallback)")
analyze(post, "POST-FIX (DexScreener price fetch active)")
analyze(closed, "ALL COMBINED")

# ── Improvement comparison ──
print(f"\n{'='*70}")
print("IMPROVEMENT COMPARISON: PRE-FIX vs POST-FIX")
print(f"{'='*70}")
if pre and post:
    pre_wr = len([t for t in pre if t['pnl_pct']>0])/len(pre)*100
    post_wr = len([t for t in post if t['pnl_pct']>0])/len(post)*100
    pre_pnl = sum(t['pnl_sol'] for t in pre)
    post_pnl = sum(t['pnl_sol'] for t in post)
    pre_phantom = len([t for t in pre if t['pnl_pct']==0 and t['entry_price']==t.get('exit_price',0)])/len(pre)*100
    post_phantom = len([t for t in post if t['pnl_pct']==0 and t['entry_price']==t.get('exit_price',0)])/len(post)*100
    
    print(f"  {'Metric':<25} {'Pre-fix':>12} {'Post-fix':>12} {'Delta':>12}")
    print(f"  {'─'*60}")
    print(f"  {'Trades':<25} {len(pre):>12} {len(post):>12} {len(post)-len(pre):>+12}")
    print(f"  {'Win rate':<25} {pre_wr:>11.1f}% {post_wr:>11.1f}% {post_wr-pre_wr:>+11.1f}%")
    print(f"  {'Total PnL (SOL)':<25} {pre_pnl:>+12.4f} {post_pnl:>+12.4f} {post_pnl-pre_pnl:>+12.4f}")
    print(f"  {'Phantom 0% trades':<25} {pre_phantom:>11.0f}% {post_phantom:>11.0f}% {post_phantom-pre_phantom:>+11.0f}%")
