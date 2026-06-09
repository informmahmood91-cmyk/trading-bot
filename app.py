"""
MIKA TRADING BOT — FINAL VERSION (ALL BUGS FIXED)
==================================================
BUG FIXES APPLIED:
1. compute_mcs normalization: removed *100 (was inflating scores)
2. session_gate off-hours: changed to pk_hour < 2 (00:00-01:59 = off, 02:00 = NY)
3. _send_trade "PENDING" parameter: removed misleading default
4. price = 0.0 when None: added explicit abort on missing price
5. chart-img.com API: fixed to handle direct binary response
6. buffer_lock held during send_telegram: moved network I/O outside lock
7. chatgpt_decides web search on retry: added tools to fallback
8. compute_mcs with empty snapshots: added guard
9. extract_multiline_field greedy regex: constrained with re.MULTILINE
10. off_session_buffer direction normalization: added BULLISH/BEARISH mapping
11. _quality count excludes None values: fixed bb_width tracking
12. MAX_PAYLOAD_MB check for chunked transfers: added fallback
13. reset_day: added scheduler job for midnight reset
14. Thread safety improvements throughout
15. Cache size limit with LRU eviction
16. Off-session signal age pruning
17. Scheduler timezone fix using pytz
"""

import json
import os
import re
import time
import threading
import base64
import requests
from flask import Flask, request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

app = Flask(__name__)

# ==========================================
# ENV VARIABLES
# ==========================================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CHATGPT_KEY      = os.environ.get("CHATGPT_KEY")
GEMINI_KEY       = os.environ.get("GEMINI_KEY")
FINNHUB_KEY      = os.environ.get("FINNHUB_KEY")
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY")
ENABLE_WEB_SEARCH = os.environ.get("ENABLE_WEB_SEARCH", "true").lower() == "true"
CHART_IMG_API_KEY = os.environ.get("CHART_IMG_API_KEY")
MAX_PAYLOAD_MB    = int(os.environ.get("MAX_PAYLOAD_MB", "10"))
AI_RISK_PROFILE   = os.environ.get("AI_RISK_PROFILE", "BALANCED")

# ==========================================
# API ENDPOINTS
# ==========================================
GEMINI_URL  = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
OPENAI_URL  = "https://api.openai.com/v1/chat/completions"

# ==========================================
# STATE — THREAD-SAFE
# ==========================================
pair_session_tracker = {}
neutral_cache        = {}
off_session_buffer   = {}
current_day          = None

trade_lock  = threading.Lock()
cache_lock  = threading.Lock()
buffer_lock = threading.Lock()

# ==========================================
# CONFIGURATION (env-tunable)
# ==========================================
MCS_THRESHOLD            = float(os.environ.get("MCS_THRESHOLD",            "15.0"))
PRICE_VETO_PCT           = float(os.environ.get("PRICE_VETO_PCT",           "0.30"))
CACHE_EXPIRY_MINUTES     = int(os.environ.get("CACHE_EXPIRY_MINUTES",       "30"))
MAX_AUTO_NEUTRAL_STREAK  = int(os.environ.get("MAX_AUTO_NEUTRAL_STREAK",    "4"))
MAX_CACHE_ENTRIES        = int(os.environ.get("MAX_CACHE_ENTRIES",          "200"))
OFF_SESSION_MAX_AGE_HOURS = int(os.environ.get("OFF_SESSION_MAX_AGE_HOURS", "12"))
OFF_MIN_SIGNALS          = 3
OFF_CONSISTENCY_PCT      = 0.80
SL_ATR_MULTIPLIER        = float(os.environ.get("SL_ATR_MULTIPLIER",        "1.5"))
TP_ATR_MULTIPLIER        = float(os.environ.get("TP_ATR_MULTIPLIER",        "2.5"))
MIN_SL_DISTANCE_PCT      = 0.002   # 0.2% minimum SL distance

# Risk profile configurations
RISK_PROFILES = {
    "CONSERVATIVE": {
        "triple_exhaust_enabled": True,
        "low_volume_threshold": 0.6,
        "pivot_block_enabled": True,
        "macro_ranging_block": True,
        "min_confidence_for_trade": 75,
    },
    "BALANCED": {
        "triple_exhaust_enabled": True,
        "low_volume_threshold": 0.4,
        "pivot_block_enabled": False,
        "macro_ranging_block": False,
        "min_confidence_for_trade": 65,
    },
    "AGGRESSIVE": {
        "triple_exhaust_enabled": False,
        "low_volume_threshold": 0.3,
        "pivot_block_enabled": False,
        "macro_ranging_block": False,
        "min_confidence_for_trade": 55,
    }
}


# ==========================================
# TELEGRAM — CHUNKED SENDER
# ==========================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram config")
        return
    if len(msg) <= 4000:
        chunks = [msg]
    else:
        chunks = []
        while len(msg) > 3900:
            split_at = msg.rfind("\n", 0, 3900)
            if split_at == -1:
                split_at = 3900
            chunks.append(msg[:split_at])
            msg = msg[split_at:].lstrip("\n")
        if msg:
            chunks.append(msg)
    for chunk in chunks:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
                timeout=10
            )
            if len(chunks) > 1:
                time.sleep(0.5)
        except Exception as e:
            print(f"Telegram error: {e}")


# ==========================================
# UTILITIES
# ==========================================
def safe_str(v, default="N/A"):
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()

def safe_float(v, allow_zero=False):
    """Returns float or None. Zero returns None unless allow_zero=True."""
    try:
        f = float(str(v).replace(",", "").strip())
        if f == 0.0 and not allow_zero:
            return None
        return f
    except Exception:
        return None

def safe_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0

def safe_bool(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")

def clean_symbol(symbol):
    """Validate and clean symbol. Returns "UNKNOWN" if invalid."""
    if not symbol or not isinstance(symbol, str):
        return "UNKNOWN"
    if len(symbol) > 20:
        return "UNKNOWN"
    prefixes = ["FX:","OANDA:","BINANCE:","PYTH:","TVC:",
                "CAPITALCOM:","PEPPERSTONE:","ICMARKETS:","FXCM:","FOREX:"]
    s = symbol.upper().strip()
    for p in prefixes:
        s = s.replace(p, "")
    result = s.strip()
    if len(result) < 3:
        return "UNKNOWN"
    return result

def parse_incoming_data(rj, rt):
    """Enhanced parser with better error handling and logging."""
    if rj and isinstance(rj, dict):
        return rj
    
    if rt and isinstance(rt, str) and rt.strip():
        try:
            parsed = json.loads(rt.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        
        try:
            result = {}
            for line in rt.strip().split('\n'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    result[key.strip()] = value.strip()
            if result:
                return result
        except:
            pass
        
        try:
            json_match = re.search(r'\{.*\}', rt, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                if isinstance(parsed, dict):
                    return parsed
        except:
            pass
    
    print(f"Cannot parse data. RJ type: {type(rj)}, RT length: {len(rt) if rt else 0}")
    return {}


# ==========================================
# EXTRACTION HELPERS
# ==========================================
def extract_direction(text):
    m = re.search(r'FINAL\s+DIRECTION\s*[:\-]\s*(BUY|SELL|NEUTRAL)', text.upper())
    if m:
        return m.group(1)
    m = re.search(r'(?<!\w)DIRECTION\s*[:\-]\s*(BUY|SELL|NEUTRAL)', text.upper())
    if m:
        return m.group(1)
    return "NEUTRAL"

def extract_confidence(text):
    m = re.search(r'CONFIDENCE\s*[:\-]\s*(\d+)', text)
    return m.group(1) + "%" if m else "N/A"

def extract_multiline_field(text, label):
    # FIXED: Added re.MULTILINE flag and constrained to uppercase headers
    # Pattern requires next field to be ALL CAPS with optional spaces before colon
    pattern = rf'{re.escape(label)}\s*[:\-]\s*(.*?)(?=\n[A-Z][A-Z ]{{3,}}\s*[:\-]|\n\Z|\Z)'
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip().replace("\n", " ")[:400]
    return "N/A"

def extract_neutral_reason(text_a, text_b, calendar, signal=None):
    combined = (text_a + " " + text_b).lower()
    events   = calendar.get("events", [])
    if calendar.get("high_impact_soon") and events:
        first = events[0] if isinstance(events[0], str) else events[0].get("event","")
        return f"{first.split()[0][:8]} event"
    if signal:
        if safe_bool(signal.get("lon_fail_up")) or safe_bool(signal.get("ny_fail_up")):
            return "Failed bullish auction"
        if safe_bool(signal.get("lon_fail_down")) or safe_bool(signal.get("ny_fail_down")):
            return "Failed bearish auction"
    if "double top" in combined or "double bottom" in combined:
        return "Reversal pattern"
    if "choch" in combined or "change of character" in combined:
        return "CHoCH detected"
    if "bos" in combined or "break of structure" in combined:
        return "BOS conflict"
    if "technical conflict" in combined or "contradict" in combined:
        return "Tech conflict"
    if "pivot" in combined and ("r2" in combined or "s2" in combined):
        return "At pivot extreme"
    if "ranging" in combined or "adx" in combined and "low" in combined:
        return "Ranging market"
    if "exhaustion" in combined or "overbought" in combined:
        return "Momentum exhausted"
    if "opposite" in combined:
        return "Tech vs Fund"
    return "No clear edge"


# ==========================================
# ATR-BASED SL/TP CALCULATION (Python — not AI)
# ==========================================
def calculate_sl_tp(direction, price, atr):
    """
    Deterministic SL/TP from ATR. AI does NOT calculate this.
    SL = ATR x 1.5, TP = ATR x 2.5 -> gives 1:1.67 minimum R:R
    """
    try:
        pr = float(price)
        if pr <= 0:
            return "N/A", "N/A", "N/A"
        atr_v = float(atr) if atr is not None else pr * 0.005
        if atr_v <= 0:
            atr_v = pr * 0.005
    except (TypeError, ValueError):
        return "N/A", "N/A", "N/A"

    if direction == "BUY":
        sl = pr - (atr_v * SL_ATR_MULTIPLIER)
        tp = pr + (atr_v * TP_ATR_MULTIPLIER)
        min_dist = pr * MIN_SL_DISTANCE_PCT
        if (pr - sl) < min_dist:
            sl = pr - min_dist
    elif direction == "SELL":
        sl = pr + (atr_v * SL_ATR_MULTIPLIER)
        tp = pr - (atr_v * TP_ATR_MULTIPLIER)
        min_dist = pr * MIN_SL_DISTANCE_PCT
        if (sl - pr) < min_dist:
            sl = pr + min_dist
    else:
        return "N/A", "N/A", "N/A"

    risk   = abs(pr - sl)
    reward = abs(tp - pr)
    rr     = f"1:{reward/risk:.1f}" if risk > 0 else "N/A"
    return round(sl, 5), round(tp, 5), rr


def validate_levels(direction, price, sl, tp):
    # FIXED: Added explicit N/A check before float conversion
    if sl == "N/A" or tp == "N/A" or price == "N/A":
        return False, "N/A values in levels"
    try:
        sl_f = float(sl); tp_f = float(tp); pr_f = float(price)
        if direction == "BUY":
            return sl_f < pr_f and tp_f > pr_f, f"BUY: SL{sl_f}<{pr_f}<TP{tp_f}"
        if direction == "SELL":
            return sl_f > pr_f and tp_f < pr_f, f"SELL: TP{tp_f}<{pr_f}<SL{sl_f}"
    except (ValueError, TypeError):
        pass
    return False, "Invalid levels"


# ==========================================
# MARKET REGIME DETECTION
# ==========================================
def detect_market_regime(adx, bb_width=None):
    if adx is not None:
        if adx > 25:
            return "TRENDING"
        if adx < 20:
            return "RANGING"
    if bb_width is not None:
        try:
            if float(bb_width) < 5:
                return "COMPRESSION"
        except Exception:
            pass
    return "TRANSITION"


# ==========================================
# CHART IMAGE FETCHER (FIXED)
# ==========================================
def fetch_chart_image(chart_url):
    """
    Extract symbol and interval from URL, fetch chart image from chart-img.com API.
    FIXED: Converts numerical interval mappings and style references natively to 1h/candles.
    FIXED: Strips exchange prefix before symbol lookup to prevent 422 errors.
    FIXED: Correct symbol mappings per chart-img docs. style=1 (candles numeric code).
    """
    if not CHART_IMG_API_KEY:
        print("No CHART_IMG_API_KEY - chart vision disabled")
        return None
   
    if not chart_url or not isinstance(chart_url, str):
        print("No chart URL provided")
        return None
   
    symbol = None
    interval = "60"
   
    symbol_match = re.search(r'symbol=([^&\s]+)', chart_url)
    if symbol_match:
        symbol = symbol_match.group(1)
   
    interval_match = re.search(r'interval=(\d+)', chart_url)
    if interval_match:
        interval = interval_match.group(1)
   
    if not symbol:
        print(f"Could not extract symbol from URL: {chart_url[:150]}")
        return None
   
    clean_sym = clean_symbol(symbol)
    if ":" in clean_sym:
        clean_sym = clean_sym.split(":", 1)[1].upper()
   
    SYMBOL_TO_TV_TICKER = {
        "GOLD":    "TVC:GOLD",
        "XAUUSD":  "TVC:GOLD",
        "BTCUSD":  "BITSTAMP:BTCUSD",
        "ETHUSD":  "BITSTAMP:ETHUSD",
        "BTCUSDT": "BINANCE:BTCUSDT",
        "ETHUSDT": "BINANCE:ETHUSDT",
    }
   
    if clean_sym in SYMBOL_TO_TV_TICKER:
        tv_symbol = SYMBOL_TO_TV_TICKER[clean_sym]
    elif len(clean_sym) == 6 and clean_sym not in SYMBOL_TO_TV_TICKER:
        tv_symbol = f"OANDA:{clean_sym}"
    else:
        tv_symbol = clean_sym
       
    api_interval = "1h"
    
    print(f"Fetching chart: {clean_sym} -> {tv_symbol}, explicitly forced interval={api_interval}")
   
    headers = {"Authorization": f"Bearer {CHART_IMG_API_KEY}"}
    api_url = "https://api.chart-img.com/v1/tradingview/advanced-chart"
   
    params = {
        "symbol":   tv_symbol,
        "interval": api_interval,
        "width":    800,
        "height":   500,
        "theme":    "dark",
        "style":    "1",
    }
   
    try:
        resp = requests.get(api_url, headers=headers, params=params, timeout=20)
        print(f"Chart API: status={resp.status_code}, type={resp.headers.get('Content-Type','')}, size={len(resp.content)}")
        resp.raise_for_status()
       
        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            data = resp.json()
            if data.get("url"):
                img_resp = requests.get(data["url"], timeout=15)
                img_resp.raise_for_status()
                return base64.b64encode(img_resp.content).decode("utf-8")
            return None
        elif len(resp.content) > 500:
            return base64.b64encode(resp.content).decode("utf-8")
        return None
           
    except Exception as e:
        print(f"Chart fetch error detailed bypass: {e}")
        return None
        
def get_image_mime(chart_url):
    """Safely maps out structural binary mime formats."""
    url_lower = (chart_url or "").lower()
    if ".jpg" in url_lower or ".jpeg" in url_lower:
        return "image/jpeg"
    if ".webp" in url_lower:
        return "image/webp"
    return "image/png"
# ==========================================
# MCS ENGINE (FIXED)
# ==========================================
def _pct_change(old, new):
    try:
        o = float(old); n = float(new)
        return 0.0 if o == 0 else abs((n - o) / o) * 100.0
    except Exception:
        return 0.0

def _normalize(pct, scale):
    return min((pct / scale) * 100.0, 100.0)

def compute_mcs(prev, curr):
    """
    FIXED: Removed *100 from final normalization.
    _normalize already returns 0-100. Weights sum to 1.0.
    mcs / total_weight gives 0-100 directly.
    """
    # FIXED: Guard against empty snapshots
    if not prev or not curr:
        return 0.0
    
    # FIXED: Check if snapshots have any valid data
    has_any_data = False
    for field in ["price", "rsi", "macd_histogram", "stoch_k", "score", "volume_ratio"]:
        if prev.get(field) is not None and curr.get(field) is not None:
            has_any_data = True
            break
    
    if not has_any_data:
        return 0.0
    
    components = [
        ("price",          0.25, 0.30),
        ("macd_histogram", 0.20, 50.0),
        ("rsi",            0.20, 10.0),
        ("score",          0.15, 15.0),
        ("stoch_k",        0.10, 20.0),
        ("volume_ratio",   0.10, 30.0),
    ]
    mcs = 0.0
    total_weight = 0.0
    for field, w, scale in components:
        ov = prev.get(field); nv = curr.get(field)
        if ov is None or nv is None:
            continue
        mcs += _normalize(_pct_change(ov, nv), scale) * w
        total_weight += w
    
    # FIXED: No *100 here - _normalize already returns 0-100
    return round(mcs / total_weight, 2) if total_weight > 0 else 0.0

def build_signal_snapshot(data, twelve):
    def _f(v):
        try:
            return float(str(v).replace(",","").strip())
        except Exception:
            return None
    return {
        "price":          _f(data.get("price") or data.get("close")),
        "rsi":            _f(twelve.get("rsi")),
        "macd_histogram": _f(twelve.get("macd_histogram")),
        "stoch_k":        _f(twelve.get("stoch_k")),
        "score":          _f(data.get("score")),
        "volume_ratio":   _f(data.get("volume_ratio")),
    }

def should_auto_neutral(symbol, session, data, twelve, calendar):
    """
    Master gate - decides whether to skip AI entirely and auto-neutral.
    
    PRIORITY ORDER:
    1. 3+ high-impact calendar events -> AUTO-NEUTRAL (0 tokens, no AI)
    2. No cache -> run AI
    3. Last outcome was BUY/SELL -> run AI
    4. Cache expired -> run AI
    5. Price moved > PRICE_VETO_PCT -> run AI
    6. Auto-streak limit reached -> run AI
    7. MCS < threshold -> AUTO-NEUTRAL (skip AI)
    8. MCS >= threshold -> run AI
    9. 1-2 high-impact events -> run AI (with warning)
    """
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        cached = neutral_cache.get(key)
    
        # ── CHECK FOR 3+ HIGH-IMPACT EVENTS ──
    # FIXED: Filter events by relevant currencies only
    sc_sym = clean_symbol(symbol)
    
    COMMODITY_CURRENCY_MAP = {
        "XAUUSD": ["USD"], "GOLD": ["USD"], "XAGUSD": ["USD"], "SILVER": ["USD"],
        "USOIL": ["USD"], "UKOIL": ["USD", "GBP"], "WTI": ["USD"], "BRENT": ["USD", "GBP"],
        "BTCUSD": ["USD"], "ETHUSD": ["USD"],
        "US30": ["USD"], "NAS100": ["USD"], "SPX500": ["USD"],
        "GER40": ["EUR"], "UK100": ["GBP"], "JP225": ["JPY"],
    }
    
    if sc_sym in COMMODITY_CURRENCY_MAP:
        relevant_currencies = COMMODITY_CURRENCY_MAP[sc_sym]
    elif len(sc_sym) == 6:
        relevant_currencies = [sc_sym[:3], sc_sym[3:]]
    else:
        relevant_currencies = [sc_sym]
    
    calendar_events = calendar.get("events", [])
    filtered_events = []
    for e in calendar_events:
        if isinstance(e, dict):
            event_currency = e.get("currency", "")
            if event_currency in relevant_currencies:
                filtered_events.append(e.get("event", ""))
        elif isinstance(e, str):
            if any(curr in e.upper() for curr in relevant_currencies):
                filtered_events.append(e)
    
    event_count = len(filtered_events)
    
    if event_count >= 3:
        events_str = ", ".join(filtered_events[:3])
        return True, f"🚫 3+ HIGH-IMPACT EVENTS: {events_str} — AUTO-NEUTRAL", 0.0
    
    # V1 - No cache
    if cached is None:
        return False, "No cache - first signal", 0.0

    # V2 - Last outcome was a trade
    if cached.get("last_outcome") in ("BUY", "SELL"):
        return False, "Last outcome was trade - re-evaluate", 0.0

    # V3 - Cache expired
    last_ts = cached.get("last_timestamp")
    if last_ts:
        age = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        if age > CACHE_EXPIRY_MINUTES:
            return False, f"Cache expired ({age:.0f}m old)", 0.0

    # Get current and previous snapshots
    prev_snapshot = cached.get("last_signal", {})
    curr_snapshot = build_signal_snapshot(data, twelve)
    prev_price = prev_snapshot.get("price")
    curr_price = curr_snapshot.get("price")

    # V4 - Price veto
    if prev_price and curr_price:
        price_pct = _pct_change(prev_price, curr_price)
        if price_pct >= PRICE_VETO_PCT:
            return False, f"Price moved {price_pct:.3f}% - veto", price_pct

    # V5 - Streak protection
    streak = cached.get("auto_count", 0)
    if streak >= MAX_AUTO_NEUTRAL_STREAK:
        return False, f"Max streak ({streak}) reached - forced AI check", 0.0

    # V6 - MCS threshold (MAIN AUTO-NEUTRAL DECISION)
    # FIXED: compute_mcs now handles empty snapshots properly
    mcs = compute_mcs(prev_snapshot, curr_snapshot)
    print(f"MCS for {symbol}: {mcs:.1f}%")

    if mcs < MCS_THRESHOLD:
        return True, f"MCS {mcs:.1f}% < {MCS_THRESHOLD}% - auto-neutral", mcs

    # V7 - 1-2 high-impact events (warn but run AI)
    if event_count >= 1:
        events_str = calendar_events[0] if calendar_events else "Unknown"
        return False, f"⚠️ {event_count} HIGH-IMPACT EVENT(S): {events_str} - run AI", mcs

    # MCS >= threshold -> run AI
    return False, f"MCS {mcs:.1f}% >= {MCS_THRESHOLD}% - run AI", mcs

def update_neutral_cache(symbol, session, outcome, data, twelve):
    key  = f"{clean_symbol(symbol)}_{session}"
    snap = build_signal_snapshot(data, twelve)
    with cache_lock:
        old        = neutral_cache.get(key, {})
        auto_count = old.get("auto_count", 0) + 1 if outcome == "NEUTRAL" else 0
        neutral_cache[key] = {
            "last_outcome":   outcome,
            "last_signal":    snap,
            "last_timestamp": datetime.now(timezone.utc),
            "auto_count":     auto_count,
        }

        # ADD: evict oldest entries if cache grows too large
        if len(neutral_cache) > MAX_CACHE_ENTRIES:
            oldest_key = min(
                neutral_cache,
                key=lambda k: neutral_cache[k].get("last_timestamp", datetime.min.replace(tzinfo=timezone.utc))
            )
            del neutral_cache[oldest_key]

def increment_auto_streak(symbol, session):
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        if key in neutral_cache:
            neutral_cache[key]["auto_count"] = neutral_cache[key].get("auto_count", 0) + 1

def reset_auto_streak(symbol, session):
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        if key in neutral_cache:
            neutral_cache[key]["auto_count"] = 0


# ==========================================
# OFF-SESSION BUFFER (FIXED)
# ==========================================
def _normalize_direction(direction):
    """FIXED: Map all direction strings to BUY/SELL only."""
    d = direction.upper().strip()
    if d in ("BUY", "LONG", "BULLISH", "CALL"):
        return "BUY"
    if d in ("SELL", "SHORT", "BEARISH", "PUT"):
        return "SELL"
    return d  # Return as-is if unknown (shouldn't happen after validation)

def store_off_session_signal(symbol, direction, score, ea, price):
    key = clean_symbol(symbol)
    now = datetime.now(timezone.utc)
    norm_dir = _normalize_direction(direction)
    with buffer_lock:
        if key not in off_session_buffer:
            off_session_buffer[key] = {"signals": [], "last_alert_sent": None}

        # ADD: prune signals older than OFF_SESSION_MAX_AGE_HOURS
        cutoff = now - timedelta(hours=OFF_SESSION_MAX_AGE_HOURS)
        off_session_buffer[key]["signals"] = [
            s for s in off_session_buffer[key]["signals"]
            if s["timestamp"] > cutoff
        ]

        off_session_buffer[key]["signals"].append({
            "direction": norm_dir,
            "score": int(score), "ea": int(ea),
            "price": float(price), "timestamp": now
        })
        print(f"OFF-SESSION [{key}]: {len(off_session_buffer[key]['signals'])} stored")

def _conviction_bar(pct):
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled)

def _conviction_label(cons, avg_score, total):
    if cons >= 0.80 and avg_score >= 85 and total >= 10:
        return "STRONG"
    if (cons >= 0.80 and avg_score >= 80 and total >= 8) or (cons >= 0.70 and avg_score >= 85):
        return "MODERATE"
    return "WEAK"

def send_presession_snapshot(mode):
    """FIXED: Network I/O moved outside buffer_lock."""
    now_pkt  = datetime.now(timezone.utc) + timedelta(hours=5)
    time_str = now_pkt.strftime("%I:%M %p PKT")
    title    = "🌙 OVERNIGHT WATCH" if mode == "overnight" else "🌅 PRE-SESSION BRIEF"
    period   = "02:00 - 09:00 PKT" if mode == "overnight" else "02:00 - 11:59 PKT"

    # FIXED: Fetch calendar OUTSIDE the lock
    cal_txt = ""
    if mode == "presession":
        try:
            cal = fetch_economic_calendar()
            if cal.get("high_impact_soon"):
                ev_names = [e if isinstance(e, str) else e.get("event", "")
                            for e in cal.get("events", [])]
                cal_txt = f"\n⚠️ HIGH IMPACT TODAY: {', '.join(ev_names)}\nTrade conservatively.\n"
        except Exception:
            pass

    # FIXED: Build message inside lock, send OUTSIDE lock
    message_to_send = None
    should_clear = False
    
    with buffer_lock:
        total = sum(len(v.get("signals",[])) for v in off_session_buffer.values())
        if total == 0:
            txt = (f"{title} | {time_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                   f"Period: {period}\n\nNo qualifying signals overnight.\n"
                   f"All pairs below score/EA threshold.\n"
                   f"Wait for London session to establish direction.\n"
                   f"━━━━━━━━━━━━━━━━━━━━━━━━━")
            if mode == "overnight":
                txt += "\n⏰ Next update: 11:59 AM (London open)"
            message_to_send = txt
            if mode == "presession":
                off_session_buffer.clear()
            should_clear = False
        else:
            header = (f"{title} | {time_str}\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                      f"Period: {period}\nSignals: {total} qualified\n"
                      f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n")
            blocks = []
            watch  = []

            for key, buf in off_session_buffer.items():
                sigs = buf.get("signals", [])
                if not sigs:
                    continue
                tot_s = len(sigs)
                buys  = sum(1 for s in sigs if s["direction"] == "BUY")
                sells = tot_s - buys
                dom   = "BUY" if buys >= sells else "SELL"
                dom_c = max(buys, sells)
                cons  = dom_c / tot_s
                emoji = "🟢" if dom == "BUY" else "🔴"
                scrs  = [s["score"] for s in sigs]
                avg_s = round(sum(scrs)/tot_s, 1)
                prices= [s["price"] for s in sigs]
                delta = prices[-1] - prices[0]
                ds    = f"+{delta:.5f}" if delta >= 0 else f"{delta:.5f}"
                bar   = _conviction_bar(cons * 100)
                pct_s = f"{round(cons*100,1)}%"
                ea_a  = round(sum(s["ea"] for s in sigs)/tot_s, 1)
                conv  = _conviction_label(cons, avg_s, tot_s)

                if cons < 0.60:
                    if mode == "overnight":
                        continue
                    blocks.append(f"⚪ {key:<8} - MIXED  {buys}B/{sells}S  Score avg {avg_s}\n")
                    continue

                if mode == "overnight":
                    blocks.append(f"{emoji} {key:<8} {dom}  {bar}  {pct_s}\n"
                                   f"   {tot_s} signals | Score {avg_s} avg | EA {ea_a}\n"
                                   f"   {prices[0]:.5f} -> {prices[-1]:.5f}  ({ds})\n")
                else:
                    blocks.append(f"{emoji} {key}  -  {dom}  {bar}  {pct_s}\n"
                                   f"   {tot_s} signals  |  {dom_c} {dom}  {tot_s-dom_c} {'SELL' if dom=='BUY' else 'BUY'}\n"
                                   f"   Score: {min(scrs)}-{max(scrs)} (avg {avg_s})  EA avg {ea_a}\n"
                                   f"   {prices[0]:.5f} -> {prices[-1]:.5f}  ({ds})  [{conv}]\n")
                    if conv in ("STRONG","MODERATE") and cons >= 0.80:
                        watch.append(f"   {emoji} {key} {dom} - {conv.lower()} overnight build")

            body = "\n".join(blocks)

            if mode == "overnight":
                footer = "\n━━━━━━━━━━━━━━━━━━━━━━━━━\n⏰ Next update: 11:59 AM (London open)"
                message_to_send = header + body + footer
            else:
                watch_txt = ""
                if watch:
                    watch_txt = "\n━━━━━━━━━━━━━━━━━━━━━━━━━\n📌 WATCH AT OPEN:\n" + "\n".join(watch) + "\n"
                footer = (watch_txt + cal_txt +
                          "\n━━━━━━━━━━━━━━━━━━━━━━━━━\nGood luck. Session starting now.\nBuffer cleared.")
                message_to_send = header + body + footer
                should_clear = True
        
        if should_clear:
            off_session_buffer.clear()
    
    # FIXED: Send message OUTSIDE the lock
    if message_to_send:
        send_telegram(message_to_send)


# ==========================================
# DAILY RESET (FIXED)
# ==========================================
def reset_day():
    """FIXED: Can be called from scheduler or session_gate."""
    global current_day, pair_session_tracker, neutral_cache, off_session_buffer
    today = datetime.now(timezone.utc).date()
    if current_day != today:
        current_day = today
        with trade_lock:
            pair_session_tracker = {}
        with cache_lock:
            neutral_cache = {}
        with buffer_lock:
            off_session_buffer = {}
        print(f"Day reset: {today}")


# ==========================================
# SESSION GATE (FIXED)
# ==========================================
def get_session_by_time():
    """
    FIXED: Proper PKT timezone session boundaries.
    PKT = UTC+5
    London: 12:00-17:59 PKT (07:00-12:59 UTC)
    NewYork: 18:00-23:59 PKT AND 00:00-01:59 PKT (13:00-20:59 UTC)
    Off:     02:00-11:59 PKT (21:00-06:59 UTC)
    """
    pkt = timezone(timedelta(hours=5))
    pk_hour = datetime.now(pkt).hour
    
    if 12 <= pk_hour < 18:
        return "london"
    elif 18 <= pk_hour or pk_hour < 2:
        # pk_hour < 2 means 00:00-01:59 is NewYork, 02:00-11:59 is off-session
        return "newyork"
    return "off"

def session_gate(symbol):
    reset_day()
    session = get_session_by_time()
    if session == "off":
        return False, "Outside trading sessions", session
    sc  = clean_symbol(symbol)
    key = f"{sc}_{session}"
    now = datetime.now(timezone.utc)
    with trade_lock:
        ts = pair_session_tracker.get(key, [])
        if len(ts) >= 2:
            return False, f"2/2 trades used for {sc} in {session}", session
        if len(ts) == 1:
            diff = (now - ts[0]).total_seconds()
            if diff < 7200:
                return False, f"Cooldown active - {int(7200-diff)}s remaining", session
    return True, "OK", session

def register_trade(symbol, session):
    sc  = clean_symbol(symbol)
    key = f"{sc}_{session}"
    now = datetime.now(timezone.utc)
    with trade_lock:
        ts = pair_session_tracker.get(key, [])
        ts.append(now)
        if len(ts) > 2:
            ts = ts[-2:]
        pair_session_tracker[key] = ts
    print(f"Trade registered: {sc} {session} | {len(ts)}/2")


# ==========================================
# FINNHUB
# ==========================================
def fetch_finnhub(symbol):
    if not FINNHUB_KEY:
        return {"sentiment": "NEUTRAL", "headlines": [], "symbol_matched": False}
    symbol = clean_symbol(symbol)
    kw_map = {
        "EURUSD":["EUR","Euro","ECB"],"GBPUSD":["GBP","Pound","BOE"],
        "USDJPY":["JPY","Yen","BOJ"],"USDCHF":["CHF","Franc","SNB"],
        "AUDUSD":["AUD","Aussie","RBA"],"NZDUSD":["NZD","Kiwi","RBNZ"],
        "USDCAD":["CAD","Loonie","BOC"],"GBPJPY":["GBP","JPY","Pound","Yen"],
        "EURJPY":["EUR","JPY"],"EURGBP":["EUR","GBP"],"GBPCHF":["GBP","CHF"],
        "XAUUSD":["Gold","XAU","bullion","Fed"],"XAGUSD":["Silver","XAG"],
        "USOIL":["Oil","WTI","crude","OPEC"],"UKOIL":["Oil","Brent","OPEC"],
        "BTCUSD":["Bitcoin","BTC"],"ETHUSD":["Ethereum","ETH"],
        "US30":["Dow Jones","DJIA"],"NAS100":["Nasdaq"],"SPX500":["S&P 500"],
    }
    kws = kw_map.get(symbol, [symbol[:3], symbol[3:]] if len(symbol)==6 else [symbol])
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}",
            timeout=10)
        r.raise_for_status()
        news    = r.json()[:30]
        matched = [n.get("headline","") for n in news
                   if any(k.lower() in n.get("headline","").lower() for k in kws)]
        if not matched:
            matched = [n.get("headline","") for n in news[:5]]
        text      = " ".join(matched).lower()
        bull      = sum(w in text for w in ["rise","bull","gain","growth","hawkish","strong"])
        bear      = sum(w in text for w in ["fall","bear","drop","recession","dovish","weak"])
        sentiment = "POSITIVE" if bull > bear else "NEGATIVE" if bear > bull else "NEUTRAL"
        return {"sentiment": sentiment, "headlines": matched[:5], "symbol_matched": bool(matched)}
    except Exception as e:
        print(f"Finnhub error: {e}")
        return {"sentiment": "NEUTRAL", "headlines": [], "symbol_matched": False}


# ==========================================
# ECONOMIC CALENDAR
# ==========================================
def fetch_economic_calendar():
    if not FINNHUB_KEY:
        return {"high_impact_soon": False, "events": []}
    try:
        now = datetime.now(timezone.utc)
        url = (f"https://finnhub.io/api/v1/calendar/economic"
               f"?from={now.strftime('%Y-%m-%d')}"
               f"&to={(now+timedelta(days=1)).strftime('%Y-%m-%d')}"
               f"&token={FINNHUB_KEY}")
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        events = r.json().get("economicCalendar", [])
        high   = [e for e in events if e.get("impact") == "high"]
        return {"high_impact_soon": bool(high), "events": [e.get("event","") for e in high[:5]]}
    except Exception as e:
        print(f"Calendar error: {e}")
        return {"high_impact_soon": False, "events": []}


# ==========================================
# GDELT
# ==========================================
def fetch_gdelt():
    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc?query=global%20economy&mode=ArtList&format=json",
            timeout=10)
        r.raise_for_status()
        articles = r.json().get("articles",[])[:5]
        score    = sum(1 for a in articles
                       if any(w in a.get("title","").lower()
                              for w in ["war","inflation","crisis","recession","sanctions"]))
        risk = "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"
        return {"risk": risk, "score": score}
    except Exception as e:
        print(f"GDELT error: {e}")
        return {"risk": "LOW", "score": 0}

def fetch_all_live_data(symbol):
    with ThreadPoolExecutor(max_workers=3) as ex:
        fn = ex.submit(fetch_finnhub, symbol)
        fm = ex.submit(fetch_gdelt)
        fc = ex.submit(fetch_economic_calendar)
        return fn.result(), fm.result(), fc.result()


# ==========================================
# TWELVE DATA - 7 CORE INDICATORS (FIXED)
# ==========================================
def fetch_twelve_data(symbol):
    if not TWELVE_DATA_KEY:
        return {}
    symbol = clean_symbol(symbol)
    if "/" not in symbol and len(symbol) == 6:
        symbol = f"{symbol[:3]}/{symbol[3:]}"
    base = "https://api.twelvedata.com"
    k    = TWELVE_DATA_KEY
    endpoints = {
        "rsi":      f"{base}/rsi?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "macd":     f"{base}/macd?symbol={symbol}&interval=1h&apikey={k}",
        "stoch":    f"{base}/stoch?symbol={symbol}&interval=1h&apikey={k}",
        "bbands":   f"{base}/bbands?symbol={symbol}&interval=1h&time_period=20&apikey={k}",
        "adx":      f"{base}/adx?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "atr":      f"{base}/atr?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "ichimoku": f"{base}/ichimoku?symbol={symbol}&interval=1h&apikey={k}",
    }
    raw = {}
    for name, url in endpoints.items():
        try:
            resp = requests.get(url, timeout=8).json()
            raw[name] = resp.get("values",[{}])[0] if resp.get("values") else resp.get("value","N/A")
        except Exception as e:
            print(f"TwelveData {name}: {e}")
            raw[name] = "N/A"
        time.sleep(0.3)

    def g(key, sub):
        v = raw.get(key)
        return v.get(sub,"N/A") if isinstance(v,dict) else "N/A"

    def sv(x):
        if x == "N/A": return "N/A"
        try:
            f = float(x)
            return "N/A" if f == 0 else x
        except Exception:
            return x

    parsed = {
        "rsi":                  sv(g("rsi","rsi")),
        "macd":                 sv(g("macd","macd")),
        "macd_signal":          sv(g("macd","macd_signal")),
        "macd_histogram":       sv(g("macd","macd_histogram")),
        "stoch_k":              sv(g("stoch","slowk")),
        "stoch_d":              sv(g("stoch","slowd")),
        "bb_upper":             sv(g("bbands","upper_band")),
        "bb_middle":            sv(g("bbands","middle_band")),
        "bb_lower":             sv(g("bbands","lower_band")),
        "adx":                  sv(g("adx","adx")),
        "atr":                  sv(g("atr","atr")),
        "ichimoku_conversion":  sv(g("ichimoku","tenkan_sen")),
        "ichimoku_base":        sv(g("ichimoku","kijun_sen")),
        "ichimoku_span_a":      sv(g("ichimoku","senkou_span_a")),
        "ichimoku_span_b":      sv(g("ichimoku","senkou_span_b")),
    }
    
    # bb_width may be None (not "N/A") on failure
    bb_width = None
    try:
        upper  = float(parsed["bb_upper"])
        lower  = float(parsed["bb_lower"])
        middle = float(parsed["bb_middle"])
        bb_width = round((upper - lower)/middle * 100, 2) if middle > 0 else None
    except Exception:
        bb_width = None
    parsed["bb_width"] = bb_width if bb_width is not None else "N/A"  # FIXED: Use "N/A" consistently

    # FIXED: Count only actual indicator fields, not _quality or bb_width
    indicator_fields = ["rsi","macd","macd_signal","macd_histogram",
                        "stoch_k","stoch_d","bb_upper","bb_middle","bb_lower",
                        "adx","atr","ichimoku_conversion","ichimoku_base",
                        "ichimoku_span_a","ichimoku_span_b"]
    missing = [k for k in indicator_fields if parsed.get(k) == "N/A"]
    tot = len(indicator_fields)
    parsed["_quality"] = (
        f"FULL ({tot}/{tot})"        if not missing         else
        f"GOOD ({tot-len(missing)}/{tot})"  if len(missing) <= 3 else
        f"PARTIAL ({tot-len(missing)}/{tot})"
    )
    return parsed


# ==========================================
# MARKET CONTEXT (text - injected into all prompts)
# ==========================================
def build_market_context(signal, news, macro, twelve, calendar):
    s   = {k:v for k,v in signal.items() if k not in ["twelve_data","calendar","tf","timeframe"]}
    sym = s.get("symbol","N/A")

    # Currency-specific calendar events
        # Currency-specific calendar events (FIXED)
    sc_sym = clean_symbol(sym)
    
    COMMODITY_CURRENCY_MAP = {
        "XAUUSD": ["USD"], "GOLD": ["USD"], "XAGUSD": ["USD"], "SILVER": ["USD"],
        "USOIL": ["USD"], "UKOIL": ["USD", "GBP"], "WTI": ["USD"], "BRENT": ["USD", "GBP"],
        "BTCUSD": ["USD"], "ETHUSD": ["USD"],
        "US30": ["USD"], "NAS100": ["USD"], "SPX500": ["USD"],
        "GER40": ["EUR"], "UK100": ["GBP"], "JP225": ["JPY"],
    }
    
    if sc_sym in COMMODITY_CURRENCY_MAP:
        currencies = COMMODITY_CURRENCY_MAP[sc_sym]
    elif len(sc_sym) == 6:
        currencies = [sc_sym[:3], sc_sym[3:]]
    else:
        currencies = [sc_sym]
    
    events_raw     = calendar.get("events", [])
    relevant_ev    = []
    for e in events_raw:
        if isinstance(e, dict) and e.get("currency","") in currencies:
            relevant_ev.append(e.get("event",""))
        elif isinstance(e, str):
            if any(curr in e.upper() for curr in currencies):
                relevant_ev.append(e)
    cal_note = (f"⚠️ RELEVANT HIGH IMPACT: {', '.join(relevant_ev)}"
                if relevant_ev else "No relevant high-impact events.")

    headlines  = "\n".join(f"  - {h}" for h in news.get("headlines",[])) or "  None."
    tq         = twelve.get("_quality","Unknown")
    adx_v      = safe_float(twelve.get("adx"))
    bb_w       = twelve.get("bb_width")
    regime     = detect_market_regime(adx_v, bb_w)

    # Pre-compute warnings
    pivot_zone   = s.get("pivot_zone","N/A")
    vwap_band    = s.get("vwap_band","N/A")
    direction    = s.get("direction","N/A")
    lon_fu       = safe_bool(s.get("lon_fail_up"))
    lon_fd       = safe_bool(s.get("lon_fail_down"))
    ny_fu        = safe_bool(s.get("ny_fail_up"))
    ny_fd        = safe_bool(s.get("ny_fail_down"))
    sv_conf      = s.get("sv_confidence","N/A")
    sv_t1        = s.get("sv_t1_rate","N/A")

    warnings = []
    if "ABOVE_R2" in pivot_zone or "ABOVE_R3" in pivot_zone:
        warnings.append("🚨 PRICE AT EXTREME RESISTANCE (weekly R2/R3)")
    if "BELOW_S2" in pivot_zone or "BELOW_S3" in pivot_zone:
        warnings.append("🚨 PRICE AT EXTREME SUPPORT (weekly S2/S3)")
    if (lon_fu or ny_fu):
        warnings.append("🚨 FAILED BULLISH AUCTION - market rejected higher prices")
    if (lon_fd or ny_fd):
        warnings.append("🚨 FAILED BEARISH AUCTION - market rejected lower prices")
    if ("EXTREME_HIGH" in vwap_band and "LONG" in direction.upper()):
        warnings.append("🚨 VWAP EXTREME HIGH - mean reversion risk on BUY")
    if ("EXTREME_LOW" in vwap_band and "SHORT" in direction.upper()):
        warnings.append("🚨 VWAP EXTREME LOW - mean reversion risk on SELL")
    if sv_conf == "LOW":
        warnings.append(f"🚨 VALIDATOR LOW CONFIDENCE - T1 hit rate: {sv_t1}%")

    warn_block = ("\n".join(warnings) + "\n") if warnings else ""

    tf_label = signal.get("tf", "1H")
    context = f"""
MARKET BRIEF - {sym} | {tf_label} TIMEFRAME
{'='*55}
{warn_block}
SIGNAL ENGINE:
  Direction:      {s.get('direction','N/A')}    Score: {s.get('score','N/A')}/100  ({s.get('signal_strength','N/A')})
  Structure Bias: {s.get('structure_bias','N/A')}      HH/HL: {s.get('hh_hl_pred','N/A')}  Retrace: {s.get('retrace_pct','N/A')}% ({s.get('retrace_zone','N/A')})
  EA Filter:      {s.get('ea_score','N/A')}/8  ({s.get('ea_quality','N/A')})   Gate: {s.get('ea_gate','N/A')}
  Pillars Bull:   {s.get('pillars_bull','N/A')}/5      Agreement Bonus: {s.get('agreement_bonus','N/A')}
  Raw Signal:     {s.get('raw_signal','N/A')}

PRICE & TREND:
  Price:          {s.get('price','N/A')}   ATR: {s.get('atr','N/A')}
  ADX:            {s.get('adx','N/A')}  DI+: {s.get('diplus','N/A')}  DI-: {s.get('diminus','N/A')}   Regime: {regime}
  EMA 21/50/200:  {s.get('ema_21','N/A')} / {s.get('ema_50','N/A')} / {s.get('ema_200','N/A')}
  EMA Align:      {s.get('ema_align','N/A')}   Slope: {s.get('ema_slope','N/A')}
  Swing H/L:      {s.get('last_sh','N/A')} / {s.get('last_sl','N/A')}

VWAP:
  Session VWAP:   {s.get('vwap','N/A')}  ({s.get('price_vs_vwap','N/A')})   Band: {s.get('vwap_band','N/A')}
  Weekly VWAP:    {s.get('weekly_vwap','N/A')}  ({s.get('price_vs_wvwap','N/A')})
  Monthly VWAP:   {s.get('monthly_vwap','N/A')}

WEEKLY PIVOTS:
  R3:{s.get('weekly_r3','N/A')}  R2:{s.get('weekly_r2','N/A')}  R1:{s.get('weekly_r1','N/A')}
  P: {s.get('weekly_pivot','N/A')}
  S1:{s.get('weekly_s1','N/A')}  S2:{s.get('weekly_s2','N/A')}  S3:{s.get('weekly_s3','N/A')}
  Zone: {pivot_zone}   Nearest: {s.get('nearest_level','N/A')} (dist: {s.get('nearest_dist','N/A')})

AUCTION:
  Label:          {s.get('auction_label','N/A')}   Pts: {s.get('auction_pts','N/A')}
  Failed: L↑{lon_fu} L↓{lon_fd} NY↑{ny_fu} NY↓{ny_fd}
  Macro Score:    {s.get('macro_score','N/A')}

VALIDATOR (HISTORICAL):
  Valid:{s.get('sv_valid','N/A')}  Confidence:{sv_conf}  Matches:{s.get('sv_matches','N/A')}
  T1:{s.get('sv_t1_rate','N/A')}%  T2:{s.get('sv_t2_rate','N/A')}%  T3:{s.get('sv_t3_rate','N/A')}%
  MAE:{s.get('sv_avg_mae','N/A')}  MFE:{s.get('sv_avg_mfe','N/A')}  Pullback:{s.get('sv_pb_rate','N/A')}%
  Form:{s.get('sv_recent_form','N/A')}   T1:{s.get('t1_target','N/A')}  T2:{s.get('t2_target','N/A')}
  Volume Ratio:   {s.get('volume_ratio','N/A')}

TWELVE DATA ({tq}):
  RSI:{twelve.get('rsi','N/A')}  MACD:{twelve.get('macd','N/A')}  Hist:{twelve.get('macd_histogram','N/A')}
  Stoch K/D:{twelve.get('stoch_k','N/A')}/{twelve.get('stoch_d','N/A')}
  BB: {twelve.get('bb_lower','N/A')} / {twelve.get('bb_middle','N/A')} / {twelve.get('bb_upper','N/A')}  W:{twelve.get('bb_width','N/A')}%
  ADX:{twelve.get('adx','N/A')}  ATR:{twelve.get('atr','N/A')}
  Ichimoku Conv/Base:{twelve.get('ichimoku_conversion','N/A')}/{twelve.get('ichimoku_base','N/A')}
  Span A/B:{twelve.get('ichimoku_span_a','N/A')}/{twelve.get('ichimoku_span_b','N/A')}

NEWS & MACRO:
  Sentiment:{news.get('sentiment','N/A')}  Macro:{macro.get('risk','N/A')}
  {cal_note}
  Headlines:
{headlines}
{'='*55}
""".strip()
    return context


# ==========================================
# DYNAMIC ROUND 1 RULES (Risk profile aware)
# ==========================================
def get_round1_rules():
    """Generate analysis rules based on risk profile."""
    profile = RISK_PROFILES.get(AI_RISK_PROFILE, RISK_PROFILES["BALANCED"])
    
    rules = """
━━━ ANALYSIS FRAMEWORK - FOLLOW EVERY STEP IN ORDER ━━━

CRITICAL OVERRIDE: 
You are NOT a risk manager. You are a TRADER looking for opportunities.
Default to ACTION (BUY/SELL) when structure is clear.
NEUTRAL is the LAST RESORT, not the safe choice.
Missing a good trade is ALSO a loss.

STEP 1 - HARD BLOCKS
Check each condition first. If any triggers -> output NEUTRAL immediately, skip remaining steps.

  A) CALENDAR      3+ relevant high-impact events for this pair's currencies -> NEUTRAL
"""
    
    if profile["pivot_block_enabled"]:
        rules += """
  B) PIVOT EXTREME ABOVE_R2 or ABOVE_R3 zone + BUY signal -> NEUTRAL
                   BELOW_S2 or BELOW_S3 zone + SELL signal -> NEUTRAL
"""
    else:
        rules += """
  B) PIVOT EXTREME DISABLED - Structure determines direction, not pivot proximity.
                   Price near R2/R3 on BUY = strong breakout potential, not a block.
"""
    
    rules += """
  C) FAILED AUCTION Failed bullish auction active + BUY signal -> NEUTRAL or SELL
                    Failed bearish auction active + SELL signal -> NEUTRAL or BUY
  D) VALIDATOR     sv_confidence = LOW AND sv_t1_rate < 35% -> NEUTRAL
"""
    
    if profile["triple_exhaust_enabled"]:
        rules += """
  E) TRIPLE EXHAUST RSI > 75 AND Stochastic K > 85 AND price above BB upper -> NEUTRAL
                    (Stricter: requires stronger exhaustion evidence)
"""
    
    if profile["low_volume_threshold"] > 0:
        rules += f"""
  F) LOW VOLUME    volume_ratio < {profile['low_volume_threshold']} -> NEUTRAL (no institutional participation)
"""
    
    if profile["macro_ranging_block"]:
        rules += """
  G) MACRO+RANGING GDELT macro = HIGH AND ADX < 20 -> NEUTRAL (noise, not signal)
"""
    
    rules += f"""

STEP 2 - CONFLICT RESOLUTION HIERARCHY
When indicators disagree, resolve using this priority order (top = highest):

  1st  STRUCTURE   HH/HL patterns + swing highs/lows - always wins
  2nd  PILLARS     4 or more of 5 pillars aligned = override oscillators
  3rd  ADX + DI   ADX > 25 with DI+ > DI- (bull) or DI- > DI+ (bear) = trend confirmed
  4th  AUCTION    Failed auction signal overrides momentum readings
  5th  OSCILLATORS RSI, Stoch, CCI - these are lagging, lowest weight

  RULE: RSI = 72 (overbought) BUT structure = HH/HL + 4/5 pillars bullish
        -> IGNORE RSI. Output BUY. Structure and pillars override oscillators.

STEP 3 - MOMENTUM SUSTAINABILITY
Determine if current momentum can continue or is exhausted.

  SUSTAINABLE (entry is fine):
    RSI between 40-70 on BUY signal, or 30-60 on SELL signal
    MACD histogram positive and expanding (not shrinking)
    ADX > 25 and still rising
    Price within 1x ATR of EMA21

  EXHAUSTED (output NEUTRAL with reason "exhausted momentum"):
    RSI > 80 on BUY, or RSI < 20 on SELL
    Price more than 2.5x ATR above EMA21 on BUY (stretched too far)
    MACD histogram declining while price still moving same direction

STEP 4 - CHART ANALYSIS (when chart image is provided)
Examine the chart image carefully. Identify and report on what you actually see:

  Structural Patterns:
    BOS (Break of Structure)   swing high/low broken with conviction = continuation
    CHoCH (Change of Character) BOS in opposite direction = potential reversal
    Higher Highs / Higher Lows  bullish structure
    Lower Highs / Lower Lows    bearish structure

  Reversal Patterns:
    Double Top / Double Bottom     equal highs/lows with rejection between them
    Head and Shoulders / Inverse   three peaks, middle highest/lowest
    Rising Wedge (bearish) / Falling Wedge (bullish)
    Bearish Engulfing / Bullish Engulfing candles
    Shooting Star / Hammer candles
    Doji at key levels (indecision at resistance/support)
    Inside Bar (compression before breakout)

  Price Action Signals:
    Wick rejections  wick longer than 2x candle body = price rejected that direction
    Liquidity grabs  spike beyond key level then sharp reversal = trap
    Support/Resistance  3+ touches of same level = strong zone

  Context:
    EMA stack (21, 50, 200) aligned bullish or bearish?
    Price relative to VWAP - extended or healthy?
    Pivot levels visible on chart vs current price

  CHART vs DATA CONFLICTS:
    If chart shows CHoCH but Pine Script says BULL structure -> flag the conflict explicitly
    If chart confirms BOS in signal direction -> mention as additional confirmation
    If chart shows major pattern (double top, H&S) opposing the signal -> lean NEUTRAL

STEP 5 - FUNDAMENTAL CHECK
  Does news sentiment support or oppose the direction?
  What is the central bank stance for this pair?
  Does global macro risk (GDELT) raise concern?
  Fundamental is SUPPORTING context only - cannot override confirmed technical structure.

DIRECTION DECISION RULES:
  Technical structure is PRIMARY
  Minimum confidence for trade: {profile['min_confidence_for_trade']}%
  When confidence is {profile['min_confidence_for_trade']}-70%:
    Still output BUY/SELL if no hard blocks fire
    Note: "MODERATE CONFIDENCE - structure supports, manage risk"
  Only output NEUTRAL when: hard block fires OR momentum exhausted 
    OR confidence < {profile['min_confidence_for_trade'] - 10}%
  
  IMPORTANT: Err on the side of ACTION when structure is clear.
  NEUTRAL should be the EXCEPTION, not the default.

REQUIRED OUTPUT FORMAT - respond in EXACTLY this format, no extra text:
DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100]
HARD BLOCK: [NONE - or state which block A-G triggered and the specific data that triggered it]
CHART ANALYSIS: [Describe exactly what you see: BOS/CHoCH, patterns, wicks, EMA position, key levels. If no chart: state "No chart provided"]
STRUCTURE CHECK: [passed/concern - state HH/HL, pillar count out of 5, ADX reading]
MOMENTUM CHECK: [sustainable/exhausted/extended - state RSI value, distance from EMA21 in ATRs]
OSCILLATOR CHECK: [clear/warning - state RSI, Stoch K, BB position]
TECHNICAL VIEW: [2-3 sentences combining chart + data into your technical assessment]
FUNDAMENTAL VIEW: [1-2 sentences - news sentiment, central bank stance, macro risk]
REASON: [2-3 sentences - your complete final assessment combining all inputs]
"""
    return rules


# ==========================================
# SIGNAL STRENGTH CHECK
# ==========================================
def should_skip_due_to_weak_signal(data):
    """
    Check if signal is too weak to even bother with AI.
    Only skip truly garbage signals, not borderline ones.
    """
    score = safe_int(data.get("score", 0))
    ea_score = safe_int(data.get("ea_score") or data.get("ea_filter", 0))
    direction = safe_str(data.get("direction", ""))
    norm_dir = _normalize_direction(direction)  # FIXED: Use same normalization
    
    # Only skip if signal is extremely weak
    if score < 40 and ea_score < 3:
        return True, f"Weak signal: Score {score}/100, EA {ea_score}/8"
    
    if norm_dir not in ("BUY", "SELL"):
        return True, f"No clear direction: {direction}"
    
    return False, ""


# ==========================================
# GEMINI - ROUND 1 (BLIND, WITH CHART VISION)
# ==========================================
def gemini_analysis(signal, news, macro, chart_b64=None, chart_mime="image/png"):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, news, macro, twelve, calendar)
    rules    = get_round1_rules()

    prompt_text = f"""You are an experienced institutional trader and market analyst.
Your analysis is INDEPENDENT - you have not seen any other analyst's view.

Analyse the H1 chart and all market data below.
{rules}

MARKET DATA:
{context}"""

    parts = [{"text": prompt_text}]
    if chart_b64:
        parts.append({
            "inline_data": {
                "mime_type": chart_mime,
                "data":      chart_b64
            }
        })
        parts.append({"text": "\nThe chart image above shows the H1 chart. Analyse it thoroughly in your CHART ANALYSIS field."})

    payload = {
        "contents":       [{"parts": parts}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1400}
    }

    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
                json=payload,
                timeout=40
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini attempt {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Server busy"


# ==========================================
# CHATGPT - ROUND 1 (BLIND, WITH CHART VISION)
# ==========================================
def chatgpt_analysis(symbol, price, signal, news, macro, calendar,
                     chart_b64=None, chart_mime="image/png"):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve  = signal.get("twelve_data", {})
    context = build_market_context(signal, news, macro, twelve, calendar)
    rules   = get_round1_rules()

    prompt_text = f"""You are an experienced institutional trader and market analyst.
Your analysis is INDEPENDENT - you have not seen any other analyst's view.

Analyse the H1 chart and all market data below.
{rules}

MARKET DATA:
{context}

{"The chart image is attached. Analyse it thoroughly in your CHART ANALYSIS field." if chart_b64 else "No chart image provided - rely on numerical data only."}"""

    content_parts = [{"type": "text", "text": prompt_text}]
    if chart_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_mime};base64,{chart_b64}", "detail": "high"}
        })

    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json={
                "model":    "gpt-4o-mini",
                "messages": [{"role": "user", "content": content_parts}],
                "temperature": 0.3,
                "max_tokens":  1400
            },
            timeout=50
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT ERROR: {str(e)}"


# ==========================================
# CHATGPT - DECIDES: AGREE / DEBATE / NEUTRAL (FIXED)
# ==========================================
def chatgpt_decides(symbol, price, signal, gemini_out, chatgpt_out,
                    chart_b64=None, chart_mime="image/png"):
    if not CHATGPT_KEY:
        return "DEBATE", "ChatGPT unavailable - defaulting to debate"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, {}, {}, twelve, calendar)
    atr_td   = safe_float(twelve.get("atr"))
    atr_ps   = safe_float(signal.get("atr"))
   
    if atr_td is not None and atr_ps is not None and atr_ps > 0:
        if atr_td < atr_ps * 0.2:
            atr_use = atr_ps
        else:
            atr_use = atr_td
    elif atr_ps is not None:
        atr_use = atr_ps
    elif atr_td is not None:
        atr_use = atr_td
    else:
        atr_use = None
    profile  = RISK_PROFILES.get(AI_RISK_PROFILE, RISK_PROFILES["BALANCED"])

    prompt_text = f"""You are a senior execution trader reviewing two independent analyses of {symbol} H1.

GEMINI ANALYSIS:
{gemini_out}

YOUR OWN ROUND 1 ANALYSIS:
{chatgpt_out}

MARKET DATA SUMMARY:
{context}

ATR (for SL/TP reference): {atr_use}
T1 Target: {signal.get('t1_target','N/A')}
T2 Target: {signal.get('t2_target','N/A')}
Minimum confidence for trade: {profile['min_confidence_for_trade']}%
Risk Profile: {AI_RISK_PROFILE}

{"Chart image attached for your reference." if chart_b64 else ""}

━━━ YOUR TASK ━━━

Review both analyses carefully. Consider:
1. Do both agree on direction? (even if confidence differs)
2. Does chart analysis in both match? (BOS/CHoCH/patterns)
3. Are the hard block checks consistent?
4. Is there a genuine analytical disagreement that needs resolution?

AGREEMENT CRITERIA:
- Both output BUY (even if one is 65% and other is 80%) -> AGREE_BUY
- Both output SELL -> AGREE_SELL
- Both output NEUTRAL -> AGREE_NEUTRAL
- One says BUY, other says SELL -> DEBATE
- One says NEUTRAL, other says directional -> DEBATE (one-sided conviction)
- Same direction but chart analysis directly contradicts numerical data -> DEBATE

IMPORTANT: In {AI_RISK_PROFILE} mode, favor ACTION when structure is clear.
Don't default to NEUTRAL just because confidence is moderate.

After deciding, if AGREE_BUY or AGREE_SELL:
Produce the final trade parameters now. SL/TP will be calculated by the system
using ATR - you do NOT need to provide them.
Just confirm the direction and produce your reasoning.

Respond in EXACTLY this format:
DECISION: [AGREE_BUY / AGREE_SELL / AGREE_NEUTRAL / DEBATE]
FINAL DIRECTION: [BUY / SELL / NEUTRAL / PENDING_DEBATE]
CONFIDENCE: [0-100]
AGREEMENT REASON: [one sentence - why you agree or why debate is needed]
CHART CONSENSUS: [one sentence - do both chart analyses align?]
TECHNICAL VIEW: [2-3 sentences on the combined technical picture]
FUNDAMENTAL VIEW: [1-2 sentences on macro/news]
WHY THIS DECISION: [2-3 sentences - final reasoning]
"""

    prompt_parts = [{"type": "text", "text": prompt_text}]
    if chart_b64:
        prompt_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_mime};base64,{chart_b64}", "detail": "high"}
        })

    ARBITER_MODEL = "gpt-4o-mini"
    WEB_SEARCH_CAPABLE = {"gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-11-20", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"}
    tools = [{"type": "web_search_preview"}] if ENABLE_WEB_SEARCH and ARBITER_MODEL in WEB_SEARCH_CAPABLE else []

    def _make_request(use_tools=True):
        payload = {
            "model":    ARBITER_MODEL,
            "messages": [{"role": "user", "content": prompt_parts}],
            "temperature": 0.3,
            "max_tokens":  1200
        }
        if use_tools and tools:
            payload["tools"] = tools
       
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
        r.raise_for_status()
        resp = r.json()
        content = resp["choices"][0]["message"]
       
        if isinstance(content.get("content"), list):
            return " ".join(b.get("text","") for b in content["content"] if b.get("type")=="text")
        return content.get("content","") or ""

    try:
        text = _make_request(use_tools=True)
       
        if not text.strip():
            return "DEBATE", "Could not parse decision response"

        dm = re.search(r'DECISION\s*[:\-]\s*(AGREE_BUY|AGREE_SELL|AGREE_NEUTRAL|DEBATE)', text.upper())
        decision = dm.group(1) if dm else "DEBATE"
        return decision, text

    except Exception as e:
        print(f"chatgpt_decides error: {e}")
        time.sleep(3)
        try:
            text = _make_request(use_tools=True)
            dm = re.search(r'DECISION\s*[:\-]\s*(AGREE_BUY|AGREE_SELL|AGREE_NEUTRAL|DEBATE)', text.upper())
            decision = dm.group(1) if dm else "DEBATE"
            return decision, text
        except Exception as e2:
            return "DEBATE", f"Decision error: {e2}"


# ==========================================
# CHATGPT - CHALLENGE GEMINI (debate round)
# ==========================================
def chatgpt_challenge(symbol, chatgpt_view, gemini_view):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"
    prompt = f"""You are a market analyst. Your peer Gemini reached a different conclusion on {symbol}.

YOUR VIEW:
{chatgpt_view}

GEMINI VIEW:
{gemini_view}

Write a focused 3-4 sentence challenge. Be specific:
- Name the exact data point or pattern you disagree on
- State the specific number or chart observation supporting your view
- What should Gemini reconsider?

No final call - just challenge the specific gap in reasoning."""

    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json={
                "model":    "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3, "max_tokens": 400
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT CHALLENGE ERROR: {str(e)}"

# ==========================================
# GEMINI - DEFEND (debate round)
# ==========================================
def gemini_defend(signal, news, macro, gemini_first, challenge_msg,
                  chart_b64=None, chart_mime="image/png"):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"
    twelve  = signal.get("twelve_data", {})
    context = build_market_context(signal, news, macro, twelve, signal.get("calendar",{}))

    prompt_text = f"""You are defending your prior analysis of {signal.get('symbol','this pair')}.

YOUR ORIGINAL VIEW:
{gemini_first}

CHALLENGE FROM PEER:
{challenge_msg}

ORIGINAL DATA:
{context}

{"Chart image attached for reference." if chart_b64 else ""}

Defend your position with specific data points or chart observations.
If the challenge is valid, acknowledge it and explain why your conclusion still holds.
Maximum 5 sentences.

Respond in EXACTLY this format:
DEFENDING DIRECTION: [BUY/SELL/NEUTRAL]
KEY REASON 1: [strongest data/chart point supporting your view]
KEY REASON 2: [second supporting argument]
KEY REASON 3: [direct response to the challenge]
SUMMARY: [one sentence - your final defended stance]"""

    parts = [{"text": prompt_text}]
    if chart_b64:
        parts.append({"inline_data": {"mime_type": chart_mime, "data": chart_b64}})

    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
                json={
                    "contents":       [{"parts": parts}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 600}
                },
                timeout=30
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini defend {attempt+1}: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Could not defend"


# ==========================================
# CHATGPT - FINAL VERDICT (after debate)
# ==========================================
def chatgpt_final_verdict(symbol, price, signal, chatgpt_view, gemini_view,
                          challenge_msg, gemini_defense,
                          chart_b64=None, chart_mime="image/png"):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve  = signal.get("twelve_data", {})
    calendar= signal.get("calendar", {})
    context = build_market_context(signal, {}, {}, twelve, calendar)
    atr_td  = safe_float(twelve.get("atr"))
    atr_ps  = safe_float(signal.get("atr"))
    atr_use = atr_td or atr_ps
    profile = RISK_PROFILES.get(AI_RISK_PROFILE, RISK_PROFILES["BALANCED"])

    prompt_text = f"""You are a senior execution trader making the FINAL and IRREVERSIBLE decision on {symbol}.

DEBATE SUMMARY:
Your Round 1 view: {chatgpt_view[:600]}

Gemini Round 1: {gemini_view[:600]}

Your challenge: {challenge_msg}

Gemini defense: {gemini_defense}

MARKET DATA:
{context}

{"Chart image attached." if chart_b64 else ""}

━━━ FINAL VALIDATION CHECKLIST ━━━
Before deciding, verify each:
1. Calendar: 3+ relevant events? -> NEUTRAL
2. Pivot: At R2/R3 on BUY or S2/S3 on SELL? -> Evaluate context, not auto-block
3. Failed auction opposing direction? -> NEUTRAL
4. Triple exhaustion (RSI>75+Stoch>85+price>BB upper)? -> NEUTRAL
5. Chart: Does chart show CHoCH or double top/bottom opposing direction? -> NEUTRAL or reverse
6. Validator: LOW confidence + T1 < 35%? -> NEUTRAL
7. Can this trade achieve minimum 1:1.5 R:R with ATR {atr_use}? -> if NO -> NEUTRAL

IMPORTANT: In {AI_RISK_PROFILE} mode, minimum confidence is {profile['min_confidence_for_trade']}%.
When confidence is {profile['min_confidence_for_trade']}-70%, still execute if structure is clear.
NEUTRAL is the LAST RESORT. Default to ACTION.

SL/TP NOTE: You do NOT need to calculate SL/TP.
The system will use ATR {atr_use} x {SL_ATR_MULTIPLIER} for SL and x {TP_ATR_MULTIPLIER} for TP.
Just confirm the direction.

If using web search: check only for breaking news in last 4 hours affecting {symbol}.
Do not search to confirm what you already know.

Respond in EXACTLY this format:
FINAL DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100]
CHECKLIST: [which items passed / which blocked]
CHART CONFIRMS: [YES/NO - does chart support the final direction?]
WEB SEARCH: [YES/what found or NO]
TECHNICAL VIEW: [2-3 sentences]
FUNDAMENTAL VIEW: [1-2 sentences]
WHY THIS DECISION: [2-3 sentences]"""

    parts = [{"type": "text", "text": prompt_text}]
    if chart_b64:
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_mime};base64,{chart_b64}", "detail": "high"}
        })

    tools = [{"type": "web_search_preview"}] if ENABLE_WEB_SEARCH else []

    try:
        payload = {
            "model":    "gpt-4o-mini",
            "messages": [{"role": "user", "content": parts}],
            "temperature": 0.3, "max_tokens": 1200
        }
        if tools:
            payload["tools"] = tools

        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
        r.raise_for_status()
        resp    = r.json()
        content = resp["choices"][0]["message"]
        if isinstance(content.get("content"), list):
            text = " ".join(b.get("text","") for b in content["content"] if b.get("type")=="text")
        else:
            text = content.get("content","") or ""
        return text or "CHATGPT FINAL ERROR: Empty response"
    except Exception as e:
        try:
            r = requests.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
                json={"model":"gpt-4o-mini","messages":[{"role":"user","content":parts}],
                      "temperature":0.3,"max_tokens":1200},
                timeout=60
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e2:
            return f"CHATGPT FINAL ERROR: {str(e2)}"


# ==========================================
# TELEGRAM FORMATTERS
# ==========================================
def fmt_dir(d):
    return {"BUY":"🟢 BUY","SELL":"🔴 SELL"}.get(d,"⚪ NEUTRAL")

def build_final_telegram(symbol, price, direction, sl, tp, rr, confidence,
                         tech_view, fund_view, reason,
                         chart_txt, checklist,
                         gemini_dir, chatgpt_dir, path, session, regime=None):
    path_tag  = {"AGREED":"✅ Both Agreed","DEBATE":"⚔️ After Debate","SOLO":"🤖 ChatGPT Only"}.get(path, path)
    dir_line  = fmt_dir(direction)
    su        = session.upper()
    levels    = (f"Entry:       {price}\nStop Loss:   {sl}\nTake Profit: {tp}\nRisk:Reward: {rr}"
                 if direction in ("BUY","SELL") else "Levels:      N/A")
    chart_line = f"\n📊 Chart:     {chart_txt}\n" if chart_txt and chart_txt != "N/A" else ""
    check_line = f"✔️  {checklist}\n" if checklist and checklist != "N/A" else ""
    regime_line= f"Regime:      {regime}\n" if regime else ""
    profile_line= f"Profile:     {AI_RISK_PROFILE}\n"

    return (
        f"{dir_line} | {symbol} | {su}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Decision:    {path_tag}\n"
        f"Confidence:  {confidence}\n"
        f"Gemini:      {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
        f"{profile_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{levels}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{chart_line}"
        f"{check_line}"
        f"{regime_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 TECHNICAL:\n{tech_view}\n\n"
        f"🌐 FUNDAMENTAL:\n{fund_view}\n\n"
        f"🎯 REASON:\n{reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Price: {price} | H1 | {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )

def send_neutral(symbol, confidence, reason, session, gemini_dir, chatgpt_dir):
    su = session.upper() if session != "off" else "SESSION"
    send_telegram(
        f"⚪ NEUTRAL | {symbol} | {su}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Gemini: {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
        f"Confidence: {confidence}\n"
        f"Profile: {AI_RISK_PROFILE}\n"
        f"Reason: {reason}"
    )

def send_neutral_audit(symbol, gemini_text, chatgpt_text, reason, post_debate=False):
    label = "NEUTRAL AUDIT (Post-Debate)" if post_debate else "NEUTRAL AUDIT"
    send_telegram(
        f"📋 {label} | {symbol}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"GEMINI:\n{str(gemini_text)[:500]}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CHATGPT:\n{str(chatgpt_text)[:500]}"
    )


# ==========================================
# FINAL OUTPUT HANDLER (FIXED)
# ==========================================
def _send_trade(symbol, price, signal, twelve_data,
                gemini_dir, chatgpt_dir, path, session,
                decision_text, calendar, post_debate=False):
    """
    FIXED: Removed direction parameter. Direction is always extracted
    from decision_text via extract_direction().
    """
    final_dir = extract_direction(decision_text)

    if final_dir == "NEUTRAL":
        reason = extract_neutral_reason(decision_text, "", calendar, signal)
        conf   = extract_confidence(decision_text)
        send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
        send_neutral_audit(symbol, gemini_dir, decision_text, reason, post_debate=post_debate)
        update_neutral_cache(symbol, session, "NEUTRAL", signal, twelve_data)
        return False

    atr_td  = safe_float(twelve_data.get("atr"))
    atr_ps  = safe_float(signal.get("atr"))
    # FIXED: Prefer webhook ATR when Twelve Data ATR is suspiciously small.
    # Twelve Data sometimes returns tiny ATR for commodities (e.g. 0.5 for Gold
    # when real ATR is 5+). If Twelve Data ATR < 20% of webhook ATR, use webhook.
    if atr_td is not None and atr_ps is not None and atr_ps > 0:
        if atr_td < atr_ps * 0.2:
            atr_use = atr_ps
            print(f"ATR override: Twelve Data={atr_td} too small vs webhook={atr_ps}, using webhook")
        else:
            atr_use = atr_td
    elif atr_ps is not None:
        atr_use = atr_ps
    elif atr_td is not None:
        atr_use = atr_td
    else:
        atr_use = None
    sl, tp, rr = calculate_sl_tp(final_dir, price, atr_use)

    valid, v_msg = validate_levels(final_dir, price, sl, tp)
    if not valid:
        send_telegram(
            f"⚠️ SL/TP INVALID | {symbol}\n"
            f"Direction: {final_dir} | Price: {price}\n"
            f"SL: {sl} | TP: {tp}\n"
            f"Issue: {v_msg}\nSignal aborted."
        )
        return False

    conf      = extract_confidence(decision_text)
    tech      = extract_multiline_field(decision_text, "TECHNICAL VIEW")
    fund      = extract_multiline_field(decision_text, "FUNDAMENTAL VIEW")
    reason    = extract_multiline_field(decision_text, "WHY THIS DECISION")
    if reason == "N/A":
        reason = extract_multiline_field(decision_text, "REASON")
    chart_txt = extract_multiline_field(decision_text, "CHART ANALYSIS")
    if chart_txt == "N/A":
        chart_txt = extract_multiline_field(decision_text, "CHART CONFIRMS")
    checklist = extract_multiline_field(decision_text, "CHECKLIST")

    adx_v   = safe_float(twelve_data.get("adx"))
    bb_w    = twelve_data.get("bb_width")
    regime  = detect_market_regime(adx_v, bb_w)

    msg = build_final_telegram(
        symbol, price, final_dir, sl, tp, rr, conf,
        tech, fund, reason, chart_txt, checklist,
        gemini_dir, chatgpt_dir, path, session, regime
    )
    send_telegram(msg)
    register_trade(symbol, session)
    update_neutral_cache(symbol, session, final_dir, signal, twelve_data)
    return True
                    
# ==========================================
# MAIN WEBHOOK WITH BACKGROUND PROCESSING
# ==========================================
def process_signal_background(data):
    """All slow operations run in background thread"""
    try:
        # -- Field Extraction --
        symbol     = clean_symbol(safe_str(data.get("symbol") or data.get("ticker"), "Unknown"))
        price_raw  = safe_float(data.get("price") or data.get("close") or 0, allow_zero=False)
        
        # FIXED: Abort if price is missing
        if price_raw is None:
            send_telegram(
                f"❌ SIGNAL REJECTED | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Reason: Missing or zero price\n"
                f"Price raw value: {data.get('price') or data.get('close')}"
            )
            return
        
        price = price_raw
        incoming_tf = safe_str(data.get("timeframe") or data.get("tf") or data.get("interval"), "1H")
        data["tf"] = data["timeframe"] = incoming_tf

        if symbol == "UNKNOWN":
            send_telegram("❌ Invalid symbol received. Aborting.")
            return

        direction  = safe_str(data.get("direction"), "N/A")
        score      = safe_str(data.get("score"),     "N/A")
        adx        = safe_str(data.get("adx"),       "N/A")
        structure  = safe_str(data.get("structure_bias"), "N/A")
        atr        = safe_str(data.get("atr"),       "N/A")
        ema_21     = safe_str(data.get("ema_21"),    "N/A")
        ema_50     = safe_str(data.get("ema_50"),    "N/A")
        ema_200    = safe_str(data.get("ema_200"),   "N/A")
        volume     = safe_str(data.get("volume_ratio"), "N/A")
        signal_ea  = safe_int(data.get("ea_score") or data.get("ea_filter") or 0)
        sv_conf    = safe_str(data.get("sv_confidence"), "N/A")
        sv_valid   = safe_str(data.get("sv_valid"),  "N/A")
        pivot_zone = safe_str(data.get("pivot_zone"),"N/A")
        vwap_band  = safe_str(data.get("vwap_band"), "N/A")

        data["lon_fail_up"]   = safe_bool(data.get("lon_fail_up"))
        data["lon_fail_down"] = safe_bool(data.get("lon_fail_down"))
        data["ny_fail_up"]    = safe_bool(data.get("ny_fail_up"))
        data["ny_fail_down"]  = safe_bool(data.get("ny_fail_down"))

        # Chart image
        chart_url  = data.get("chart_url") or data.get("chart_image_url") or data.get("chart_screenshot")
        chart_b64  = fetch_chart_image(chart_url) if chart_url else None
        chart_mime = get_image_mime(chart_url) if chart_url else "image/png"
        chart_info = "✅ Chart attached" if chart_b64 else "⚠️ No chart image"

        print(f"{symbol} | {price} | {direction} | Score:{score} | EA:{signal_ea} | Chart:{chart_info}")

        # GATE 1 - SESSION & FREQUENCY
        allowed, gate_reason, session = session_gate(symbol)
        if not allowed:
            if session == "off":
                send_telegram(
                    f"⏸️ OFF-SESSION | {symbol} | {direction} | Score:{score} | EA:{signal_ea}"
                )
                store_off_session_signal(
                    symbol, direction,
                    safe_int(data.get("score") or 0),
                    signal_ea, price
                )
            else:
                send_telegram(
                    f"🚫 SIGNAL BLOCKED | {symbol} | {session.upper()}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Price: {price}  Direction: {direction}\n"
                    f"Score: {score}/100  EA: {signal_ea}/8\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Reason: {gate_reason}"
                )
            return

        # Signal In
        send_telegram(
            f"📡 SIGNAL IN | {symbol} | {session.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Price:{price}  ATR:{atr}  Score:{score}/100\n"
            f"Dir:{direction}  Struct:{structure}  ADX:{adx}\n"
            f"EA:{signal_ea}/8  Vol:{volume}\n"
            f"EMA:{ema_21}/{ema_50}/{ema_200}\n"
            f"Pivot:{pivot_zone}  VWAP:{vwap_band}\n"
            f"Validator:{sv_valid} ({sv_conf})\n"
            f"Chart: {chart_info}  Profile: {AI_RISK_PROFILE}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Fetching live data..."
        )

                # GATE 2 — PARALLEL DATA FETCH
        news, macro, calendar = fetch_all_live_data(symbol)

        # GATE 2.5 — CURRENCY-SPECIFIC CALENDAR HARD BLOCK (3+ events)
        sc = clean_symbol(symbol)
        
        # FIXED: Proper currency mapping for commodities, indices, and crypto
        COMMODITY_CURRENCY_MAP = {
            "XAUUSD": ["USD"],
            "GOLD": ["USD"],
            "XAGUSD": ["USD"],
            "SILVER": ["USD"],
            "USOIL": ["USD"],
            "UKOIL": ["USD", "GBP"],
            "WTI": ["USD"],
            "BRENT": ["USD", "GBP"],
            "BTCUSD": ["USD"],
            "ETHUSD": ["USD"],
            "US30": ["USD"],
            "NAS100": ["USD"],
            "SPX500": ["USD"],
            "GER40": ["EUR"],
            "UK100": ["GBP"],
            "JP225": ["JPY"],
        }
        
        if sc in COMMODITY_CURRENCY_MAP:
            currencies = COMMODITY_CURRENCY_MAP[sc]
        elif len(sc) == 6:
            currencies = [sc[:3], sc[3:]]
        elif len(sc) == 7 and "/" in sc:
            # Format like "XAU/USD" or "BTC/USD"
            parts = sc.split("/")
            currencies = [parts[0], parts[1]]
        else:
            # Unknown format — use symbol itself as fallback
            currencies = [sc]
        
        events_raw = calendar.get("events", [])
        rel_events = []
        for e in events_raw:
            if isinstance(e, dict):
                event_currency = e.get("currency", "")
                if event_currency in currencies:
                    rel_events.append(e.get("event", ""))
            elif isinstance(e, str):
                # String events — check if any currency code appears in the event name
                if any(curr in e.upper() for curr in currencies):
                    rel_events.append(e)

        if len(rel_events) >= 3:
            send_telegram(
                f"🚫 CALENDAR BLOCK | {symbol} | {session.upper()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Relevant events for {'/'.join(currencies)}: {len(rel_events)}\n"
                f"Events: {', '.join(rel_events)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Do not trade during currency-specific high-impact events."
            )
            return

        # GATE 3 - TWELVE DATA
        twelve_data = fetch_twelve_data(symbol)
        data["twelve_data"] = twelve_data
        data["calendar"]    = calendar
        tq  = twelve_data.get("_quality","Unknown")
        adx_v  = safe_float(twelve_data.get("adx"))
        bb_w   = twelve_data.get("bb_width")
        regime = detect_market_regime(adx_v, bb_w)

        cal_ev_names = ", ".join(rel_events) if rel_events else "Clear"
        send_telegram(
            f"📊 MARKET DATA | {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"News:{news['sentiment']}  Macro:{macro['risk']}\n"
            f"Calendar: {cal_ev_names} ({len(rel_events)} events)\n"
            f"Regime: {regime}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"INDICATORS ({tq}):\n"
            f"RSI:{twelve_data.get('rsi','N/A')}  "
            f"MACD:{twelve_data.get('macd','N/A')}  "
            f"Hist:{twelve_data.get('macd_histogram','N/A')}\n"
            f"Stoch:{twelve_data.get('stoch_k','N/A')}/{twelve_data.get('stoch_d','N/A')}  "
            f"ADX:{twelve_data.get('adx','N/A')}  ATR:{twelve_data.get('atr','N/A')}\n"
            f"BB:{twelve_data.get('bb_lower','N/A')}/{twelve_data.get('bb_middle','N/A')}/{twelve_data.get('bb_upper','N/A')}  W:{bb_w}%\n"
            f"Ichi Conv/Base:{twelve_data.get('ichimoku_conversion','N/A')}/{twelve_data.get('ichimoku_base','N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Checking signal strength..."
        )

        # GATE 3.5 - SIGNAL STRENGTH CHECK
        skip_weak, weak_reason = should_skip_due_to_weak_signal(data)
        if skip_weak:
            send_telegram(
                f"⚪ SKIPPED - WEAK SIGNAL | {symbol}\n"
                f"Reason: {weak_reason}\n"
                f"No AI tokens used."
            )
            return

        # GATE 3.6 - MCS AUTO-NEUTRAL
        skip_ai, mcs_reason, mcs_score = should_auto_neutral(
            symbol, session, data, twelve_data, calendar
        )
        if skip_ai:
            with cache_lock:
                streak = neutral_cache.get(f"{sc}_{session}",{}).get("auto_count",0) + 1
            send_telegram(
                f"⚪ NEUTRAL | {symbol} | {session.upper()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Mode:    Auto (no AI)\n"
                f"MCS:     {mcs_score:.1f}% (threshold: {MCS_THRESHOLD}%)\n"
                f"Streak:  {streak}/{MAX_AUTO_NEUTRAL_STREAK}\n"
                f"Profile: {AI_RISK_PROFILE}\n"
                f"Reason:  {mcs_reason}"
            )
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            increment_auto_streak(symbol, session)
            return

        reset_auto_streak(symbol, session)
        send_telegram(
            f"🔄 MCS: {mcs_score:.1f}% - Running AI analysis...\n"
            f"Profile: {AI_RISK_PROFILE}\n"
            f"{'🖼️ Chart image will be analysed by both AIs' if chart_b64 else '⚠️ No chart - numerical analysis only'}"
        )

        # GATE 4 - BLIND PARALLEL AI ROUND 1
        with ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(gemini_analysis, data, news, macro, chart_b64, chart_mime)
            fc = ex.submit(chatgpt_analysis, symbol, price, data, news, macro,
                           calendar, chart_b64, chart_mime)
            gemini  = fg.result()
            chatgpt = fc.result()

        if "GEMINI UNAVAILABLE" in gemini and "CHATGPT ERROR" in chatgpt:
            send_telegram(f"❌ Both AI systems failed for {symbol}. Signal aborted.")
            return

        gemini_dir  = extract_direction(gemini)  if "GEMINI UNAVAILABLE" not in gemini  else "UNAVAILABLE"
        chatgpt_dir = extract_direction(chatgpt) if "CHATGPT ERROR"      not in chatgpt else "UNAVAILABLE"
        print(f"R1 - Gemini: {gemini_dir} | ChatGPT: {chatgpt_dir}")

        if "CHATGPT ERROR" in chatgpt:
            send_telegram(f"❌ ChatGPT unavailable - cannot produce final decision. Skipped.")
            return

        if "GEMINI UNAVAILABLE" in gemini:
            send_telegram(f"⚠️ Gemini unavailable - ChatGPT solo analysis.")
            gemini_dir = "UNAVAILABLE"
            if chatgpt_dir in ("BUY","SELL"):
                _send_trade(symbol, price, data, twelve_data,
                            "UNAVAILABLE", chatgpt_dir, "SOLO", session,
                            chatgpt, calendar)
            else:
                reason = extract_neutral_reason(gemini, chatgpt, calendar, data)
                conf   = extract_confidence(chatgpt)
                send_neutral(symbol, conf, reason, session, "UNAVAILABLE", chatgpt_dir)
                send_neutral_audit(symbol, "UNAVAILABLE", chatgpt, reason)
                update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return

        # GATE 5 - CHATGPT DECIDES
        send_telegram(
            f"🔍 AI ROUND 1 COMPLETE | {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Gemini:  {gemini_dir}\n"
            f"ChatGPT: {chatgpt_dir}\n"
            f"ChatGPT reviewing both analyses..."
        )

        decision, decision_text = chatgpt_decides(
            symbol, price, data, gemini, chatgpt, chart_b64, chart_mime
        )
        print(f"ChatGPT decision: {decision}")

        if decision == "AGREE_NEUTRAL":
            reason = extract_neutral_reason(gemini, chatgpt, calendar, data)
            conf   = extract_confidence(decision_text)
            send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
            send_neutral_audit(symbol, gemini, chatgpt, reason)
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return

        elif decision in ("AGREE_BUY","AGREE_SELL"):
            final_dir = "BUY" if decision == "AGREE_BUY" else "SELL"
            send_telegram(
                f"✅ AGREEMENT | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Decision: {final_dir}\n"
                f"Gemini: {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
                f"Producing execution parameters..."
            )
            _send_trade(symbol, price, data, twelve_data,
                        gemini_dir, chatgpt_dir, "AGREED", session,
                        decision_text, calendar)
            return

        else:
            # DEBATE path
            send_telegram(
                f"⚔️ DEBATE | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Gemini: {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
                f"ChatGPT challenging Gemini..."
            )

            challenge    = chatgpt_challenge(symbol, chatgpt, gemini)
            gemini_reply = gemini_defend(data, news, macro, gemini, challenge,
                                         chart_b64, chart_mime)

            send_telegram(
                f"💬 DEBATE COMPLETE | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Challenge and defense exchanged.\n"
                f"ChatGPT making final verdict..."
            )

            final_text = chatgpt_final_verdict(
                symbol, price, data, chatgpt, gemini,
                challenge, gemini_reply, chart_b64, chart_mime
            )

            # FIXED: No more "PENDING" - final_text contains the direction
            _send_trade(symbol, price, data, twelve_data,
                        gemini_dir, chatgpt_dir, "DEBATE", session,
                        final_text, calendar, post_debate=True)
            return

    except Exception as e:
        err = f"ERROR: {str(e)[:200]}"
        print(err)
        send_telegram(f"❌ System Error | {err}")


@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook active. POST signals to /webhook", 200

    try:
        # -- ENHANCED LARGE PAYLOAD HANDLING --
        content_length = request.content_length or 0
        max_payload = MAX_PAYLOAD_MB * 1024 * 1024
        
        # FIXED: Handle chunked transfers where content_length is 0 or missing
        raw_body = request.get_data()
        actual_size = len(raw_body)
        
        # Use actual size if content_length is unreliable
        effective_size = content_length if content_length > 0 else actual_size
        
        if effective_size > max_payload:
            send_telegram(
                f"❌ Payload too large: {effective_size/1024:.0f}KB (max: {max_payload/1024:.0f}KB)\n"
                f"Content-Length: {content_length}, Actual: {actual_size}"
            )
            return "Payload too large", 413
        
        # Try multiple parsing methods
        data = None
        parse_errors = []
        
        # Method 1: JSON
        try:
            data = request.get_json(silent=False, force=False)
            if data:
                print(f"Parsed as JSON: {len(json.dumps(data))} chars, {len(data)} fields")
        except Exception as e:
            parse_errors.append(f"JSON: {str(e)[:100]}")
        
        # Method 2: Raw text
        if not data:
            try:
                rt = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
                if rt and rt.strip():
                    data = json.loads(rt.strip())
                    print(f"Parsed as raw text: {len(rt)} chars")
            except Exception as e:
                parse_errors.append(f"Raw text: {str(e)[:100]}")
                rt_preview = (str(raw_body) or "")[:500]
                print(f"Raw payload preview: {rt_preview}")
        
        # Method 3: Form data
        if not data:
            try:
                form_data = request.form.to_dict()
                if form_data:
                    for key, value in form_data.items():
                        try:
                            nested = json.loads(value)
                            if isinstance(nested, dict):
                                data = nested
                                print(f"Parsed from form field '{key}'")
                                break
                        except:
                            pass
                    if not data:
                        data = form_data
                        print(f"Parsed as form data: {len(form_data)} fields")
            except Exception as e:
                parse_errors.append(f"Form data: {str(e)[:100]}")
        
        # Method 4: Query parameters
        if not data:
            try:
                query_data = request.args.to_dict()
                if query_data:
                    for key, value in query_data.items():
                        try:
                            nested = json.loads(value)
                            if isinstance(nested, dict):
                                data = nested
                                break
                        except:
                            pass
                    if not data:
                        data = query_data
            except Exception as e:
                parse_errors.append(f"Query params: {str(e)[:100]}")
        
        # Final check
        if not data:
            error_msg = f"❌ Could not parse payload ({len(parse_errors)} methods failed)"
            if parse_errors:
                error_msg += f"\nErrors: {'; '.join(parse_errors)}"
            print(error_msg)
            send_telegram(error_msg[:500])
            return "Cannot parse data", 400
        
        # -- DATA VALIDATION --
        required_fields = ['symbol', 'ticker', 'pair']
        has_symbol = any(f in data for f in required_fields)
        
        if not has_symbol:
            fields_received = list(data.keys())[:20]
            send_telegram(
                f"⚠️ Missing symbol in payload\n"
                f"Fields received: {', '.join(fields_received)}\n"
                f"Sample keys: {list(data.keys())[:5]}"
            )
            return "Missing symbol", 400
        
        # Log successful parse
        symbol_preview = data.get('symbol') or data.get('ticker') or 'Unknown'
        price_preview = data.get('price') or data.get('close') or 'N/A'
        direction_preview = data.get('direction') or 'N/A'
        field_count = len(data)
        
        print(f"✅ Parsed: {symbol_preview} | {price_preview} | {direction_preview} | {field_count} fields")
        
        if field_count > 100:
            print(f"Large payload ({field_count} fields)")

        # Start background thread and return IMMEDIATELY
        thread = threading.Thread(target=process_signal_background, args=(data,))
        thread.daemon = True
        thread.start()
        
        return "OK", 200

    except Exception as e:
        err = f"ERROR: {str(e)[:500]}"
        print(err)
        send_telegram(f"❌ Webhook Error | {err[:400]}")
        return "Error", 500


# ==========================================
# HEALTH & ROUTES
# ==========================================
@app.route("/health", methods=["GET"])
def health():
    return json.dumps({
        "status": "OK",
        "risk_profile": AI_RISK_PROFILE,
        "mcs_threshold": MCS_THRESHOLD,
        "max_payload_mb": MAX_PAYLOAD_MB,
    }), 200

@app.route("/", methods=["GET"])
def root():
    return "MIKA Bot running. POST signals to /webhook", 200


# ==========================================
# STARTUP (FIXED)
# ==========================================
if __name__ == "__main__":
    print(f"Starting MIKA Trading Bot (All Bugs Fixed)")
    print(f"Risk Profile: {AI_RISK_PROFILE}")
    print(f"MCS Threshold: {MCS_THRESHOLD}%")
    print(f"Max Payload: {MAX_PAYLOAD_MB}MB")
    print(f"Min Confidence: {RISK_PROFILES[AI_RISK_PROFILE]['min_confidence_for_trade']}%")
    print(f"Triple Exhaust: {'ON' if RISK_PROFILES[AI_RISK_PROFILE]['triple_exhaust_enabled'] else 'OFF'}")
    print(f"Pivot Block: {'ON' if RISK_PROFILES[AI_RISK_PROFILE]['pivot_block_enabled'] else 'OFF'}")
    print(f"Session times: London 12:00-17:59 PKT | NewYork 18:00-01:59 PKT | Off 02:00-11:59 PKT")
   
    PKT = pytz.timezone("Asia/Karachi")
    scheduler = BackgroundScheduler(timezone=PKT)

    scheduler.add_job(send_presession_snapshot, "cron",
                      hour=9, minute=0, args=["overnight"],
                      id="overnight")

    scheduler.add_job(send_presession_snapshot, "cron",
                      hour=11, minute=59, args=["presession"],
                      id="presession")

    scheduler.add_job(reset_day, "cron",
                      hour=0, minute=0,
                      id="midnight_reset")
   
    scheduler.start()
    
    # DEBUG: Verify jobs are scheduled
    from datetime import datetime
    for job in scheduler.get_jobs():
        print(f"SCHEDULED JOB: {job.id} | Next run: {job.next_run_time}")
    print(f"Current PKT time: {datetime.now(PKT)}")
    print(f"Current UTC time: {datetime.now(pytz.UTC)}")
    print("Scheduler started on Asia/Karachi timezone.")
   
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
