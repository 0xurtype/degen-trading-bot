#!/usr/bin/env python3
import json, sys
from datetime import datetime, timezone
from collections import Counter

with open('/tmp/GMGN-scanner/data/bot_state.json') as f:
    state = json.load(f)

positions = state.get('positions', {})
closed = state.get('closed_trades', [])

# ── Open positions ──
print(f"=== OPEN POSITIONS ({len(positions)}) ===")
for token, p in positions.items():
    age_min = (datetime.now(timezone.utc).timestamp() - p['entry_ts']) / 60
    print(f"  {p['token_name']:12s} | mcap=${p['entry_mcap']:>10,.0f} | org={p['organic_score']:.0f} | src={p['source_count']} | snip={p['sniper_count']} | topH={p['top_holders_pct']:.0f}% | liq=${p['liquidity_usd']:,.0f} | age={age_min:.0f}m")

# ── Closed trades ──
pre = [t for t in closed if t.get('organic_score', 0) == 0 and t.get('source_count', 0) == 0]
post = [t for t in closed if t.get('organic_score', 0) > 0 or t.get('source_count', 0) > 0]

print(f"\n=== CLOSED TRADES: {len(closed)} total (pre-Charon={len(pre)}, post-Charon={len(post)}) ===")

def analyze(trades, label):
    if not trades:
        print(f"\n  {label}: 0 trades"); return
    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] < 0]
    be = [t for t in trades if t['pnl_pct'] == 0]
    total_pnl = sum(t['pnl_sol'] for t in trades)
    wr = len(wins)/len(trades)*100
    print(f"\n  ── {label} ({len(trades)} trades) ──")
    print(f"  W/L/BE: {len(wins)}/{len(losses)}/{len(be)} | WR: {wr:.1f}% | PnL: {total_pnl:+.4f} SOL")
    if wins:
        print(f"  Avg win:  {sum(t['pnl_pct'] for t in wins)/len(wins):+.1f}% ({sum(t['pnl_sol'] for t in wins)/len(wins):+.4f} SOL)")
    if losses:
        print(f"  Avg loss: {sum(t['pnl_pct'] for t in losses)/len(losses):+.1f}% ({sum(t['pnl_sol'] for t in losses)/len(losses):+.4f} SOL)")
    reasons = Counter(t.get('exit_reason','?') for t in trades)
    print(f"  Exit reasons: {dict(reasons)}")
    ht = [(t['exit_ts']-t['entry_ts'])/60 for t in trades if t['exit_ts']>0]
    if ht: print(f"  Hold time: avg={sum(ht)/len(ht):.1f}m min={min(ht):.1f}m max={max(ht):.1f}m")
    mcaps = [t['entry_mcap'] for t in trades if t['entry_mcap']>0]
    if mcaps: print(f"  MCAP: ${min(mcaps):,.0f}-${max(mcaps):,.0f} avg=${sum(mcaps)/len(mcaps):,.0f}")
    wk = [t['wallet_count'] for t in trades if t.get('wallet_count',0)>0]
    cv = [t['smart_conviction'] for t in trades if t.get('smart_conviction',0)>0]
    if wk: print(f"  Smart $$: wallets avg={sum(wk)/len(wk):.0f} conviction avg={sum(cv)/len(cv):.1f}")

    st = sorted(trades, key=lambda t: t['pnl_pct'])
    print(f"  Worst 5:")
    for t in st[:5]:
        print(f"    {t['token_name']:12s} pnl={t['pnl_pct']:+.1f}%({t['pnl_sol']:+.4f}SOL) reason={t.get('exit_reason','')} mcap=${t['entry_mcap']:,.0f} w={t.get('wallet_count',0)} c={t.get('smart_conviction',0)}")
    if len(st)>5:
        print(f"  Best 5:")
        for t in st[-5:]:
            print(f"    {t['token_name']:12s} pnl={t['pnl_pct']:+.1f}%({t['pnl_sol']:+.4f}SOL) reason={t.get('exit_reason','')} mcap=${t['entry_mcap']:,.0f} w={t.get('wallet_count',0)} c={t.get('smart_conviction',0)}")

    # Zero-PnL analysis (entry==exit = couldn't sell)
    zero_pnl = [t for t in trades if t['pnl_pct'] == 0 and t['entry_price'] == t['exit_price'] and t['exit_price'] > 0]
    if zero_pnl:
        print(f"  ⚠ Zero-PnL (entry==exit, same price): {len(zero_pnl)} trades — bot exited but didn't actually sell at profit/loss")

analyze(pre, "PRE-CHARON")
analyze(post, "POST-CHARON")
analyze(closed, "ALL COMBINED")

# ── Signal log stats ──
print(f"\n=== SIGNAL LOG ===")
total_signals = 0
charon_signals = 0
smart_money_signals = 0
first_ts = None
last_ts = None
try:
    with open('/tmp/GMGN-scanner/data/signals_log.jsonl') as f:
        for line in f:
            total_signals += 1
            try:
                d = json.loads(line)
                ts = d.get('timestamp', d.get('ts', 0))
                if first_ts is None: first_ts = ts
                last_ts = ts
                if d.get('source') == 'charon' or d.get('organic_score', 0) > 0:
                    charon_signals += 1
                elif d.get('wallet_count', 0) > 0 or d.get('source') == 'smart_money':
                    smart_money_signals += 1
            except:
                pass
            if total_signals % 500000 == 0:
                print(f"  ...processed {total_signals:,} signals...", flush=True)
except Exception as e:
    print(f"  Error reading signal log: {e}")

print(f"  Total signals: {total_signals:,}")
print(f"  Charon signals: {charon_signals:,}")
print(f"  Smart money signals: {smart_money_signals:,}")
if first_ts and last_ts:
    span_hrs = (last_ts - first_ts) / 3600
    print(f"  Time span: {span_hrs:.1f} hours ({first_ts} → {last_ts})")
    print(f"  Rate: {total_signals/span_hrs:.0f} signals/hr" if span_hrs > 0 else "")
