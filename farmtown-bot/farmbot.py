#!/usr/bin/env python3
"""
FarmTown Auto-Farm Bot + Dashboard
====================================
Playwright-based bot with real-time monitoring dashboard.

SETUP:
  pip install playwright fastapi uvicorn sse-starlette
  python -m playwright install chromium

USAGE:
  python farmbot.py                         # Bot + Dashboard (localhost:3001)
  python farmbot.py --crop cucumber         # Farm specific crop
  python farmbot.py --port 3001             # Dashboard port
  python farmbot.py --headless              # No browser window

DASHBOARD:
  http://localhost:3001 — live monitoring
  Shows: farm grid, stats, action log, inventory, crop timers
"""

import asyncio
import json
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, Page, BrowserContext
except ImportError:
    print("ERROR: playwright not installed. Run:")
    print("  pip install playwright")
    print("  python -m playwright install chromium")
    sys.exit(1)

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from sse_starlette.sse import EventSourceResponse
    import uvicorn
except ImportError:
    print("ERROR: dashboard deps not installed. Run:")
    print("  pip install fastapi uvicorn sse-starlette")
    sys.exit(1)


# ─────────────────────────────────────────────
# CROP DATA
# ─────────────────────────────────────────────
CROPS = {
    "potato":       {"unlock": 1,  "cost": 0,    "grow_sec": 120,   "death_sec": 300,   "reward_gold": 60,    "xp": 4,    "emoji": "🥔"},
    "carrot":       {"unlock": 1,  "cost": 20,   "grow_sec": 120,   "death_sec": 300,   "reward_gold": 40,    "xp": 4,    "emoji": "🥕"},
    "corn":         {"unlock": 1,  "cost": 45,   "grow_sec": 300,   "death_sec": 720,   "reward_gold": 95,    "xp": 7,    "emoji": "🌽"},
    "tomato":       {"unlock": 5,  "cost": 90,   "grow_sec": 480,   "death_sec": 360,   "reward_gold": 200,   "xp": 14,   "emoji": "🍅"},
    "onion":        {"unlock": 5,  "cost": 140,  "grow_sec": 720,   "death_sec": 480,   "reward_gold": 330,   "xp": 22,   "emoji": "🧅"},
    "wheat":        {"unlock": 5,  "cost": 220,  "grow_sec": 1080,  "death_sec": 720,   "reward_gold": 560,   "xp": 32,   "emoji": "🌾"},
    "pumpkin":      {"unlock": 10, "cost": 400,  "grow_sec": 1800,  "death_sec": 1200,  "reward_gold": 1050,  "xp": 55,   "emoji": "🎃"},
    "melon":        {"unlock": 10, "cost": 650,  "grow_sec": 2700,  "death_sec": 1800,  "reward_gold": 1800,  "xp": 80,   "emoji": "🍈"},
    "cucumber":     {"unlock": 10, "cost": 850,  "grow_sec": 3600,  "death_sec": 2700,  "reward_gold": 2400,  "xp": 105,  "emoji": "🥒"},
    "pepper":       {"unlock": 15, "cost": 1300, "grow_sec": 5400,  "death_sec": 3600,  "reward_gold": 4000,  "xp": 150,  "emoji": "🌶️"},
    "strawberry":   {"unlock": 15, "cost": 1900, "grow_sec": 7200,  "death_sec": 2700,  "reward_gold": 6200,  "xp": 210,  "emoji": "🍓"},
    "blueberry":    {"unlock": 15, "cost": 2600, "grow_sec": 10800, "death_sec": 3600,  "reward_gold": 8800,  "xp": 280,  "emoji": "🫐"},
    "grape":        {"unlock": 20, "cost": 4000, "grow_sec": 14400, "death_sec": 4500,  "reward_gold": 9500,  "xp": 220,  "emoji": "🍇"},
    "eggplant":     {"unlock": 20, "cost": 5500, "grow_sec": 18000, "death_sec": 5400,  "reward_gold": 13000, "xp": 280,  "emoji": "🍆"},
    "watermelon":   {"unlock": 20, "cost": 7500, "grow_sec": 21600, "death_sec": 7200,  "reward_gold": 18000, "xp": 360,  "emoji": "🍉"},
    "dragonfruit":  {"unlock": 25, "cost": 12000,"grow_sec": 28800, "death_sec": 9000,  "reward_gold": 28000, "xp": 500,  "emoji": "🐉"},
    "pineapple":    {"unlock": 25, "cost": 18000,"grow_sec": 36000, "death_sec": 10800, "reward_gold": 42000, "xp": 700,  "emoji": "🍍"},
    "crystal_berry":{"unlock": 25, "cost": 25000,"grow_sec": 43200, "death_sec": 10800, "reward_gold": 60000, "xp": 900,  "emoji": "💎"},
    "starfruit":    {"unlock": 30, "cost": 50000,"grow_sec": 64800, "death_sec": 3600,  "reward_gold": 100000,"xp": 1200, "emoji": "⭐"},
}

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
CONFIG = {
    "farm_url": "https://play.farmtown.online/",
    "user_data_dir": str(Path.home() / ".farmtown-bot-profile"),
    "viewport": {"width": 1280, "height": 800},
    "delay_min": 0.8,
    "delay_max": 2.5,
    "action_cooldown": 1.5,
    "min_seeds": 5,
    "queue_timeout": 600,
    "pool_claim_interval": 21600,
    "dashboard_port": 3001,
}


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def random_delay():
    return random.uniform(CONFIG["delay_min"], CONFIG["delay_max"])


# ─────────────────────────────────────────────
# SHARED STATE (bot → dashboard)
# ─────────────────────────────────────────────
class SharedState:
    """Thread-safe shared state between bot and dashboard."""
    def __init__(self):
        self._lock = threading.Lock()
        self.gold = 0
        self.xp = 0
        self.level = 1
        self.stars = 0
        self.tiles = {}
        self.inventory = {}
        self.orders = []
        self.connected = False
        self.in_queue = False
        self.farm_joined = False
        self.room_id = None
        self.player_id = None
        self.crop_strategy = "auto"
        self.active_crop = None
        self.total_planted = 0
        self.total_harvested = 0
        self.total_orders = 0
        self.total_gold_earned = 0
        self.cycle_count = 0
        self.start_time = time.time()
        self.action_log = deque(maxlen=200)
        self.subscribers = []

    def to_dict(self):
        with self._lock:
            now = time.time()
            elapsed = int(now - self.start_time)
            h, m = divmod(elapsed, 3600)
            m, s = divmod(m, 60)

            # Compute tile stats
            growing = {}
            ready = {}
            empty = 0
            now_ms = int(now * 1000)
            for key, tile in self.tiles.items():
                gs = tile.get("groundState", "")
                crop_id = tile.get("cropId", "")
                if gs == "planted" and crop_id:
                    crop_name = crop_id.replace("_seed", "")
                    ready_at = tile.get("readyAt", 0)
                    if ready_at and ready_at <= now_ms:
                        ready[key] = {"crop": crop_name, "ready": True}
                    else:
                        remaining = max(0, (ready_at - now_ms) // 1000) if ready_at else 0
                        growing[key] = {"crop": crop_name, "remaining_sec": remaining}
                elif gs == "tilled" or (gs == "none" and not crop_id):
                    empty += 1

            # Inventory
            inv_display = {}
            for k, v in self.inventory.items():
                if k.endswith("_seed"):
                    crop_name = k.replace("_seed", "")
                    emoji = CROPS.get(crop_name, {}).get("emoji", "🌱")
                    inv_display[f"{emoji} {crop_name}"] = v
                else:
                    inv_display[k] = v

            return {
                "stats": {
                    "gold": self.gold,
                    "xp": self.xp,
                    "level": self.level,
                    "stars": self.stars,
                    "uptime": f"{h}h {m}m {s}s",
                    "uptime_sec": elapsed,
                    "cycles": self.cycle_count,
                    "planted": self.total_planted,
                    "harvested": self.total_harvested,
                    "orders": self.total_orders,
                    "gold_earned": self.total_gold_earned,
                },
                "status": {
                    "connected": self.connected,
                    "in_queue": self.in_queue,
                    "farm_joined": self.farm_joined,
                    "room_id": self.room_id,
                    "crop_strategy": self.crop_strategy,
                    "active_crop": self.active_crop,
                },
                "farm_grid": {
                    "growing": len(growing),
                    "ready": len(ready),
                    "empty": empty,
                    "growing_tiles": growing,
                    "ready_tiles": ready,
                },
                "inventory": inv_display,
                "orders": self.orders[:10],
                "log": list(self.action_log)[-50:],
                "crops": {k: {**v, "emoji": CROPS[k]["emoji"]} for k, v in CROPS.items()},
                "timestamp": now,
            }

    def emit_event(self, event_type, message, data=None):
        event = {
            "type": event_type,
            "message": message,
            "data": data,
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        with self._lock:
            self.action_log.append(event)
        # Notify SSE subscribers
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                self.subscribers.remove(q)

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)


SHARED = SharedState()


# ─────────────────────────────────────────────
# GAME STATE TRACKER
# ─────────────────────────────────────────────
class GameState:
    def __init__(self):
        self.connected = False
        self.in_queue = False
        self.farm_joined = False
        self.gold = 0
        self.xp = 0
        self.level = 1
        self.stars = 0
        self.tiles = {}
        self.inventory = {}
        self.orders = []
        self.farm_jobs = []
        self.last_action = 0
        self.cycle_count = 0
        self.total_planted = 0
        self.total_harvested = 0
        self.total_orders = 0
        self.total_gold_earned = 0
        self.start_time = time.time()
        self.room_id = None
        self.player_id = None

    def summary(self):
        elapsed = time.time() - self.start_time
        h, m = divmod(int(elapsed), 3600)
        m, s = divmod(m, 60)
        return (
            f"Gold={self.gold:,} | XP={self.xp:,} | Lv{self.level} | "
            f"Stars={self.stars} | Cycles={self.cycle_count} | "
            f"Planted={self.total_planted} | Harvested={self.total_harvested} | "
            f"Orders={self.total_orders} | Uptime={h}h{m}m"
        )

    def available_tiles(self):
        return {k: v for k, v in self.tiles.items()
                if v.get("groundState") == "tilled" or
                   (v.get("groundState") == "none" and not v.get("cropId"))}

    def planted_tiles(self):
        return {k: v for k, v in self.tiles.items()
                if v.get("groundState") == "planted" and v.get("cropId")}

    def ready_tiles(self):
        now_ms = int(time.time() * 1000)
        return {k: v for k, v in self.planted_tiles().items()
                if v.get("readyAt") and v["readyAt"] <= now_ms}


# ─────────────────────────────────────────────
# FASTAPI DASHBOARD
# ─────────────────────────────────────────────
app = FastAPI(title="FarmTown Dashboard")


@app.get("/", response_class=HTMLResponse)
async def index():
    return DASHBOARD_HTML


@app.get("/api/state")
async def get_state():
    return JSONResponse(SHARED.to_dict())


@app.get("/api/events")
async def sse_events(request: Request):
    q = asyncio.Queue()
    SHARED.subscribers.append(q)

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield {"event": "action", "data": json.dumps(event)}
                except asyncio.TimeoutError:
                    # Send heartbeat
                    yield {"event": "heartbeat", "data": json.dumps({"ts": time.time()})}
        finally:
            if q in SHARED.subscribers:
                SHARED.subscribers.remove(q)

    return EventSourceResponse(stream())


# ─────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FarmTown Bot Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--surface:#111827;--surface2:#1a2332;--border:#1e293b;--text:#e2e8f0;--dim:#64748b;--green:#22c55e;--blue:#3b82f6;--yellow:#eab308;--orange:#f97316;--red:#ef4444;--purple:#a855f7;--cyan:#06b6d4}
body{font-family:'SF Mono','Cascadia Code','Consolas',monospace;background:var(--bg);color:var(--text);min-height:100vh}
.header{background:linear-gradient(135deg,#0f172a,#1e293b);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:16px}
.header h1{font-size:20px;font-weight:700;background:linear-gradient(90deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header .status{margin-left:auto;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.dot.off{background:var(--red);box-shadow:0 0 8px var(--red)}
.dot.queue{background:var(--yellow);box-shadow:0 0 8px var(--yellow);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

.container{max-width:1400px;margin:0 auto;padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.container{grid-template-columns:1fr}}

.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;overflow:hidden}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:12px;display:flex;align-items:center;gap:8px}
.card h2 .icon{font-size:16px}

/* Stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{background:var(--surface2);border-radius:8px;padding:12px;text-align:center}
.stat .label{font-size:10px;text-transform:uppercase;color:var(--dim);letter-spacing:.5px}
.stat .value{font-size:22px;font-weight:700;margin-top:4px}
.stat.gold .value{color:var(--yellow)}
.stat.xp .value{color:var(--cyan)}
.stat.level .value{color:var(--green)}
.stat.stars .value{color:var(--purple)}

/* Farm grid */
.farm-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:4px;max-height:300px;overflow-y:auto}
.farm-tile{aspect-ratio:1;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:14px;position:relative;transition:all .3s}
.farm-tile.empty{background:#1a2332;border:1px dashed #2d3a4d}
.farm-tile.growing{background:#0f3324;border:1px solid #166534}
.farm-tile.ready{background:#422006;border:2px solid var(--yellow);animation:readyPulse 2s infinite}
@keyframes readyPulse{0%,100%{box-shadow:0 0 0 rgba(234,179,8,0)}50%{box-shadow:0 0 12px rgba(234,179,8,.4)}}
.farm-tile .timer{position:absolute;bottom:2px;font-size:8px;color:var(--dim)}

/* Inventory */
.inv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px}
.inv-item{background:var(--surface2);border-radius:8px;padding:8px;text-align:center;font-size:12px}
.inv-item .count{font-size:18px;font-weight:700;color:var(--green);margin-top:4px}

/* Action log */
.log-container{max-height:320px;overflow-y:auto;scroll-behavior:smooth}
.log-entry{display:flex;gap:8px;padding:6px 8px;border-bottom:1px solid var(--border);font-size:12px;animation:fadeIn .3s}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.log-entry .time{color:var(--dim);min-width:60px;flex-shrink:0}
.log-entry .icon{min-width:20px;text-align:center}
.log-entry .msg{color:var(--text)}
.log-entry.harvest .msg{color:var(--green)}
.log-entry.plant .msg{color:var(--cyan)}
.log-entry.buy .msg{color:var(--yellow)}
.log-entry.order .msg{color:var(--orange)}
.log-entry.error .msg{color:var(--red)}
.log-entry.info .msg{color:var(--dim)}

/* Orders */
.order-item{background:var(--surface2);border-radius:8px;padding:10px;margin-bottom:8px;display:flex;align-items:center;gap:10px}
.order-item .emoji{font-size:20px}
.order-item .detail{flex:1;font-size:12px}
.order-item .reward{color:var(--yellow);font-size:12px;font-weight:600}
.order-status{font-size:10px;padding:2px 6px;border-radius:4px;text-transform:uppercase;font-weight:600}
.order-status.ready{background:#422006;color:var(--yellow)}
.order-status.pending{background:#1e293b;color:var(--dim)}

/* Farm summary */
.farm-summary{display:flex;gap:12px;flex-wrap:wrap}
.farm-summary .chip{background:var(--surface2);border-radius:20px;padding:6px 12px;font-size:12px;display:flex;align-items:center;gap:6px}
.farm-summary .chip .num{font-weight:700}
.farm-summary .chip.grow .num{color:var(--green)}
.farm-summary .chip.ready .num{color:var(--yellow)}
.farm-summary .chip.empty .num{color:var(--dim)}

/* Crop reference */
.crop-table{max-height:200px;overflow-y:auto}
.crop-row{display:grid;grid-template-columns:30px 1fr 60px 50px 60px;font-size:11px;padding:4px 8px;border-bottom:1px solid var(--border)}
.crop-row.header{color:var(--dim);text-transform:uppercase;font-size:9px;letter-spacing:.5px;position:sticky;top:0;background:var(--surface)}
.crop-row .name{display:flex;align-items:center;gap:4px}
.crop-row .time{color:var(--dim)}
.crop-row .xp{color:var(--cyan)}
.crop-row .gold{color:var(--yellow)}
.crop-row .level{color:var(--green)}

/* Full width cards */
.full{grid-column:1/-1}
</style>
</head>
<body>

<div class="header">
  <h1>🌾 FarmTown Bot</h1>
  <div class="status">
    <span id="statusDot" class="dot off"></span>
    <span id="statusText" style="font-size:12px;color:var(--dim)">Connecting...</span>
  </div>
</div>

<div class="container">
  <!-- Stats -->
  <div class="card full">
    <h2><span class="icon">📊</span> Stats</h2>
    <div class="stats-grid">
      <div class="stat gold"><div class="label">Gold</div><div class="value" id="gold">0</div></div>
      <div class="stat xp"><div class="label">XP</div><div class="value" id="xp">0</div></div>
      <div class="stat level"><div class="label">Level</div><div class="value" id="level">1</div></div>
      <div class="stat stars"><div class="label">Stars</div><div class="value" id="stars">0</div></div>
    </div>
    <div style="display:flex;gap:16px;margin-top:12px;flex-wrap:wrap">
      <span style="font-size:12px;color:var(--dim)">⏱ Uptime: <span id="uptime" style="color:var(--text)">0h 0m</span></span>
      <span style="font-size:12px;color:var(--dim)">🔄 Cycles: <span id="cycles" style="color:var(--text)">0</span></span>
      <span style="font-size:12px;color:var(--dim)">🌱 Planted: <span id="planted" style="color:var(--green)">0</span></span>
      <span style="font-size:12px;color:var(--dim)">🌾 Harvested: <span id="harvested" style="color:var(--yellow)">0</span></span>
      <span style="font-size:12px;color:var(--dim)">📦 Orders: <span id="ordersDone" style="color:var(--orange)">0</span></span>
      <span style="font-size:12px;color:var(--dim)">💰 Gold Earned: <span id="goldEarned" style="color:var(--yellow)">0</span></span>
    </div>
  </div>

  <!-- Farm Grid -->
  <div class="card full">
    <h2><span class="icon">🗺️</span> Farm Grid</h2>
    <div class="farm-summary">
      <div class="chip grow">🌱 Growing: <span class="num" id="gridGrowing">0</span></div>
      <div class="chip ready">✅ Ready: <span class="num" id="gridReady">0</span></div>
      <div class="chip empty">⬜ Empty: <span class="num" id="gridEmpty">0</span></div>
    </div>
    <div class="farm-grid" id="farmGrid" style="margin-top:12px"></div>
  </div>

  <!-- Action Log -->
  <div class="card">
    <h2><span class="icon">📋</span> Action Log</h2>
    <div class="log-container" id="logContainer"></div>
  </div>

  <!-- Inventory + Orders -->
  <div class="card">
    <h2><span class="icon">🎒</span> Inventory</h2>
    <div class="inv-grid" id="inventory"></div>
    <h2 style="margin-top:16px"><span class="icon">📦</span> Orders</h2>
    <div id="orderList"></div>
  </div>

  <!-- Crop Reference -->
  <div class="card full">
    <h2><span class="icon">📚</span> Crop Reference</h2>
    <div class="crop-table">
      <div class="crop-row header">
        <div></div><div>Crop</div><div>Time</div><div>XP</div><div>Gold</div><div>Lv</div>
      </div>
      <div id="cropTable"></div>
    </div>
  </div>
</div>

<script>
// State
let lastData = null;
const CROPS_JSON = %%CROPS_JSON%%;

function formatNum(n) {
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return n.toLocaleString();
}

function formatSec(s) {
  if (s >= 3600) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  if (s >= 60) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return s + 's';
}

function renderStats(s) {
  document.getElementById('gold').textContent = formatNum(s.stats.gold);
  document.getElementById('xp').textContent = formatNum(s.stats.xp);
  document.getElementById('level').textContent = s.stats.level;
  document.getElementById('stars').textContent = s.stats.stars;
  document.getElementById('uptime').textContent = s.stats.uptime;
  document.getElementById('cycles').textContent = formatNum(s.stats.cycles);
  document.getElementById('planted').textContent = s.stats.planted;
  document.getElementById('harvested').textContent = s.stats.harvested;
  document.getElementById('ordersDone').textContent = s.stats.orders;
  document.getElementById('goldEarned').textContent = formatNum(s.stats.gold_earned);
}

function renderStatus(s) {
  const dot = document.getElementById('statusDot');
  const txt = document.getElementById('statusText');
  if (s.status.connected && !s.status.in_queue) {
    dot.className = 'dot on';
    txt.textContent = `Connected | Room: ${s.status.room_id || '?'} | Crop: ${s.status.crop_strategy} | Active: ${s.status.active_crop || 'none'}`;
  } else if (s.status.in_queue) {
    dot.className = 'dot queue';
    txt.textContent = 'In Queue — waiting for server...';
  } else {
    dot.className = 'dot off';
    txt.textContent = 'Disconnected';
  }
}

function renderGrid(s) {
  const grid = document.getElementById('farmGrid');
  document.getElementById('gridGrowing').textContent = s.farm_grid.growing;
  document.getElementById('gridReady').textContent = s.farm_grid.ready;
  document.getElementById('gridEmpty').textContent = s.farm_grid.empty;

  // Merge all tiles into a display grid
  const allTiles = {};
  for (const [k, v] of Object.entries(s.farm_grid.growing_tiles)) {
    allTiles[k] = {crop: v.crop, state: 'growing', remaining: v.remaining_sec};
  }
  for (const [k, v] of Object.entries(s.farm_grid.ready_tiles)) {
    allTiles[k] = {crop: v.crop, state: 'ready'};
  }

  // Calculate grid size
  let maxX = 8, maxY = 6;
  for (const k of Object.keys(allTiles)) {
    const [x, y] = k.split(',').map(Number);
    maxX = Math.max(maxX, x + 1);
    maxY = Math.max(maxY, y + 1);
  }
  maxX = Math.min(maxX, 12);
  maxY = Math.min(maxY, 10);

  let html = '';
  for (let y = 0; y < maxY; y++) {
    for (let x = 0; x < maxX; x++) {
      const key = `${x},${y}`;
      const t = allTiles[key];
      if (t) {
        const emoji = CROPS_JSON[t.crop]?.emoji || '🌱';
        const timer = t.remaining ? `<div class="timer">${formatSec(t.remaining)}</div>` : '';
        html += `<div class="farm-tile ${t.state}">${emoji}${timer}</div>`;
      } else {
        html += `<div class="farm-tile empty"></div>`;
      }
    }
  }
  grid.innerHTML = html;
}

function renderInventory(s) {
  const el = document.getElementById('inventory');
  if (Object.keys(s.inventory).length === 0) {
    el.innerHTML = '<div style="color:var(--dim);font-size:12px">No inventory data yet</div>';
    return;
  }
  el.innerHTML = Object.entries(s.inventory).map(([name, count]) =>
    `<div class="inv-item"><div>${name}</div><div class="count">${count}</div></div>`
  ).join('');
}

function renderOrders(s) {
  const el = document.getElementById('orderList');
  if (!s.orders || s.orders.length === 0) {
    el.innerHTML = '<div style="color:var(--dim);font-size:12px">No orders</div>';
    return;
  }
  el.innerHTML = s.orders.map(o => {
    const emoji = CROPS_JSON[o.cropId?.replace('_seed','')]?.emoji || '📦';
    const isReady = o.ready || o.status === 'ready';
    return `<div class="order-item">
      <div class="emoji">${emoji}</div>
      <div class="detail">${o.cropId || o.name || 'Order'} x${o.quantity || 1}</div>
      <span class="order-status ${isReady ? 'ready' : 'pending'}">${isReady ? 'READY' : 'PENDING'}</span>
      <div class="reward">${o.gold || 0}g</div>
    </div>`;
  }).join('');
}

function renderLog(s) {
  const el = document.getElementById('logContainer');
  const entries = s.log || [];
  const icons = {harvest: '🌾', plant: '🌱', buy: '🏪', order: '📦', error: '❌', info: 'ℹ️', pool: '🏊', connect: '🔌', queue: '⏳'};
  el.innerHTML = entries.slice(-50).reverse().map(e =>
    `<div class="log-entry ${e.type}">
      <span class="time">${e.time}</span>
      <span class="icon">${icons[e.type] || '•'}</span>
      <span class="msg">${e.message}</span>
    </div>`
  ).join('');
  el.scrollTop = 0;
}

function renderCrops() {
  const el = document.getElementById('cropTable');
  el.innerHTML = Object.entries(CROPS_JSON).map(([name, c]) =>
    `<div class="crop-row">
      <div>${c.emoji}</div>
      <div class="name">${name}</div>
      <div class="time">${formatSec(c.grow_sec)}</div>
      <div class="xp">${c.xp}</div>
      <div class="gold">${formatNum(c.reward_gold)}</div>
      <div class="level">Lv${c.unlock}</div>
    </div>`
  ).join('');
}

function renderAll(data) {
  lastData = data;
  renderStats(data);
  renderStatus(data);
  renderGrid(data);
  renderInventory(data);
  renderOrders(data);
  renderLog(data);
}

// Polling
async function poll() {
  try {
    const r = await fetch('/api/state');
    const data = await r.json();
    renderAll(data);
  } catch(e) {}
  setTimeout(poll, 2000);
}

// SSE for live action log
function connectSSE() {
  const es = new EventSource('/api/events');
  es.addEventListener('action', (e) => {
    const event = JSON.parse(e.data);
    if (lastData) {
      lastData.log.push(event);
      renderLog(lastData);
    }
  });
  es.onerror = () => {
    setTimeout(connectSSE, 5000);
  };
}

renderCrops();
poll();
connectSSE();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
# BOT ENGINE
# ─────────────────────────────────────────────
class FarmBot:
    def __init__(self, crop_name: str = "auto", headless: bool = False):
        self.crop = crop_name
        self.headless = headless
        self.state = GameState()
        self.page: Page = None
        self.context: BrowserContext = None
        self._last_pool_claim = 0
        self._running = True

        SHARED.crop_strategy = crop_name

    async def start(self):
        """Main entry point."""
        log("Starting FarmTown Bot + Dashboard...")

        # Start dashboard server in background thread
        self._start_dashboard()

        async with async_playwright() as pw:
            self.context = await pw.chromium.launch_persistent_context(
                CONFIG["user_data_dir"],
                headless=self.headless,
                viewport=CONFIG["viewport"],
                locale="en-US",
                timezone_id="America/New_York",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )

            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

            await self._setup_ws_interceptor()
            await self.page.goto(CONFIG["farm_url"], wait_until="domcontentloaded")
            log(f"Dashboard: http://localhost:{CONFIG['dashboard_port']}")
            log("Waiting for auth (solve CAPTCHA + connect Phantom)...")

            SHARED.emit_event("info", "Bot started. Waiting for auth...")
            await self._wait_for_auth()
            await self._farm_loop()

    def _start_dashboard(self):
        """Start FastAPI dashboard in background thread."""
        def run():
            uvicorn.run(app, host="0.0.0.0", port=CONFIG["dashboard_port"],
                       log_level="warning", access_log=False)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        log(f"Dashboard running on port {CONFIG['dashboard_port']}")

    async def _setup_ws_interceptor(self):
        await self.page.add_init_script("""
            window.__farmBot = {
                events: [],
                socket: null,
                log(event, data) {
                    this.events.push({t: Date.now(), e: event, d: data});
                    if (this.events.length > 500) this.events.shift();
                },
                popEvents() {
                    const e = [...this.events];
                    this.events = [];
                    return e;
                },
                emit(event, data) {
                    if (this.socket) {
                        this.socket.emit(event, data);
                        return true;
                    }
                    return false;
                },
                setSocket(sock) {
                    this.socket = sock;
                    if (sock && !this._patched) {
                        this._patched = true;
                        const origEmit = sock.emit.bind(sock);
                        const bot = this;
                        sock.emit = (...args) => {
                            bot.log('emit', {event: args[0], data: args[1]});
                            return origEmit(...args);
                        };
                        const origOn = sock.on.bind(sock);
                        sock.on = function(event, handler) {
                            return origOn(event, function(...handlerArgs) {
                                bot.log('on', {event: event, data: handlerArgs[0]});
                                return handler(...handlerArgs);
                            });
                        };
                    }
                }
            };
            const origIO = window.io;
            if (origIO) {
                window.io = function(...args) {
                    const socket = origIO(...args);
                    window.__farmBot.setSocket(socket);
                    console.log('[FarmBot] Socket.IO intercepted');
                    return socket;
                };
            }
            const checkInterval = setInterval(() => {
                if (window.__farmBot.socket) { clearInterval(checkInterval); return; }
                const keys = Object.keys(window);
                for (const k of keys) {
                    try {
                        const v = window[k];
                        if (v && typeof v.emit === 'function' && typeof v.on === 'function' && v.connected) {
                            window.__farmBot.setSocket(v);
                            clearInterval(checkInterval);
                            return;
                        }
                    } catch(e) {}
                }
            }, 2000);
        """)

    async def _wait_for_auth(self):
        start = time.time()
        while time.time() - start < 600:
            try:
                has_farm = await self.page.evaluate("""
                    () => {
                        const bot = window.__farmBot;
                        if (bot && bot.events.some(e => e.e === 'on' && e.d?.event === 'farm:snapshot')) return true;
                        const hotbar = document.querySelector('.hotbar-dock, .farm-menu-panel, .app-shell');
                        return !!hotbar;
                    }
                """)
                if has_farm:
                    log("Farm loaded! Bot active.")
                    SHARED.emit_event("connect", "Farm loaded — bot active")
                    SHARED.update(farm_joined=True, connected=True)
                    self.state.farm_joined = True
                    return
            except Exception:
                pass
            await asyncio.sleep(3)
        log("ERROR: Timed out waiting for farm.")
        SHARED.emit_event("error", "Timed out waiting for farm")

    async def _farm_loop(self):
        log("═══════════════════════════════════════")
        log("  FarmTown Bot + Dashboard Active")
        log(f"  Dashboard: http://localhost:{CONFIG['dashboard_port']}")
        log("  Press Ctrl+C to stop")
        log("═══════════════════════════════════════")

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(CONFIG["action_cooldown"])
            except KeyboardInterrupt:
                log("Stopping...")
                self._running = False
            except Exception as e:
                log(f"Error: {e}")
                await asyncio.sleep(5)

        log(f"Bot stopped. {self.state.summary()}")

    async def _tick(self):
        events = await self._pop_events()
        self._process_events(events)
        self._sync_shared()

        if time.time() - self.state.last_action < CONFIG["action_cooldown"]:
            return

        self.state.cycle_count += 1
        crop_info = self._select_crop()

        # Harvest
        ready = self.state.ready_tiles()
        if ready:
            for tile_key in list(ready.keys())[:10]:
                x, y = tile_key.split(",")
                await self._do_harvest(int(x), int(y))
                await asyncio.sleep(random_delay())

        # Plant
        available = self.state.available_tiles()
        if available and crop_info:
            seed_count = self.state.inventory.get(f"{crop_info['id']}_seed", 0)
            if seed_count < CONFIG["min_seeds"]:
                await self._do_buy_seeds(crop_info["id"], 10)
                await asyncio.sleep(random_delay())
            for tile_key in list(available.keys())[:10]:
                x, y = tile_key.split(",")
                await self._do_plant(int(x), int(y), f"{crop_info['id']}_seed")
                await asyncio.sleep(random_delay())

        # Orders
        for order in self.state.orders:
            if order.get("ready") or order.get("status") == "ready":
                await self._do_complete_order(order.get("id", order.get("orderId")))
                await asyncio.sleep(random_delay())

        # Pool claim
        if time.time() - self._last_pool_claim > CONFIG["pool_claim_interval"]:
            await self._do_claim_pool()
            self._last_pool_claim = time.time()

        if self.state.cycle_count % 10 == 0:
            log(f"[{self.state.cycle_count}] {self.state.summary()}")

    def _sync_shared(self):
        """Push bot state to shared state for dashboard."""
        SHARED.update(
            gold=self.state.gold,
            xp=self.state.xp,
            level=self.state.level,
            stars=self.state.stars,
            tiles=dict(self.state.tiles),
            inventory=dict(self.state.inventory),
            orders=list(self.state.orders),
            connected=self.state.connected,
            in_queue=self.state.in_queue,
            farm_joined=self.state.farm_joined,
            room_id=self.state.room_id,
            total_planted=self.state.total_planted,
            total_harvested=self.state.total_harvested,
            total_orders=self.state.total_orders,
            total_gold_earned=self.state.total_gold_earned,
            cycle_count=self.state.cycle_count,
            active_crop=self._select_crop()["id"] if self._select_crop() else None,
        )

    def _select_crop(self):
        if self.crop == "auto":
            candidates = [(n, i) for n, i in CROPS.items() if i["unlock"] <= self.state.level]
            if not candidates:
                candidates = [("potato", CROPS["potato"])]
            candidates.sort(key=lambda x: x[1]["xp"] / max(x[1]["grow_sec"], 1), reverse=True)
            for name, info in candidates:
                if self.state.gold >= info["cost"]:
                    return {"id": name, **info}
            return {"id": "potato", **CROPS["potato"]}
        else:
            cid = self.crop.lower()
            if cid in CROPS:
                return {"id": cid, **CROPS[cid]}
            return {"id": "potato", **CROPS["potato"]}

    def _process_events(self, events):
        for evt in events:
            d = evt.get("d", {})
            ev = d.get("event", "") if isinstance(d, dict) else ""
            if evt.get("e") == "on":
                payload = d.get("data", {}) if isinstance(d, dict) else {}
                if ev == "farm:snapshot" or ev == "farm:state/sync":
                    self._apply_snapshot(payload)
                elif ev == "tile:update":
                    self._apply_tile_update(payload)
                elif ev == "player:farmState/sync":
                    self._apply_player_state(payload)
                elif ev == "roomJoined":
                    self.state.connected = True
                    self.state.room_id = payload.get("roomId")
                    SHARED.emit_event("connect", f"Room joined: {self.state.room_id}")
                elif ev == "game:actionResult":
                    self._handle_action_result(payload)
                elif ev == "queue:update":
                    self.state.in_queue = True
                    SHARED.emit_event("queue", "In queue...")
                elif ev == "queue:ready":
                    self.state.in_queue = False
                    SHARED.emit_event("queue", "Queue ready! Joining...")

    def _apply_snapshot(self, data):
        if not isinstance(data, dict):
            return
        player = data.get("player", data.get("playerState", {}))
        if player:
            self.state.gold = player.get("gold", self.state.gold)
            self.state.xp = player.get("xp", self.state.xp)
            self.state.level = player.get("level", self.state.level)
            self.state.stars = player.get("premiumBalance", {}).get("stars", self.state.stars)
            self.state.player_id = player.get("id", self.state.player_id)
            inv = player.get("inventory", {})
            if inv:
                self.state.inventory = inv
            orders = player.get("orders", [])
            if orders:
                self.state.orders = orders
        tiles = data.get("tiles", [])
        if isinstance(tiles, list):
            for tile in tiles:
                key = f"{tile.get('x', tile.get('tileX', 0))},{tile.get('y', tile.get('tileY', 0))}"
                self.state.tiles[key] = tile
        elif isinstance(tiles, dict):
            self.state.tiles.update(tiles)

    def _apply_tile_update(self, data):
        if not isinstance(data, dict):
            return
        x = data.get("x", data.get("tileX", 0))
        y = data.get("y", data.get("tileY", 0))
        key = f"{x},{y}"
        if key in self.state.tiles:
            self.state.tiles[key].update(data)
        else:
            self.state.tiles[key] = data

    def _apply_player_state(self, data):
        if not isinstance(data, dict):
            return
        self.state.gold = data.get("gold", self.state.gold)
        self.state.xp = data.get("xp", self.state.xp)
        self.state.level = data.get("level", self.state.level)
        self.state.stars = data.get("premiumBalance", {}).get("stars", self.state.stars)
        inv = data.get("inventory", {})
        if inv:
            self.state.inventory = inv
        if data.get("orders"):
            self.state.orders = data["orders"]

    def _handle_action_result(self, data):
        if not isinstance(data, dict):
            return
        action = data.get("actionType", data.get("action", ""))
        status = data.get("status", "")
        if status in ("ok", "success"):
            if action == "plant":
                self.state.total_planted += 1
            elif action == "harvest":
                self.state.total_harvested += 1
        elif status in ("error", "failed", "denied"):
            SHARED.emit_event("error", f"Action failed: {action}")

    async def _pop_events(self):
        try:
            return await self.page.evaluate("() => window.__farmBot ? window.__farmBot.popEvents() : []")
        except Exception:
            return []

    async def _emit(self, event: str, data: dict):
        try:
            return await self.page.evaluate(
                f"(data) => window.__farmBot ? window.__farmBot.emit('{event}', data) : false", data)
        except Exception:
            return False

    async def _do_harvest(self, x, y):
        log(f"🌾 Harvesting ({x},{y})")
        SHARED.emit_event("harvest", f"Harvested ({x},{y})")
        self.state.last_action = time.time()
        return await self._emit("game:action", {
            "roomId": self.state.room_id, "action": "harvest",
            "actionId": f"harv:{x},{y}:{int(time.time()*1000)}",
            "tileX": x, "tileY": y,
            "clientSentAt": int(time.time() * 1000),
            "clientDebug": {"interactionMode": "farm", "networkMode": "socket"}
        })

    async def _do_plant(self, x, y, seed_id):
        crop_name = seed_id.replace("_seed", "")
        emoji = CROPS.get(crop_name, {}).get("emoji", "🌱")
        log(f"🌱 Planting {crop_name} ({x},{y})")
        SHARED.emit_event("plant", f"{emoji} Planted {crop_name} ({x},{y})")
        self.state.last_action = time.time()
        return await self._emit("game:action", {
            "roomId": self.state.room_id, "action": "plant",
            "actionId": f"pl:{x},{y}:{int(time.time()*1000)}",
            "tileX": x, "tileY": y, "seedId": seed_id,
            "clientSentAt": int(time.time() * 1000),
            "clientDebug": {"interactionMode": "farm", "networkMode": "socket"}
        })

    async def _do_buy_seeds(self, crop_id, qty=10):
        emoji = CROPS.get(crop_id, {}).get("emoji", "🏪")
        log(f"🏪 Buying {qty}x {crop_id} seeds")
        SHARED.emit_event("buy", f"{emoji} Bought {qty}x {crop_id} seeds")
        self.state.last_action = time.time()
        return await self._emit("store:buySeed/request", {
            "roomId": self.state.room_id, "seedId": f"{crop_id}_seed",
            "quantity": qty, "actionId": f"buy:{crop_id}:{int(time.time()*1000)}",
            "clientSentAt": int(time.time() * 1000),
        })

    async def _do_complete_order(self, order_id):
        if not order_id:
            return False
        log(f"📦 Completing order {order_id}")
        SHARED.emit_event("order", f"Completed order {order_id}")
        self.state.total_orders += 1
        self.state.last_action = time.time()
        return await self._emit("order:complete/request", {
            "roomId": self.state.room_id, "orderId": order_id,
        })

    async def _do_claim_pool(self):
        log("🏊 Claiming Farmer's Pool...")
        SHARED.emit_event("pool", "Claimed Farmer's Pool")
        self.state.last_action = time.time()
        try:
            result = await self.page.evaluate("""
                async () => {
                    try {
                        const token = localStorage.getItem('farmtown_auth_token') ||
                                      document.cookie.match(/auth_token=([^;]+)/)?.[1];
                        if (!token) return {ok: false, message: 'No auth token'};
                        const r = await fetch('/api/rewards/farmer-pool/status', {
                            headers: {Authorization: `Bearer ${token}`}
                        });
                        return await r.json();
                    } catch(e) { return {ok: false, message: e.message}; }
                }
            """)
            log(f"Pool: {json.dumps(result)[:200]}")
        except Exception as e:
            log(f"Pool error: {e}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="FarmTown Bot + Dashboard")
    parser.add_argument("--crop", default="auto", help="Crop to farm")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--port", type=int, default=3001, help="Dashboard port")
    args = parser.parse_args()

    CONFIG["dashboard_port"] = args.port

    # Inject crops into HTML for frontend
    crops_for_js = {}
    for k, v in CROPS.items():
        crops_for_js[k] = {kk: vv for kk, vv in v.items()}
    global DASHBOARD_HTML
    DASHBOARD_HTML = DASHBOARD_HTML.replace("%%CROPS_JSON%%", json.dumps(crops_for_js))

    bot = FarmBot(crop_name=args.crop, headless=args.headless)
    asyncio.run(bot.start())


if __name__ == "__main__":
    main()
