"""
MIKA TRADING BOT — FINAL VERSION
=================================
Architecture:
  Gate 1   → Session & frequency check
  Gate 2   → Parallel data fetch (Finnhub + GDELT + Calendar)
  Gate 2.5 → Currency-specific calendar hard block
  Gate 3   → Twelve Data fetch (7 core indicators)
  Gate 3.5 → MCS auto-neutral filter
  Gate 4   → Blind parallel AI Round 1 (Gemini + ChatGPT)
             Both receive: data context + chart image (vision)
             Both independently analyse: patterns, BOS, CHoCH,
             structure, indicators, pivots, auction, news
  Gate 5   → ChatGPT decides: agree / debate / neutral
  Gate 6   → If debate: challenge → defend → final verdict
  Gate 7   → ATR-based SL/TP calculation (Python, not AI)
  Gate 8   → SL/TP sanity validation

Chart Vision:
  Pine Script includes chart_url in alert JSON.
  Bot fetches, converts to base64, sends to both AIs.
  AIs identify: BOS, CHoCH, double top/bottom, wedges,
  trend lines, rejection wicks, liquidity grabs, etc.
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
OFF_MIN_SIGNALS          = 3
OFF_CONSISTENCY_PCT      = 0.80
SL_ATR_MULTIPLIER        = float(os.environ.get("SL_ATR_MULTIPLIER",        "1.5"))
TP_ATR_MULTIPLIER        = float(os.environ.get("TP_ATR_MULTIPLIER",        "2.5"))
MIN_SL_DISTANCE_PCT      = 0.002   # 0.2% minimum SL distance


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
    prefixes = ["FX:","OANDA:","BINANCE:","PYTH:","TVC:",
                "CAPITALCOM:","PEPPERSTONE:","ICMARKETS:","FXCM:","FOREX:"]
    s = symbol.upper().strip()
    for p in prefixes:
        s = s.replace(p, "")
    return s.strip()

def parse_incoming_data(rj, rt):
    if rj and isinstance(rj, dict):
        return rj
    if rt:
        try:
            return json.loads(rt.strip())
        except Exception:
            pass
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
    pattern = rf'{re.escape(label)}\s*[:\-]\s*(.*?)(?=\n[A-Z][A-Z ]*\s*[:\-]|\Z)'
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
    SL = ATR × 1.5, TP = ATR × 2.5 → gives 1:1.67 minimum R:R
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
    try:
        sl_f = float(sl); tp_f = float(tp); pr_f = float(price)
        if direction == "BUY":
            return sl_f < pr_f and tp_f > pr_f, f"BUY: SL{sl_f}<{pr_f}<TP{tp_f}"
        if direction == "SELL":
            return sl_f > pr_f and tp_f < pr_f, f"SELL: TP{tp_f}<{pr_f}<SL{sl_f}"
    except Exception:
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
# CHART IMAGE FETCHER
# ==========================================
def fetch_chart_image(chart_url):
    """
    Fetches chart image from URL and converts to base64.
    Supports: TradingView published chart URLs, direct image URLs.
    Returns base64 string or None if failed.
    """
    if not chart_url or not isinstance(chart_url, str):
        return None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)",
            "Accept":     "image/png,image/jpeg,image/*"
        }
        r = requests.get(chart_url.strip(), headers=headers, timeout=15)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "image" not in content_type and "octet-stream" not in content_type:
            print(f"Chart URL returned non-image content: {content_type}")
            return None
        b64 = base64.b64encode(r.content).decode("utf-8")
        print(f"Chart image fetched: {len(r.content)} bytes from {chart_url[:60]}")
        return b64
    except Exception as e:
        print(f"Chart fetch error: {e}")
        return None


def get_image_mime(chart_url):
    """Detect mime type from URL extension."""
    url_lower = (chart_url or "").lower()
    if ".jpg" in url_lower or ".jpeg" in url_lower:
        return "image/jpeg"
    if ".webp" in url_lower:
        return "image/webp"
    return "image/png"


# ==========================================
# MCS ENGINE
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
    components = [
        ("price",          0.25, 0.30),
        ("macd_histogram", 0.20, 50.0),
        ("rsi",            0.20, 10.0),
        ("score",          0.15, 15.0),
        ("stoch_k",        0.10, 20.0),
        ("volume_ratio",   0.10, 30.0),
    ]
    mcs = 0.0
    for field, w, scale in components:
        ov = prev.get(field); nv = curr.get(field)
        if ov is None or nv is None:
            continue
        mcs += _normalize(_pct_change(ov, nv), scale) * w
    return round(mcs, 2)

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
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        cached = neutral_cache.get(key)
    if cached is None:
        return False, "No cache", 0.0
    if cached.get("last_outcome") in ("BUY","SELL"):
        return False, "Last outcome was trade", 0.0
    last_ts = cached.get("last_timestamp")
    if last_ts:
        age = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        if age > CACHE_EXPIRY_MINUTES:
            return False, f"Cache expired ({age:.0f}m)", 0.0
    if calendar.get("high_impact_soon"):
        return False, "High-impact event", 0.0
    prev = cached.get("last_signal", {})
    curr = build_signal_snapshot(data, twelve)
    pv   = prev.get("price"); cv = curr.get("price")
    if pv and cv and _pct_change(pv, cv) >= PRICE_VETO_PCT:
        return False, f"Price moved >{PRICE_VETO_PCT}%", 0.0
    if cached.get("auto_count", 0) >= MAX_AUTO_NEUTRAL_STREAK:
        return False, "Max streak reached", 0.0
    mcs = compute_mcs(prev, curr)
    if mcs >= MCS_THRESHOLD:
        return False, f"MCS {mcs:.1f}% >= {MCS_THRESHOLD}%", mcs
    return True, f"MCS {mcs:.1f}% < {MCS_THRESHOLD}%", mcs

def update_neutral_cache(symbol, session, outcome, data, twelve):
    key  = f"{clean_symbol(symbol)}_{session}"
    snap = build_signal_snapshot(data, twelve)
    with cache_lock:
        old        = neutral_cache.get(key, {})
        auto_count = old.get("auto_count", 0) if outcome == "NEUTRAL" else 0
        neutral_cache[key] = {
            "last_outcome":   outcome,
            "last_signal":    snap,
            "last_timestamp": datetime.now(timezone.utc),
            "auto_count":     auto_count,
        }

def increment_auto_streak(symbol, session):
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        if key in neutral_cache:
            neutral_cache[key]["auto_count"] += 1

def reset_auto_streak(symbol, session):
    key = f"{clean_symbol(symbol)}_{session}"
    with cache_lock:
        if key in neutral_cache:
            neutral_cache[key]["auto_count"] = 0


# ==========================================
# OFF-SESSION BUFFER
# ==========================================
def store_off_session_signal(symbol, direction, score, ea, price):
    key = clean_symbol(symbol)
    now = datetime.now(timezone.utc)
    with buffer_lock:
        if key not in off_session_buffer:
            off_session_buffer[key] = {"signals": [], "last_alert_sent": None}
        off_session_buffer[key]["signals"].append({
            "direction": direction.upper().replace("LONG","BUY").replace("SHORT","SELL"),
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
    now_pkt  = datetime.now(timezone.utc) + timedelta(hours=5)
    time_str = now_pkt.strftime("%I:%M %p PKT")
    title    = "🌙 OVERNIGHT WATCH" if mode == "overnight" else "🌅 PRE-SESSION BRIEF"
    period   = "02:00 – 09:00 PKT" if mode == "overnight" else "02:00 – 11:59 PKT"

    # FIX: fetch calendar OUTSIDE the lock — network calls must never hold locks
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
            send_telegram(txt)
            if mode == "presession":
                off_session_buffer.clear()
            return

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
                blocks.append(f"⚪ {key:<8} — MIXED  {buys}B/{sells}S  Score avg {avg_s}\n")
                continue

            if mode == "overnight":
                blocks.append(f"{emoji} {key:<8} {dom}  {bar}  {pct_s}\n"
                               f"   {tot_s} signals | Score {avg_s} avg | EA {ea_a}\n"
                               f"   {prices[0]:.5f} → {prices[-1]:.5f}  ({ds})\n")
            else:
                blocks.append(f"{emoji} {key}  —  {dom}  {bar}  {pct_s}\n"
                               f"   {tot_s} signals  |  {dom_c} {dom}  {tot_s-dom_c} {'SELL' if dom=='BUY' else 'BUY'}\n"
                               f"   Score: {min(scrs)}–{max(scrs)} (avg {avg_s})  EA avg {ea_a}\n"
                               f"   {prices[0]:.5f} → {prices[-1]:.5f}  ({ds})  [{conv}]\n")
                if conv in ("STRONG","MODERATE") and cons >= 0.80:
                    watch.append(f"   {emoji} {key} {dom} — {conv.lower()} overnight build")

        body = "\n".join(blocks)

        if mode == "overnight":
            footer = "\n━━━━━━━━━━━━━━━━━━━━━━━━━\n⏰ Next update: 11:59 AM (London open)"
        else:
            watch_txt = ""
            if watch:
                watch_txt = "\n━━━━━━━━━━━━━━━━━━━━━━━━━\n📌 WATCH AT OPEN:\n" + "\n".join(watch) + "\n"
            # cal_txt already fetched outside the lock above
            footer = (watch_txt + cal_txt +
                      "\n━━━━━━━━━━━━━━━━━━━━━━━━━\nGood luck. Session starting now.\nBuffer cleared.")
            off_session_buffer.clear()

    send_telegram(header + body + footer)


# ==========================================
# DAILY RESET
# ==========================================
def reset_day():
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
# SESSION GATE
# ==========================================
def get_session_by_time():
    now_utc = datetime.now(timezone.utc)
    pk_hour = (now_utc.hour + 5) % 24
    if 12 <= pk_hour < 18:
        return "london"
    elif 18 <= pk_hour or pk_hour <= 2:   # fixed: <= 2 includes 02:00
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
                return False, f"Cooldown active — {int(7200-diff)}s remaining", session
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
        return {"high_impact_soon": bool(high), "events": high}
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
# TWELVE DATA — 7 CORE INDICATORS
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
    try:
        upper  = float(parsed["bb_upper"])
        lower  = float(parsed["bb_lower"])
        middle = float(parsed["bb_middle"])
        parsed["bb_width"] = round((upper - lower)/middle * 100, 2) if middle > 0 else None
    except Exception:
        parsed["bb_width"] = None

    missing        = [k for k,v in parsed.items() if v == "N/A"]
    tot            = len(parsed)
    parsed["_quality"] = (
        f"FULL ({tot}/{tot})"        if not missing         else
        f"GOOD ({tot-len(missing)}/{tot})"  if len(missing) <= 3 else
        f"PARTIAL ({tot-len(missing)}/{tot})"
    )
    return parsed


# ==========================================
# MARKET CONTEXT (text — injected into all prompts)
# ==========================================
def build_market_context(signal, news, macro, twelve, calendar):
    s   = {k:v for k,v in signal.items() if k not in ["twelve_data","calendar","tf","timeframe"]}
    sym = s.get("symbol","N/A")

    # Currency-specific calendar events
    currencies     = [clean_symbol(sym)[:3], clean_symbol(sym)[3:]] if len(clean_symbol(sym)) == 6 else [sym]
    events_raw     = calendar.get("events", [])
    relevant_ev    = []
    for e in events_raw:
        if isinstance(e, dict) and e.get("currency","") in currencies:
            relevant_ev.append(e.get("event",""))
        elif isinstance(e, str):
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
        warnings.append("🚨 FAILED BULLISH AUCTION — market rejected higher prices")
    if (lon_fd or ny_fd):
        warnings.append("🚨 FAILED BEARISH AUCTION — market rejected lower prices")
    if ("EXTREME_HIGH" in vwap_band and "LONG" in direction.upper()):
        warnings.append("🚨 VWAP EXTREME HIGH — mean reversion risk on BUY")
    if ("EXTREME_LOW" in vwap_band and "SHORT" in direction.upper()):
        warnings.append("🚨 VWAP EXTREME LOW — mean reversion risk on SELL")
    if sv_conf == "LOW":
        warnings.append(f"🚨 VALIDATOR LOW CONFIDENCE — T1 hit rate: {sv_t1}%")

    warn_block = ("\n".join(warnings) + "\n") if warnings else ""

    context = f"""
MARKET BRIEF — {sym} | H1 TIMEFRAME
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
# ROUND 1 ANALYSIS PROMPT (shared by both AIs)
# ==========================================
ROUND1_RULES = """
━━━ ANALYSIS FRAMEWORK — FOLLOW EVERY STEP IN ORDER ━━━

STEP 1 — HARD BLOCKS
Check each condition first. If any triggers → output NEUTRAL immediately, skip remaining steps.

  A) CALENDAR      2+ relevant high-impact events for this pair's currencies → NEUTRAL
  B) PIVOT EXTREME ABOVE_R2 or ABOVE_R3 zone + BUY signal → NEUTRAL
                   BELOW_S2 or BELOW_S3 zone + SELL signal → NEUTRAL
  C) FAILED AUCTION Failed bullish auction active + BUY signal → NEUTRAL or SELL
                    Failed bearish auction active + SELL signal → NEUTRAL or BUY
  D) VALIDATOR     sv_confidence = LOW AND sv_t1_rate < 35% → NEUTRAL
  E) TRIPLE EXHAUST RSI > 70 AND Stochastic K > 80 AND price above BB upper → NEUTRAL
  F) LOW VOLUME    volume_ratio < 0.6 → NEUTRAL (no institutional participation)
  G) MACRO+RANGING GDELT macro = HIGH AND ADX < 20 → NEUTRAL (noise, not signal)

STEP 2 — CONFLICT RESOLUTION HIERARCHY
When indicators disagree, resolve using this priority order (top = highest):

  1st  STRUCTURE   HH/HL patterns + swing highs/lows — always wins
  2nd  PILLARS     4 or more of 5 pillars aligned = override oscillators
  3rd  ADX + DI   ADX > 25 with DI+ > DI- (bull) or DI- > DI+ (bear) = trend confirmed
  4th  AUCTION    Failed auction signal overrides momentum readings
  5th  OSCILLATORS RSI, Stoch, CCI — these are lagging, lowest weight

  RULE: RSI = 72 (overbought) BUT structure = HH/HL + 4/5 pillars bullish
        → IGNORE RSI. Output BUY. Structure and pillars override oscillators.

STEP 3 — MOMENTUM SUSTAINABILITY
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

STEP 4 — CHART ANALYSIS (when chart image is provided)
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
    Price relative to VWAP — extended or healthy?
    Pivot levels visible on chart vs current price

  CHART vs DATA CONFLICTS:
    If chart shows CHoCH but Pine Script says BULL structure → flag the conflict explicitly
    If chart confirms BOS in signal direction → mention as additional confirmation
    If chart shows major pattern (double top, H&S) opposing the signal → lean NEUTRAL

STEP 5 — FUNDAMENTAL CHECK
  Does news sentiment support or oppose the direction?
  What is the central bank stance for this pair?
  Does global macro risk (GDELT) raise concern?
  Fundamental is SUPPORTING context only — cannot override confirmed technical structure.

DIRECTION DECISION RULES:
  Technical structure is PRIMARY
  NEUTRAL when: any hard block fires, OR momentum exhausted,
  OR chart shows major reversal pattern opposing the signal
  BUY/SELL when: no hard blocks, structure confirmed, momentum sustainable,
  chart aligned or neutral, fundamentals not strongly opposed
  This is DIRECTION ASSESSMENT ONLY — do NOT provide entry price, SL, or TP

REQUIRED OUTPUT FORMAT — respond in EXACTLY this format, no extra text:
DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100]
HARD BLOCK: [NONE — or state which block A-G triggered and the specific data that triggered it]
CHART ANALYSIS: [Describe exactly what you see: BOS/CHoCH, patterns, wicks, EMA position, key levels. If no chart: state "No chart provided"]
STRUCTURE CHECK: [passed/concern — state HH/HL, pillar count out of 5, ADX reading]
MOMENTUM CHECK: [sustainable/exhausted/extended — state RSI value, distance from EMA21 in ATRs]
OSCILLATOR CHECK: [clear/warning — state RSI, Stoch K, BB position]
TECHNICAL VIEW: [2-3 sentences combining chart + data into your technical assessment]
FUNDAMENTAL VIEW: [1-2 sentences — news sentiment, central bank stance, macro risk]
REASON: [2-3 sentences — your complete final assessment combining all inputs]
"""



# ==========================================
# GEMINI — ROUND 1 (BLIND, WITH CHART VISION)
# ==========================================
def gemini_analysis(signal, news, macro, chart_b64=None, chart_mime="image/png"):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, news, macro, twelve, calendar)

    prompt_text = f"""You are an experienced institutional trader and market analyst.
Your analysis is INDEPENDENT — you have not seen any other analyst's view.

Analyse the H1 chart and all market data below.
{ROUND1_RULES}

MARKET DATA:
{context}"""

    # Build Gemini content parts
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
# CHATGPT — ROUND 1 (BLIND, WITH CHART VISION)
# ==========================================
def chatgpt_analysis(symbol, price, signal, news, macro, calendar,
                     chart_b64=None, chart_mime="image/png"):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve  = signal.get("twelve_data", {})
    context = build_market_context(signal, news, macro, twelve, calendar)

    prompt_text = f"""You are an experienced institutional trader and market analyst.
Your analysis is INDEPENDENT — you have not seen any other analyst's view.

Analyse the H1 chart and all market data below.
{ROUND1_RULES}

MARKET DATA:
{context}

{"The chart image is attached. Analyse it thoroughly in your CHART ANALYSIS field." if chart_b64 else "No chart image provided — rely on numerical data only."}"""

    # Build message content
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
# CHATGPT — DECIDES: AGREE / DEBATE / NEUTRAL
# This replaces the old static agreement check.
# ChatGPT receives both Round 1 outputs and decides
# whether both analyses agree, or debate is needed.
# ==========================================
def chatgpt_decides(symbol, price, signal, gemini_out, chatgpt_out,
                    chart_b64=None, chart_mime="image/png"):
    """
    ChatGPT reviews both Round 1 analyses independently produced
    by Gemini and ChatGPT. It decides:
      AGREE_BUY    → both effectively agree, direction is BUY
      AGREE_SELL   → both effectively agree, direction is SELL
      AGREE_NEUTRAL→ both effectively agree, no trade
      DEBATE       → genuine disagreement, debate round needed

    This is more nuanced than simple string matching —
    ChatGPT reads the reasoning of both, not just the labels.
    """
    if not CHATGPT_KEY:
        return "DEBATE", "ChatGPT unavailable — defaulting to debate"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, {}, {}, twelve, calendar)
    atr_td   = safe_float(twelve.get("atr"))
    atr_ps   = safe_float(signal.get("atr"))
    atr_use  = atr_td or atr_ps

    prompt_parts = [{"type": "text", "text": f"""You are a senior execution trader reviewing two independent analyses of {symbol} H1.

GEMINI ANALYSIS:
{gemini_out}

YOUR OWN ROUND 1 ANALYSIS:
{chatgpt_out}

MARKET DATA SUMMARY:
{context}

ATR (for SL/TP reference): {atr_use}
T1 Target: {signal.get('t1_target','N/A')}
T2 Target: {signal.get('t2_target','N/A')}

{"Chart image attached for your reference." if chart_b64 else ""}

━━━ YOUR TASK ━━━

Review both analyses carefully. Consider:
1. Do both agree on direction? (even if confidence differs)
2. Does chart analysis in both match? (BOS/CHoCH/patterns)
3. Are the hard block checks consistent?
4. Is there a genuine analytical disagreement that needs resolution?

AGREEMENT CRITERIA:
- Both output BUY (even if one is 65% and other is 80%) → AGREE_BUY
- Both output SELL → AGREE_SELL
- Both output NEUTRAL → AGREE_NEUTRAL
- One says BUY, other says SELL → DEBATE
- One says NEUTRAL, other says directional → DEBATE (one-sided conviction)
- Same direction but chart analysis directly contradicts numerical data → DEBATE

After deciding, if AGREE_BUY or AGREE_SELL:
Produce the final trade parameters now. SL/TP will be calculated by the system
using ATR — you do NOT need to provide them.
Just confirm the direction and produce your reasoning.

Respond in EXACTLY this format:
DECISION: [AGREE_BUY / AGREE_SELL / AGREE_NEUTRAL / DEBATE]
FINAL DIRECTION: [BUY / SELL / NEUTRAL / PENDING_DEBATE]
CONFIDENCE: [0-100]
AGREEMENT REASON: [one sentence — why you agree or why debate is needed]
CHART CONSENSUS: [one sentence — do both chart analyses align?]
TECHNICAL VIEW: [2-3 sentences on the combined technical picture]
FUNDAMENTAL VIEW: [1-2 sentences on macro/news]
WHY THIS DECISION: [2-3 sentences — final reasoning]
"""}]

    if chart_b64:
        prompt_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_mime};base64,{chart_b64}", "detail": "high"}
        })

    # Web search on this decision stage if enabled
    tools = [{"type": "web_search_preview"}] if ENABLE_WEB_SEARCH else []

    try:
        payload = {
            "model":    "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt_parts}],
            "temperature": 0.3,
            "max_tokens":  1200
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

        if not text.strip():
            return "DEBATE", "Could not parse decision response"

        # Extract decision
        dm = re.search(r'DECISION\s*[:\-]\s*(AGREE_BUY|AGREE_SELL|AGREE_NEUTRAL|DEBATE)', text.upper())
        decision = dm.group(1) if dm else "DEBATE"
        return decision, text

    except Exception as e:
        print(f"chatgpt_decides error: {e}")
        # Fallback without web search
        try:
            r = requests.post(
                OPENAI_URL,
                headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
                json={
                    "model":    "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt_parts}],
                    "temperature": 0.3,
                    "max_tokens":  1200
                },
                timeout=60
            )
            r.raise_for_status()
            text     = r.json()["choices"][0]["message"]["content"]
            dm       = re.search(r'DECISION\s*[:\-]\s*(AGREE_BUY|AGREE_SELL|AGREE_NEUTRAL|DEBATE)', text.upper())
            decision = dm.group(1) if dm else "DEBATE"
            return decision, text
        except Exception as e2:
            return "DEBATE", f"Decision error: {e2}"


# ==========================================
# CHATGPT — CHALLENGE GEMINI (debate round)
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

No final call — just challenge the specific gap in reasoning."""

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
# GEMINI — DEFEND (debate round)
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
SUMMARY: [one sentence — your final defended stance]"""

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
# CHATGPT — FINAL VERDICT (after debate)
# Web search enabled here for final validation
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
1. Calendar: 2+ relevant events? → NEUTRAL
2. Pivot: At R2/R3 on BUY or S2/S3 on SELL? → NEUTRAL
3. Failed auction opposing direction? → NEUTRAL
4. Triple exhaustion (RSI>70+Stoch>80+price>BB upper)? → NEUTRAL
5. Chart: Does chart show CHoCH or double top/bottom opposing direction? → NEUTRAL or reverse
6. Validator: LOW confidence + T1 < 35%? → NEUTRAL
7. Can this trade achieve minimum 1:1.5 R:R with ATR {atr_use}? → if NO → NEUTRAL

SL/TP NOTE: You do NOT need to calculate SL/TP.
The system will use ATR {atr_use} × {SL_ATR_MULTIPLIER} for SL and × {TP_ATR_MULTIPLIER} for TP.
Just confirm the direction.

If using web search: check only for breaking news in last 4 hours affecting {symbol}.
Do not search to confirm what you already know.

Respond in EXACTLY this format:
FINAL DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100]
CHECKLIST: [which items passed / which blocked]
CHART CONFIRMS: [YES/NO — does chart support the final direction?]
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

    return (
        f"{dir_line} | {symbol} | {su}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Decision:    {path_tag}\n"
        f"Confidence:  {confidence}\n"
        f"Gemini:      {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
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
# FINAL OUTPUT HANDLER (shared by all paths)
# ==========================================
def _send_trade(symbol, price, direction, signal, twelve_data,
                gemini_dir, chatgpt_dir, path, session,
                decision_text, calendar, post_debate=False):
    """
    Extracts fields from decision text, calculates SL/TP in Python,
    validates, and sends final Telegram message.
    """
    final_dir = extract_direction(decision_text)

    if final_dir == "NEUTRAL":
        reason = extract_neutral_reason(decision_text, "", calendar, signal)
        conf   = extract_confidence(decision_text)
        send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
        send_neutral_audit(symbol, gemini_dir, decision_text, reason, post_debate=post_debate)
        update_neutral_cache(symbol, session, "NEUTRAL", signal, twelve_data)
        return False

    # Calculate SL/TP deterministically from ATR
    atr_td  = safe_float(twelve_data.get("atr"))
    atr_ps  = safe_float(signal.get("atr"))
    atr_use = atr_td or atr_ps
    sl, tp, rr = calculate_sl_tp(final_dir, price, atr_use)

    # Validate
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
# MAIN WEBHOOK
# ==========================================
@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook active. POST signals to /webhook", 200

    try:
        rj   = request.get_json(silent=True, force=True)
        rt   = request.get_data(as_text=True)
        print(f"Incoming: {rt[:300]}")
        data = parse_incoming_data(rj, rt)

        if not data:
            send_telegram("❌ No parseable data from TradingView")
            return "No data", 400

        # ── Field Extraction ─────────────────────────────────────
        symbol     = clean_symbol(safe_str(data.get("symbol") or data.get("ticker"), "Unknown"))
        price_raw  = safe_float(data.get("price") or data.get("close") or 0, allow_zero=False)
        price      = price_raw if price_raw else 0.0
        data["tf"] = data["timeframe"] = "1H"

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

        # Normalize bool fields
        data["lon_fail_up"]   = safe_bool(data.get("lon_fail_up"))
        data["lon_fail_down"] = safe_bool(data.get("lon_fail_down"))
        data["ny_fail_up"]    = safe_bool(data.get("ny_fail_up"))
        data["ny_fail_down"]  = safe_bool(data.get("ny_fail_down"))

        # Chart image — fetch from URL in alert payload
        chart_url  = data.get("chart_url") or data.get("chart_image_url") or data.get("chart_screenshot")
        chart_b64  = fetch_chart_image(chart_url) if chart_url else None
        chart_mime = get_image_mime(chart_url) if chart_url else "image/png"
        chart_info = "✅ Chart attached" if chart_b64 else "⚠️ No chart image"

        print(f"{symbol} | {price} | {direction} | Score:{score} | EA:{signal_ea} | Chart:{chart_info}")

        # ══════════════════════════════════════════════════════
        # GATE 1 — SESSION & FREQUENCY
        # ══════════════════════════════════════════════════════
        allowed, gate_reason, session = session_gate(symbol)
        if not allowed:
            if session == "off":
                # ADD THIS ONE-LINE MESSAGE
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
            return "Blocked", 200

        # ── Signal In ─────────────────────────────────────────
        send_telegram(
            f"📡 SIGNAL IN | {symbol} | {session.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Price:{price}  ATR:{atr}  Score:{score}/100\n"
            f"Dir:{direction}  Struct:{structure}  ADX:{adx}\n"
            f"EA:{signal_ea}/8  Vol:{volume}\n"
            f"EMA:{ema_21}/{ema_50}/{ema_200}\n"
            f"Pivot:{pivot_zone}  VWAP:{vwap_band}\n"
            f"Validator:{sv_valid} ({sv_conf})\n"
            f"Chart: {chart_info}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Fetching live data..."
        )

        # ══════════════════════════════════════════════════════
        # GATE 2 — PARALLEL DATA FETCH
        # ══════════════════════════════════════════════════════
        news, macro, calendar = fetch_all_live_data(symbol)

        # ══════════════════════════════════════════════════════
        # GATE 2.5 — CURRENCY-SPECIFIC CALENDAR HARD BLOCK
        # Only blocks if this pair's currencies have 2+ events
        # ══════════════════════════════════════════════════════
        sc          = clean_symbol(symbol)
        currencies  = [sc[:3], sc[3:]] if len(sc) == 6 else [sc]
        events_raw  = calendar.get("events", [])
        rel_events  = []
        for e in events_raw:
            if isinstance(e, dict) and e.get("currency","") in currencies:
                rel_events.append(e.get("event",""))
            elif isinstance(e, str):
                rel_events.append(e)   # fallback if event is just a string

        if len(rel_events) >= 2:
            send_telegram(
                f"🚫 CALENDAR BLOCK | {symbol} | {session.upper()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Relevant events for {'/'.join(currencies)}: {len(rel_events)}\n"
                f"Events: {', '.join(rel_events)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Do not trade during currency-specific high-impact events."
            )
            return "Calendar block", 200

        # ══════════════════════════════════════════════════════
        # GATE 3 — TWELVE DATA
        # ══════════════════════════════════════════════════════
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
            f"Calendar: {cal_ev_names}\n"
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
            f"⏳ MCS check..."
        )

        # ══════════════════════════════════════════════════════
        # GATE 3.5 — MCS AUTO-NEUTRAL
        # ══════════════════════════════════════════════════════
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
                f"Reason:  {mcs_reason}"
            )
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            increment_auto_streak(symbol, session)
            return "OK", 200

        reset_auto_streak(symbol, session)
        send_telegram(
            f"🔄 MCS: {mcs_score:.1f}% — Running AI analysis...\n"
            f"{'🖼️ Chart image will be analysed by both AIs' if chart_b64 else '⚠️ No chart — numerical analysis only'}"
        )

        # ══════════════════════════════════════════════════════
        # GATE 4 — BLIND PARALLEL AI ROUND 1
        # Both receive data context + chart image independently
        # ══════════════════════════════════════════════════════
        with ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(gemini_analysis, data, news, macro, chart_b64, chart_mime)
            fc = ex.submit(chatgpt_analysis, symbol, price, data, news, macro,
                           calendar, chart_b64, chart_mime)
            gemini  = fg.result()
            chatgpt = fc.result()

        if "GEMINI UNAVAILABLE" in gemini and "CHATGPT ERROR" in chatgpt:
            send_telegram(f"❌ Both AI systems failed for {symbol}. Signal aborted.")
            return "AI failure", 500

        gemini_dir  = extract_direction(gemini)  if "GEMINI UNAVAILABLE" not in gemini  else "UNAVAILABLE"
        chatgpt_dir = extract_direction(chatgpt) if "CHATGPT ERROR"      not in chatgpt else "UNAVAILABLE"
        print(f"R1 — Gemini: {gemini_dir} | ChatGPT: {chatgpt_dir}")

        # ChatGPT down — cannot proceed
        if "CHATGPT ERROR" in chatgpt:
            send_telegram(f"❌ ChatGPT unavailable — cannot produce final decision. Skipped.")
            return "ChatGPT unavailable", 200

        # Gemini down — ChatGPT solo (no debate possible)
        if "GEMINI UNAVAILABLE" in gemini:
            send_telegram(f"⚠️ Gemini unavailable — ChatGPT solo analysis.")
            gemini_dir = "UNAVAILABLE"
            if chatgpt_dir in ("BUY","SELL"):
                _send_trade(symbol, price, chatgpt_dir, data, twelve_data,
                            "UNAVAILABLE", chatgpt_dir, "SOLO", session,
                            chatgpt, calendar)
            else:
                reason = extract_neutral_reason(gemini, chatgpt, calendar, data)
                conf   = extract_confidence(chatgpt)
                send_neutral(symbol, conf, reason, session, "UNAVAILABLE", chatgpt_dir)
                send_neutral_audit(symbol, "UNAVAILABLE", chatgpt, reason)
                update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return "OK", 200

        # ══════════════════════════════════════════════════════
        # GATE 5 — CHATGPT DECIDES: AGREE / DEBATE / NEUTRAL
        # ChatGPT reads both Round 1 outputs and decides.
        # More intelligent than simple string matching.
        # ══════════════════════════════════════════════════════
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

        # ── AGREE_NEUTRAL ─────────────────────────────────────
        if decision == "AGREE_NEUTRAL":
            reason = extract_neutral_reason(gemini, chatgpt, calendar, data)
            conf   = extract_confidence(decision_text)
            send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
            send_neutral_audit(symbol, gemini, chatgpt, reason)
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return "OK", 200

        # ── AGREE_BUY or AGREE_SELL ───────────────────────────
        elif decision in ("AGREE_BUY","AGREE_SELL"):
            final_dir = "BUY" if decision == "AGREE_BUY" else "SELL"
            send_telegram(
                f"✅ AGREEMENT | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Decision: {final_dir}\n"
                f"Gemini: {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
                f"Producing execution parameters..."
            )
            _send_trade(symbol, price, final_dir, data, twelve_data,
                        gemini_dir, chatgpt_dir, "AGREED", session,
                        decision_text, calendar)
            return "OK", 200

        # ── DEBATE ────────────────────────────────────────────
        else:
            send_telegram(
                f"⚔️ DEBATE | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Gemini: {gemini_dir}  |  ChatGPT: {chatgpt_dir}\n"
                f"ChatGPT challenging Gemini..."
            )

            # ════════════════════════════════════════════════
            # GATE 6 — DEBATE ROUND
            # ════════════════════════════════════════════════
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

            _send_trade(symbol, price, "PENDING", data, twelve_data,
                        gemini_dir, chatgpt_dir, "DEBATE", session,
                        final_text, calendar, post_debate=True)
            return "OK", 200

    except Exception as e:
        err = f"ERROR: {str(e)[:200]}"
        print(err)
        send_telegram(f"❌ System Error | {err}")
        return err, 500


# ==========================================
# HEALTH & ROUTES
# ==========================================
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/", methods=["GET"])
def root():
    return "MIKA Bot running. POST signals to /webhook", 200


# ==========================================
# STARTUP
# ==========================================
if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(send_presession_snapshot, "cron",
                      hour=4, minute=0, args=["overnight"], id="overnight")
    scheduler.add_job(send_presession_snapshot, "cron",
                      hour=6, minute=59, args=["presession"], id="presession")
    scheduler.start()
    print("Scheduler started: 09:00 and 11:59 PKT daily")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
