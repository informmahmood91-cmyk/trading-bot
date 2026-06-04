import json
import os
import re
import time
import requests
from flask import Flask, request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

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

# ==========================================
# API ENDPOINTS
# ==========================================
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# ==========================================
# STATE CONTROL
# ==========================================
pair_session_tracker = {}
current_day = None

# ==========================================
# NEUTRAL SIGNAL CACHE
# ==========================================
# Stores last signal snapshot per pair to enable
# Market Change Score (MCS) calculation.
# Structure per pair key:
# {
#   "last_outcome":   "NEUTRAL" / "BUY" / "SELL",
#   "last_signal":    { price, rsi, macd_hist, stoch_k,
#                       cci, mfi, willr, score, volume_ratio },
#   "last_timestamp": datetime (UTC),
#   "auto_count":     int  — how many times auto-neutral fired in a row
# }
neutral_cache = {}

# ── Tunable thresholds ─────────────────────────────────
# MCS below this → auto-neutral (skip AI entirely)
MCS_THRESHOLD = 15.0

# If price moves more than this % → always run AI
PRICE_VETO_PCT = 0.30

# Cached neutral expires after this many minutes → always run AI
CACHE_EXPIRY_MINUTES = 30

# Max consecutive auto-neutrals before forcing a real AI check
# (prevents stale cache from suppressing a regime change)
MAX_AUTO_NEUTRAL_STREAK = 4


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


def safe_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


def safe_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0


def clean_symbol(symbol):
    prefixes = [
        "FX:", "OANDA:", "BINANCE:", "PYTH:",
        "TVC:", "CAPITALCOM:", "PEPPERSTONE:",
        "ICMARKETS:", "FXCM:", "FOREX:"
    ]
    s = symbol.upper().strip()
    for prefix in prefixes:
        s = s.replace(prefix, "")
    return s.strip()


def parse_incoming_data(request_json, request_text):
    if request_json and isinstance(request_json, dict):
        print(f"Parsed as JSON: {request_json}")
        return request_json
    if request_text:
        try:
            parsed = json.loads(request_text.strip())
            print(f"Raw text parsed: {parsed}")
            return parsed
        except Exception:
            pass
    print(f"Could not parse: {request_text}")
    return {}


# ==========================================
# EXTRACTION HELPERS — UNIFIED REGEX
# ==========================================
def extract_direction(text):
    """
    Extracts DIRECTION or FINAL DIRECTION from labeled line only.
    Never does substring match — prevents 'do NOT BUY' false positives.
    Returns: BUY / SELL / NEUTRAL
    """
    match = re.search(r'FINAL\s+DIRECTION\s*[:\-]\s*(BUY|SELL|NEUTRAL)', text.upper())
    if match:
        return match.group(1)
    match = re.search(r'(?<!\w)DIRECTION\s*[:\-]\s*(BUY|SELL|NEUTRAL)', text.upper())
    if match:
        return match.group(1)
    return "NEUTRAL"


def extract_confidence(text):
    match = re.search(r'CONFIDENCE\s*[:\-]\s*(\d+)', text)
    return match.group(1) + "%" if match else "N/A"


def extract_sl(text):
    match = re.search(r'STOP\s*LOSS\s*[:\-]\s*([\d.]+)', text, re.IGNORECASE)
    return match.group(1) if match else "N/A"


def extract_tp(text):
    match = re.search(r'TAKE\s*PROFIT\s*[:\-]\s*([\d.]+)', text, re.IGNORECASE)
    return match.group(1) if match else "N/A"


def extract_rr(text):
    match = re.search(r'RISK\s*[:\-/]?\s*REWARD\s*[:\-]\s*(1\s*[:/]\s*[\d.]+)', text, re.IGNORECASE)
    return match.group(1).replace(" ", "") if match else "N/A"


def extract_field(text, label):
    """Extract single-line field value by label name."""
    pattern = rf'{re.escape(label)}\s*[:\-]\s*(.+)'
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else "N/A"


def extract_multiline_field(text, label):
    """Extract field that may span multiple lines until next labeled field."""
    pattern = rf'{re.escape(label)}\s*[:\-]\s*(.*?)(?=\n[A-Z ]+\s*[:\-]|\Z)'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip().replace("\n", " ")
    return "N/A"


# ==========================================
# SL/TP SANITY VALIDATION
# ==========================================
def validate_levels(direction, price, sl, tp):
    """
    Validates SL and TP are on the correct side of entry.
    Returns: (is_valid: bool, reason: str)
    """
    try:
        sl_f = float(sl)
        tp_f = float(tp)
        pr_f = float(price)
        if sl_f <= 0 or tp_f <= 0 or pr_f <= 0:
            return False, "Zero or negative values"
        if direction == "BUY":
            if sl_f >= pr_f:
                return False, f"BUY SL {sl_f} must be below entry {pr_f}"
            if tp_f <= pr_f:
                return False, f"BUY TP {tp_f} must be above entry {pr_f}"
            return True, "OK"
        elif direction == "SELL":
            if sl_f <= pr_f:
                return False, f"SELL SL {sl_f} must be above entry {pr_f}"
            if tp_f >= pr_f:
                return False, f"SELL TP {tp_f} must be below entry {pr_f}"
            return True, "OK"
    except (ValueError, TypeError) as e:
        return False, f"Could not parse levels: {e}"
    return False, "Unknown direction"


# ==========================================
# NEUTRAL REASON EXTRACTOR
# ==========================================
def extract_neutral_reason(text_a, text_b, calendar):
    combined = (text_a + " " + text_b).lower()
    if calendar.get("high_impact_soon"):
        events = calendar.get("events", [])
        if events:
            return f"{events[0].split()[0][:8]} event"
        return "High impact event"
    if "technical conflict" in combined or "contradict" in combined:
        return "Tech conflict"
    if "structure" in combined and "bear" in combined and "bull" in combined:
        return "Structure conflict"
    if "ranging" in combined:
        return "Ranging market"
    if "flat" in combined and "macd" in combined:
        return "Flat momentum"
    if "opposite" in combined:
        return "Tech vs Fund"
    return "No clear edge"


# ==========================================
# MARKET CHANGE SCORE (MCS) ENGINE
# ==========================================

def _pct_change(old, new):
    """
    Returns absolute % change between two values.
    Returns 0.0 if either value is missing or non-numeric.
    """
    try:
        o = float(old)
        n = float(new)
        if o == 0:
            return 0.0
        return abs((n - o) / o) * 100.0
    except (TypeError, ValueError):
        return 0.0


def _normalize(pct_change, scale):
    """
    Maps a raw % change to a 0–100 score.
    scale = the % change that represents "100% changed" for this indicator.
    E.g. RSI scale=10 means a 10-point RSI move = score of 100.
    Capped at 100.
    """
    return min((pct_change / scale) * 100.0, 100.0)


def compute_mcs(prev_snapshot, curr_snapshot):
    """
    Computes a weighted Market Change Score (0–100) between
    the previous signal snapshot and the current one.

    Weights (must sum to 1.0):
      price          0.25  — direct market movement
      macd_histogram 0.20  — best momentum shift proxy
      rsi            0.20  — momentum confirmation
      score          0.15  — your engine's own assessment
      stoch_k        0.10  — short-term momentum
      volume_ratio   0.10  — institutional activity proxy

    Scale values represent the % change that would score 100
    for each indicator — tuned for H1 forex/commodities.
    """
    components = [
        # (field,            weight, scale)
        ("price",            0.25,   0.30),   # 0.30% price move = full score
        ("macd_histogram",   0.20,   50.0),   # 50% change in histogram
        ("rsi",              0.20,   10.0),   # 10% RSI change
        ("score",            0.15,   15.0),   # 15-point score shift
        ("stoch_k",          0.10,   20.0),   # 20% stoch move
        ("volume_ratio",     0.10,   30.0),   # 30% volume change
    ]

    mcs = 0.0
    breakdown = {}

    for field, weight, scale in components:
        old_val = prev_snapshot.get(field)
        new_val = curr_snapshot.get(field)

        if old_val is None or new_val is None:
            # Missing data — treat as no change for that component
            component_score = 0.0
        else:
            raw_pct  = _pct_change(old_val, new_val)
            component_score = _normalize(raw_pct, scale) * weight

        mcs += component_score
        breakdown[field] = round(component_score / weight, 1) if weight > 0 else 0.0

    return round(mcs, 2), breakdown


def build_signal_snapshot(data, twelve_data):
    """
    Extracts the numeric values needed for MCS comparison
    from the current incoming signal and Twelve Data output.
    Returns a flat dict of floats (None if unavailable).
    """
    def _f(v):
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    return {
        "price":          _f(data.get("price") or data.get("close")),
        "rsi":            _f(twelve_data.get("rsi")),
        "macd_histogram": _f(twelve_data.get("macd_histogram")),
        "stoch_k":        _f(twelve_data.get("stoch_k")),
        "cci":            _f(twelve_data.get("cci")),
        "mfi":            _f(twelve_data.get("mfi")),
        "willr":          _f(twelve_data.get("willr")),
        "score":          _f(data.get("score")),
        "volume_ratio":   _f(data.get("volume_ratio")),
    }


def should_auto_neutral(symbol, session, data, twelve_data, calendar):
    """
    Master gate — decides whether to skip AI entirely and auto-neutral.

    Returns: (skip: bool, reason: str, mcs: float)

    skip=True  → fire auto-neutral, no AI calls
    skip=False → run full AI pipeline

    Veto conditions that ALWAYS force AI (skip=False):
      V1. No cached neutral exists for this pair/session
      V2. Last outcome was BUY or SELL (not neutral)
      V3. Cache expired (> CACHE_EXPIRY_MINUTES old)
      V4. High-impact calendar event detected
      V5. Price moved > PRICE_VETO_PCT since last signal
      V6. Auto-neutral streak >= MAX_AUTO_NEUTRAL_STREAK
      V7. MCS >= MCS_THRESHOLD
    """
    key = f"{clean_symbol(symbol)}_{session}"
    cached = neutral_cache.get(key)

    # V1 — No cache
    if cached is None:
        return False, "No cache — first signal", 0.0

    # V2 — Last outcome was a trade
    if cached.get("last_outcome") in ("BUY", "SELL"):
        return False, "Last outcome was trade — re-evaluate", 0.0

    # V3 — Cache expired
    last_ts = cached.get("last_timestamp")
    if last_ts:
        age_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        if age_minutes > CACHE_EXPIRY_MINUTES:
            return False, f"Cache expired ({age_minutes:.0f}m old)", 0.0

    # V4 — High-impact calendar event
    if calendar.get("high_impact_soon"):
        events = ", ".join(calendar.get("events", []))
        return False, f"High-impact event: {events}", 0.0

    # V5 — Price veto
    prev_snapshot = cached.get("last_signal", {})
    curr_snapshot = build_signal_snapshot(data, twelve_data)
    prev_price = prev_snapshot.get("price")
    curr_price = curr_snapshot.get("price")

    if prev_price and curr_price:
        price_pct = _pct_change(prev_price, curr_price)
        if price_pct >= PRICE_VETO_PCT:
            return False, f"Price moved {price_pct:.3f}% — veto", price_pct

    # V6 — Streak protection
    streak = cached.get("auto_count", 0)
    if streak >= MAX_AUTO_NEUTRAL_STREAK:
        return False, f"Max streak ({streak}) reached — forced AI check", 0.0

    # V7 — MCS threshold
    mcs, breakdown = compute_mcs(prev_snapshot, curr_snapshot)
    print(f"MCS for {symbol}: {mcs:.1f}% | breakdown: {breakdown}")

    if mcs >= MCS_THRESHOLD:
        return False, f"MCS {mcs:.1f}% >= threshold {MCS_THRESHOLD}% — run AI", mcs

    # All checks passed — auto-neutral is safe
    return True, f"MCS {mcs:.1f}% < {MCS_THRESHOLD}% — auto-neutral", mcs


def update_neutral_cache(symbol, session, outcome, data, twelve_data):
    """
    Updates the neutral cache after any pipeline completion.
    Called for BOTH auto-neutrals and AI-evaluated neutrals.
    outcome: "NEUTRAL" / "BUY" / "SELL"
    """
    key = f"{clean_symbol(symbol)}_{session}"
    existing = neutral_cache.get(key, {})

    snapshot = build_signal_snapshot(data, twelve_data)

    if outcome == "NEUTRAL":
        auto_count = existing.get("auto_count", 0)
        # Only increment streak if this was an auto-neutral
        # (tracked separately by caller)
    else:
        auto_count = 0  # Reset streak on any non-neutral outcome

    neutral_cache[key] = {
        "last_outcome":   outcome,
        "last_signal":    snapshot,
        "last_timestamp": datetime.now(timezone.utc),
        "auto_count":     auto_count,
    }
    print(f"Cache updated: {key} | outcome={outcome} | streak={auto_count}")


def increment_auto_streak(symbol, session):
    """Increments the auto-neutral streak counter for a pair."""
    key = f"{clean_symbol(symbol)}_{session}"
    if key in neutral_cache:
        neutral_cache[key]["auto_count"] = neutral_cache[key].get("auto_count", 0) + 1
        print(f"Auto-neutral streak: {key} = {neutral_cache[key]['auto_count']}")


def reset_auto_streak(symbol, session):
    """Resets auto-neutral streak after a real AI check runs."""
    key = f"{clean_symbol(symbol)}_{session}"
    if key in neutral_cache:
        neutral_cache[key]["auto_count"] = 0


# ==========================================
# DAILY RESET
# ==========================================
def reset_day():
    global current_day, pair_session_tracker, neutral_cache
    today = datetime.now(timezone.utc).date()
    if current_day != today:
        current_day = today
        pair_session_tracker = {}
        neutral_cache = {}
        print(f"Day reset: {today}")


# ==========================================
# SESSION DETECTION
# ==========================================
def get_session_by_time():
    now_utc = datetime.now(timezone.utc)
    pk_hour = (now_utc.hour + 5) % 24
    if 12 <= pk_hour < 18:
        return "london"
    elif 18 <= pk_hour or pk_hour < 2:
        return "newyork"
    else:
        return "off"


def session_gate(symbol):
    global pair_session_tracker
    reset_day()
    session = get_session_by_time()
    if session == "off":
        return False, "Outside trading sessions", session
    symbol_clean = clean_symbol(symbol)
    key = f"{symbol_clean}_{session}"
    now = datetime.now(timezone.utc)
    timestamps = pair_session_tracker.get(key, [])
    if len(timestamps) >= 2:
        return False, f"2/2 trades used for {symbol_clean} in {session}", session
    if len(timestamps) == 1:
        diff = (now - timestamps[0]).total_seconds()
        if diff < 7200:
            remaining = int(7200 - diff)
            return False, f"Cooldown active — {remaining}s remaining", session
    return True, "OK", session


def register_trade(symbol, session):
    global pair_session_tracker
    symbol_clean = clean_symbol(symbol)
    key = f"{symbol_clean}_{session}"
    now = datetime.now(timezone.utc)
    timestamps = pair_session_tracker.get(key, [])
    timestamps.append(now)
    if len(timestamps) > 2:
        timestamps = timestamps[-2:]
    pair_session_tracker[key] = timestamps
    print(f"Trade registered: {symbol_clean} {session} | count: {len(timestamps)}/2")


# ==========================================
# FINNHUB — SYMBOL-SPECIFIC NEWS
# ==========================================
def fetch_finnhub(symbol):
    if not FINNHUB_KEY:
        return {"sentiment": "NEUTRAL", "headlines": [], "symbol_matched": False}

    symbol = clean_symbol(symbol)
    SYMBOL_KEYWORDS = {
        "EURUSD":  ["EUR", "Euro", "ECB", "European"],
        "GBPUSD":  ["GBP", "Pound", "Sterling", "BOE"],
        "USDJPY":  ["JPY", "Yen", "BOJ", "Japan"],
        "USDCHF":  ["CHF", "Franc", "SNB", "Swiss"],
        "AUDUSD":  ["AUD", "Aussie", "RBA", "Australia"],
        "NZDUSD":  ["NZD", "Kiwi", "RBNZ", "New Zealand"],
        "USDCAD":  ["CAD", "Loonie", "BOC", "Canada"],
        "GBPJPY":  ["GBP", "Pound", "JPY", "Yen"],
        "EURJPY":  ["EUR", "Euro", "JPY", "Yen"],
        "EURGBP":  ["EUR", "Euro", "GBP", "Pound"],
        "GBPCHF":  ["GBP", "Pound", "CHF", "Franc"],
        "XAUUSD":  ["Gold", "XAU", "bullion", "precious metals", "Fed"],
        "GOLD":    ["Gold", "XAU", "bullion"],
        "XAGUSD":  ["Silver", "XAG"],
        "USOIL":   ["Oil", "WTI", "crude", "OPEC"],
        "UKOIL":   ["Oil", "Brent", "crude", "OPEC"],
        "BTCUSD":  ["Bitcoin", "BTC", "crypto"],
        "ETHUSD":  ["Ethereum", "ETH", "crypto"],
        "US30":    ["Dow Jones", "DJIA", "Wall Street"],
        "NAS100":  ["Nasdaq", "tech stocks"],
        "SPX500":  ["S&P 500", "SPX"],
    }
    keywords = SYMBOL_KEYWORDS.get(symbol, [symbol[:3], symbol[3:]] if len(symbol) == 6 else [symbol])

    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}",
            timeout=10
        )
        r.raise_for_status()
        all_news = r.json()[:30]
        matched = [n.get("headline", "") for n in all_news
                   if any(kw.lower() in n.get("headline", "").lower() for kw in keywords)]
        if not matched:
            matched = [n.get("headline", "") for n in all_news[:5]]

        text = " ".join(matched).lower()
        bull = sum(w in text for w in ["rise", "bull", "gain", "growth", "hawkish", "strong"])
        bear = sum(w in text for w in ["fall", "bear", "drop", "recession", "dovish", "weak"])
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
        url = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={now.strftime('%Y-%m-%d')}"
            f"&to={(now + timedelta(days=1)).strftime('%Y-%m-%d')}"
            f"&token={FINNHUB_KEY}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        events = r.json().get("economicCalendar", [])
        high = [e for e in events if e.get("impact") == "high"]
        return {"high_impact_soon": bool(high), "events": [e.get("event", "") for e in high[:3]]}
    except Exception as e:
        print(f"Calendar error: {e}")
        return {"high_impact_soon": False, "events": []}


# ==========================================
# GDELT — MACRO RISK
# ==========================================
def fetch_gdelt():
    try:
        r = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc?query=global%20economy&mode=ArtList&format=json",
            timeout=10
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])[:5]
        score = sum(
            1 for a in articles
            if any(w in a.get("title", "").lower()
                   for w in ["war", "inflation", "crisis", "recession", "sanctions"])
        )
        risk = "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"
        return {"risk": risk, "score": score}
    except Exception as e:
        print(f"GDELT error: {e}")
        return {"risk": "LOW", "score": 0}


# ==========================================
# PARALLEL DATA FETCH
# ==========================================
def fetch_all_live_data(symbol):
    """Runs Finnhub, GDELT, Calendar simultaneously. Saves 2-4 seconds."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        fn = ex.submit(fetch_finnhub, symbol)
        fm = ex.submit(fetch_gdelt)
        fc = ex.submit(fetch_economic_calendar)
        return fn.result(), fm.result(), fc.result()


# ==========================================
# TWELVE DATA — 10 INDICATORS
# ==========================================
def fetch_twelve_data(symbol):
    if not TWELVE_DATA_KEY:
        return {}
    symbol = clean_symbol(symbol)
    if "/" not in symbol and len(symbol) == 6:
        symbol = f"{symbol[:3]}/{symbol[3:]}"

    base = "https://api.twelvedata.com"
    k = TWELVE_DATA_KEY
    endpoints = {
        "rsi":      f"{base}/rsi?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "macd":     f"{base}/macd?symbol={symbol}&interval=1h&apikey={k}",
        "stoch":    f"{base}/stoch?symbol={symbol}&interval=1h&apikey={k}",
        "cci":      f"{base}/cci?symbol={symbol}&interval=1h&time_period=20&apikey={k}",
        "mfi":      f"{base}/mfi?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "willr":    f"{base}/willr?symbol={symbol}&interval=1h&time_period=14&apikey={k}",
        "obv":      f"{base}/obv?symbol={symbol}&interval=1h&apikey={k}",
        "aroon":    f"{base}/aroon?symbol={symbol}&interval=1h&time_period=25&apikey={k}",
        "ichimoku": f"{base}/ichimoku?symbol={symbol}&interval=1h&apikey={k}",
        "psar":     f"{base}/psar?symbol={symbol}&interval=1h&apikey={k}",
    }
    raw = {}
    for name, url in endpoints.items():
        try:
            resp = requests.get(url, timeout=8).json()
            raw[name] = resp.get("values", [{}])[0] if resp.get("values") else resp.get("value", "N/A")
        except Exception as e:
            print(f"TwelveData {name} error: {e}")
            raw[name] = "N/A"
        time.sleep(0.5)

    def g(key, sub):
        v = raw.get(key)
        return v.get(sub, "N/A") if isinstance(v, dict) else "N/A"

    parsed = {
        "rsi":                 g("rsi", "rsi"),
        "macd":                g("macd", "macd"),
        "macd_signal":         g("macd", "macd_signal"),
        "macd_histogram":      g("macd", "macd_histogram"),
        "stoch_k":             g("stoch", "slowk"),
        "stoch_d":             g("stoch", "slowd"),
        "cci":                 g("cci", "cci"),
        "mfi":                 g("mfi", "mfi"),
        "willr":               g("willr", "willr"),
        "obv":                 g("obv", "obv"),
        "aroon_up":            g("aroon", "aroon_up"),
        "aroon_down":          g("aroon", "aroon_down"),
        "ichimoku_conversion": g("ichimoku", "tenkan_sen"),
        "ichimoku_base":       g("ichimoku", "kijun_sen"),
        "ichimoku_span_a":     g("ichimoku", "senkou_span_a"),
        "ichimoku_span_b":     g("ichimoku", "senkou_span_b"),
        "psar":                g("psar", "psar"),
    }
    missing = [k for k, v in parsed.items() if v == "N/A"]
    total = len(parsed)
    if not missing:
        parsed["_quality"] = f"FULL ({total}/{total})"
    elif len(missing) <= 5:
        parsed["_quality"] = f"PARTIAL ({total - len(missing)}/{total})"
    else:
        parsed["_quality"] = f"POOR ({total - len(missing)}/{total})"
    return parsed


# ==========================================
# PROMPT BUILDER — SHARED CONTEXT BLOCK
# ==========================================
def build_market_context(signal, news, macro, twelve, calendar):
    """
    Builds the shared market data block injected into all AI prompts.
    Written as a market brief — NOT as API/signal metadata.
    """
    cal_note = ""
    if calendar.get("high_impact_soon"):
        cal_note = f"High-impact economic events scheduled today: {', '.join(calendar.get('events', []))}."
    else:
        cal_note = "No high-impact economic events scheduled today."

    headlines_text = "\n".join(
        f"  - {h}" for h in news.get("headlines", [])
    ) or "  No relevant headlines found."

    signal_clean = {k: v for k, v in signal.items()
                    if k not in ["twelve_data", "calendar", "tf", "timeframe"]}

    twelve_quality = twelve.get("_quality", "Unknown")

    context = f"""
MARKET CONTEXT — {signal_clean.get('symbol', 'N/A')} | H1 TIMEFRAME
{'='*55}

PRICE ACTION & STRUCTURE:
  Current Price:    {signal_clean.get('price', 'N/A')}
  Trend Direction:  {signal_clean.get('direction', 'N/A')}
  Structure Bias:   {signal_clean.get('structure_bias', signal_clean.get('structure', 'N/A'))}
  Signal Score:     {signal_clean.get('score', 'N/A')}/100
  EA Filter Score:  {signal_clean.get('ea_score', signal_clean.get('ea_filter', 'N/A'))}/8
  ADX:              {signal_clean.get('adx', 'N/A')}
  ATR:              {signal_clean.get('atr', 'N/A')}
  EMA 21/50/200:    {signal_clean.get('ema_21', 'N/A')} / {signal_clean.get('ema_50', 'N/A')} / {signal_clean.get('ema_200', 'N/A')}
  Volume Ratio:     {signal_clean.get('volume_ratio', 'N/A')}
  Validator:        {signal_clean.get('validator', 'N/A')}

TECHNICAL INDICATORS (H1 — Live):
  Data Quality:     {twelve_quality}
  RSI (14):         {twelve.get('rsi', 'N/A')}
  MACD:             {twelve.get('macd', 'N/A')} | Signal: {twelve.get('macd_signal', 'N/A')} | Hist: {twelve.get('macd_histogram', 'N/A')}
  Stochastic K/D:   {twelve.get('stoch_k', 'N/A')} / {twelve.get('stoch_d', 'N/A')}
  CCI (20):         {twelve.get('cci', 'N/A')}
  MFI (14):         {twelve.get('mfi', 'N/A')}
  Williams %R:      {twelve.get('willr', 'N/A')}
  OBV:              {twelve.get('obv', 'N/A')}
  Aroon Up/Down:    {twelve.get('aroon_up', 'N/A')} / {twelve.get('aroon_down', 'N/A')}
  Ichimoku Conv/Base: {twelve.get('ichimoku_conversion', 'N/A')} / {twelve.get('ichimoku_base', 'N/A')}
  Ichimoku Span A/B:  {twelve.get('ichimoku_span_a', 'N/A')} / {twelve.get('ichimoku_span_b', 'N/A')}
  Parabolic SAR:    {twelve.get('psar', 'N/A')}

NEWS & SENTIMENT (Symbol-Specific):
  Sentiment:        {news.get('sentiment', 'N/A')}
  Symbol-Matched:   {'Yes' if news.get('symbol_matched') else 'No — general headlines used'}
  Headlines:
{headlines_text}

MACRO & CALENDAR:
  Global Risk:      {macro.get('risk', 'N/A')}
  Calendar:         {cal_note}
{'='*55}"""
    return context.strip()


# ==========================================
# GEMINI — ROUND 1 (BLIND — NO CHATGPT INPUT)
# Analysis only. No SL/TP.
# ==========================================
def gemini_analysis(signal, news, macro):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, news, macro, twelve, calendar)

    symbol    = clean_symbol(safe_str(signal.get("symbol") or signal.get("ticker"), "this pair"))
    structure = safe_str(signal.get("structure_bias") or signal.get("structure"), "N/A")
    direction = safe_str(signal.get("direction"), "N/A")

    prompt = f"""You are an experienced market analyst assessing whether conditions currently favor a trade.

Review the following market data and form your independent view:

{context}

ANALYSIS GUIDELINES:
- Focus on the H1 timeframe only
- The structural bias is {structure} and the chart signal direction is {direction}
- If structure and signal direction are aligned, that supports the case
- If they conflict, that weakens the case
- News and macro are supporting factors only — they cannot override technical structure
- Only call NEUTRAL if technicals and fundamentals are genuinely opposed, or a high-impact event is imminent
- Base your view on the full picture above — not any single indicator

THIS IS A DIRECTION ASSESSMENT ONLY.
Do not provide entry price, stop loss, or take profit at this stage.

Respond in EXACTLY this format — no extra text:
DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100%]
NEWS IMPACT: [POSITIVE/NEGATIVE/NEUTRAL]
MACRO RISK: [HIGH/MEDIUM/LOW]
CALENDAR WARNING: [YES/NO]
TECHNICAL VIEW: [Your reading of structure, momentum, and indicator alignment on H1]
FUNDAMENTAL VIEW: [Your view on central bank policy, macro conditions, and sentiment for this pair]
REASON: [2-3 sentences combining all factors into your final assessment]"""

    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 900}
                },
                timeout=30
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Server busy"


# ==========================================
# CHATGPT — ROUND 1 (BLIND — NO GEMINI INPUT)
# Analysis only. No SL/TP.
# ==========================================
def chatgpt_analysis(symbol, price, signal, news, macro, calendar):
    """
    Round 1 is fully independent — ChatGPT does NOT receive Gemini's output.
    This ensures genuine independent analysis before comparison.
    """
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve  = signal.get("twelve_data", {})
    context = build_market_context(signal, news, macro, twelve, calendar)

    structure = safe_str(signal.get("structure_bias") or signal.get("structure"), "N/A")
    direction = safe_str(signal.get("direction"), "N/A")

    prompt = f"""You are an experienced market analyst assessing whether conditions currently favor a trade.

Review the following market data and form your independent view:

{context}

ANALYSIS GUIDELINES:
- Focus on the H1 timeframe only
- The structural bias is {structure} and the chart signal direction is {direction}
- If structure and signal direction are aligned, that supports the case
- If they conflict, that weakens the case
- News and macro are supporting factors only — they cannot override technical structure
- Only call NEUTRAL if technicals and fundamentals are genuinely opposed, or a high-impact event is imminent
- Base your view on the full picture above — not any single indicator

THIS IS A DIRECTION ASSESSMENT ONLY.
Do not provide entry price, stop loss, or take profit at this stage.

Respond in EXACTLY this format — no extra text:
DIRECTION: [BUY/SELL/NEUTRAL]
CONFIDENCE: [0-100%]
NEWS IMPACT: [POSITIVE/NEGATIVE/NEUTRAL]
MACRO RISK: [HIGH/MEDIUM/LOW]
CALENDAR WARNING: [YES/NO]
TECHNICAL VIEW: [Your reading of structure, momentum, and indicator alignment on H1]
FUNDAMENTAL VIEW: [Your view on central bank policy, macro conditions, and sentiment for this pair]
REASON: [2-3 sentences combining all factors into your final assessment]"""

    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 900
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT ERROR: {str(e)}"


# ==========================================
# CHATGPT — CHALLENGE GEMINI
# ==========================================
def chatgpt_challenge(symbol, chatgpt_view, gemini_view):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    prompt = f"""You are an experienced market analyst. A peer analyst (Gemini) has a different view to yours on {symbol}.

YOUR VIEW:
{chatgpt_view}

GEMINI'S VIEW:
{gemini_view}

Write a focused challenge to Gemini. Be direct and specific:
- State the key data point or reasoning that supports your view
- Identify the specific weakness or gap in Gemini's argument
- Ask Gemini to justify or reconsider one specific aspect

Keep it under 4 sentences. Do not make a final trade call here — just challenge the logic."""

    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 350
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT CHALLENGE ERROR: {str(e)}"


# ==========================================
# GEMINI — DEFEND POSITION
# ==========================================
def gemini_defend(signal, news, macro, gemini_first, chatgpt_challenge_msg):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve  = signal.get("twelve_data", {})
    context = build_market_context(signal, news, macro, twelve, signal.get("calendar", {}))

    prompt = f"""You are an experienced market analyst defending your prior assessment.

YOUR ORIGINAL VIEW:
{gemini_first}

CHALLENGE FROM PEER ANALYST:
{chatgpt_challenge_msg}

ORIGINAL MARKET DATA (for reference):
{context}

Respond clearly and concisely. Defend your position with specific data points from the market context above.
If the challenge raises a valid point, acknowledge it but explain why your conclusion still holds.
Maximum 5 sentences.

Respond in EXACTLY this format:
DEFENDING DIRECTION: [BUY/SELL/NEUTRAL]
KEY REASON 1: [strongest supporting argument]
KEY REASON 2: [second supporting argument]
KEY REASON 3: [response to the challenge specifically]
SUMMARY: [one sentence — your final defended stance]"""

    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": 500}
                },
                timeout=30
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini defend attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Could not defend"


# ==========================================
# CHATGPT — FINAL DECISION (ENTRY + SL + TP)
# Called after debate OR after agreement.
# This is the ONLY place SL/TP are produced.
# ==========================================
def chatgpt_final(symbol, price, signal, chatgpt_view, gemini_view,
                  challenge_msg=None, gemini_defense=None, agreed=False, agreed_direction=None):
    """
    agreed=True  → both agreed in Round 1, skip debate recap
    agreed=False → called after debate, include full exchange
    SL and TP are ONLY produced here — not in Round 1.
    """
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    context  = build_market_context(signal, {}, {}, twelve, calendar)

    if agreed:
        context_block = f"""Both analysts independently assessed {agreed_direction} on {symbol}.

Your own Round 1 analysis:
{chatgpt_view}

Gemini's independent Round 1 analysis:
{gemini_view}

Both analyses agree. Now produce the final execution parameters."""
    else:
        context_block = f"""After debate, you must now make the final call on {symbol}.

YOUR Round 1 analysis:
{chatgpt_view}

GEMINI Round 1 analysis:
{gemini_view}

Your challenge to Gemini:
{challenge_msg}

Gemini's defense:
{gemini_defense}

You have heard both sides. Make the final decision."""

    prompt = f"""You are a senior execution trader making the final trade decision.

SYMBOL: {symbol}
CURRENT PRICE: {price}
TIMEFRAME: H1

{context_block}

MARKET DATA SUMMARY:
{context}

EXECUTION RULES:
- If FINAL DIRECTION is BUY: ENTRY = {price}, SL must be below {price}, TP must be above {price}
- If FINAL DIRECTION is SELL: ENTRY = {price}, SL must be above {price}, TP must be below {price}
- If FINAL DIRECTION is NEUTRAL: ENTRY, STOP LOSS, TAKE PROFIT, RISK:REWARD all = N/A
- Use ATR from the signal data for SL/TP sizing where available
- Do not write Unknown or leave any field blank

Respond in EXACTLY this format — no extra text:
FINAL DIRECTION: [BUY/SELL/NEUTRAL]
ENTRY: [{price} or N/A]
STOP LOSS: [number or N/A]
TAKE PROFIT: [number or N/A]
RISK:REWARD: [1:X or N/A]
CONFIDENCE: [0-100%]
TECHNICAL VIEW: [key H1 technical factors driving this decision]
FUNDAMENTAL VIEW: [key macro/news factors supporting or opposing]
WHY THIS DECISION: [2-3 sentences — final reasoning combining both analysts' views]"""

    try:
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 700
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT FINAL ERROR: {str(e)}"


# ==========================================
# TELEGRAM MESSAGE FORMATTERS
# ==========================================
def fmt_direction_line(direction):
    if direction == "BUY":
        return "🟢 BUY"
    elif direction == "SELL":
        return "🔴 SELL"
    return "⚪ NEUTRAL"


def build_final_telegram(symbol, price, direction, entry, sl, tp, rr,
                         confidence, tech_view, fund_view, reason,
                         gemini_dir, chatgpt_dir, path, session):
    """
    Builds the clean final Telegram message.
    path: "AGREED" / "DEBATE" / "SOLO"
    """
    dir_line  = fmt_direction_line(direction)
    path_tag  = {"AGREED": "✅ Both Agreed", "DEBATE": "⚔️ After Debate", "SOLO": "🤖 ChatGPT Only"}.get(path, path)
    session_u = session.upper()

    sl_rr_line = (
        f"Entry:       {entry}\n"
        f"Stop Loss:   {sl}\n"
        f"Take Profit: {tp}\n"
        f"Risk:Reward: {rr}"
    ) if direction in ("BUY", "SELL") else "Levels:      N/A"

    msg = f"""{dir_line} | {symbol} | {session_u}
━━━━━━━━━━━━━━━━━━━━━━━━━
Decision:    {path_tag}
Confidence:  {confidence}
Gemini:      {gemini_dir}  |  ChatGPT: {chatgpt_dir}
━━━━━━━━━━━━━━━━━━━━━━━━━
{sl_rr_line}
━━━━━━━━━━━━━━━━━━━━━━━━━
📈 TECHNICAL:
{tech_view}

🌐 FUNDAMENTAL:
{fund_view}

🎯 REASON:
{reason}
━━━━━━━━━━━━━━━━━━━━━━━━━
Price: {price} | H1 | {datetime.now(timezone.utc).strftime('%H:%M UTC')}"""
    return msg


def send_neutral(symbol, confidence, reason, session, gemini_dir, chatgpt_dir):
    session_u = session.upper() if session != "off" else "SESSION"
    send_telegram(
        f"⚪ NEUTRAL | {symbol} | {session_u}\n"
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
        f"GEMINI VIEW:\n{gemini_text[:500]}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CHATGPT VIEW:\n{chatgpt_text[:500]}"
    )


# ==========================================
# WEBHOOK — MAIN PIPELINE
# ==========================================
@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Webhook active. POST trading signals to /webhook", 200

    try:
        request_json = request.get_json(silent=True, force=True)
        request_text = request.get_data(as_text=True)
        print(f"Incoming: {request_text[:300]}")

        data = parse_incoming_data(request_json, request_text)
        if not data:
            send_telegram("❌ No parseable data received from TradingView")
            return "No data", 400

        # ── Field Extraction ─────────────────────────────────────────
        symbol    = clean_symbol(safe_str(data.get("symbol") or data.get("ticker"), "Unknown"))
        price     = safe_float(data.get("price") or data.get("close") or 0)
        raw_tf    = safe_str(data.get("tf") or data.get("timeframe") or data.get("interval"), "1H")
        data["tf"] = "1H"
        data["timeframe"] = "1H"

        direction  = safe_str(data.get("direction"), "N/A")
        score      = safe_str(data.get("score"), "N/A")
        adx        = safe_str(data.get("adx"), "N/A")
        structure  = safe_str(data.get("structure_bias") or data.get("structure"), "N/A")
        atr        = safe_str(data.get("atr"), "N/A")
        ema_21     = safe_str(data.get("ema_21"), "N/A")
        ema_50     = safe_str(data.get("ema_50"), "N/A")
        ema_200    = safe_str(data.get("ema_200"), "N/A")
        volume     = safe_str(data.get("volume_ratio"), "N/A")
        validator  = safe_str(data.get("validator"), "N/A")
        confidence = safe_str(data.get("confidence"), "N/A")
        signal_ea  = safe_int(data.get("ea_score") or data.get("ea_filter") or 0)

        print(f"{symbol} | {price} | {direction} | Score:{score} | EA:{signal_ea}")

        # ══════════════════════════════════════════════════════════════
        # GATE 1 — SESSION & FREQUENCY CHECK
        # Validates: trading hours, max 2 trades/session, 2hr cooldown
        # ══════════════════════════════════════════════════════════════
        allowed, gate_reason, session = session_gate(symbol)
        if not allowed:
            # Silent block — no telegram noise for routine blocks
            print(f"BLOCKED: {symbol} | {gate_reason}")
            return "Blocked", 200

        # ── Signal received ──────────────────────────────────────────
        send_telegram(
            f"📡 SIGNAL IN | {symbol} | {session.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Price:      {price}     ATR: {atr}\n"
            f"Direction:  {direction}    Score: {score}/100\n"
            f"Structure:  {structure}    ADX: {adx}\n"
            f"EA Filter:  {signal_ea}/8      Volume: {volume}\n"
            f"EMA:        {ema_21} / {ema_50} / {ema_200}\n"
            f"Validator:  {validator}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Fetching market data..."
        )

        # ══════════════════════════════════════════════════════════════
        # GATE 2 — PARALLEL LIVE DATA FETCH
        # Runs Finnhub + GDELT + Calendar simultaneously
        # ══════════════════════════════════════════════════════════════
        news, macro, calendar = fetch_all_live_data(symbol)

        # ══════════════════════════════════════════════════════════════
        # GATE 3 — TWELVE DATA (sequential, rate-limited)
        # Quality flag added: FULL / PARTIAL / POOR
        # ══════════════════════════════════════════════════════════════
        twelve_data = fetch_twelve_data(symbol)
        data["twelve_data"] = twelve_data
        data["calendar"]    = calendar

        twelve_quality = twelve_data.get("_quality", "Unknown")
        cal_line = (
            f"⚠️ HIGH IMPACT: {', '.join(calendar.get('events', []))}"
            if calendar.get("high_impact_soon") else "Calendar: Clear"
        )

        send_telegram(
            f"📊 MARKET DATA | {symbol}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"News:      {news['sentiment']} ({len(news.get('headlines', []))} headlines)\n"
            f"Macro:     {macro['risk']}\n"
            f"{cal_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"INDICATORS ({twelve_quality}):\n"
            f"RSI: {twelve_data.get('rsi','N/A')}  "
            f"MACD: {twelve_data.get('macd','N/A')}  "
            f"CCI: {twelve_data.get('cci','N/A')}\n"
            f"Stoch: {twelve_data.get('stoch_k','N/A')}/{twelve_data.get('stoch_d','N/A')}  "
            f"MFI: {twelve_data.get('mfi','N/A')}  "
            f"WR: {twelve_data.get('willr','N/A')}\n"
            f"Aroon: {twelve_data.get('aroon_up','N/A')}↑ {twelve_data.get('aroon_down','N/A')}↓  "
            f"PSAR: {twelve_data.get('psar','N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Checking market change score..."
        )

        # ══════════════════════════════════════════════════════════════
        # GATE 3.5 — MARKET CHANGE SCORE (MCS) — AUTO-NEUTRAL FILTER
        #
        # Compares current signal to the last cached signal for this pair.
        # If market conditions have not changed enough since the last
        # NEUTRAL outcome, skip AI entirely and auto-fire neutral.
        #
        # Zero AI tokens consumed. Response in <100ms instead of 30s.
        #
        # Veto conditions that ALWAYS bypass this gate and run AI:
        #   - No previous neutral cached for this pair/session
        #   - Last outcome was a BUY or SELL (not neutral)
        #   - Cache is older than CACHE_EXPIRY_MINUTES (default 30m)
        #   - High-impact calendar event detected
        #   - Price moved more than PRICE_VETO_PCT (default 0.30%)
        #   - Auto-neutral streak >= MAX_AUTO_NEUTRAL_STREAK (default 4)
        #   - MCS >= MCS_THRESHOLD (default 15.0%)
        # ══════════════════════════════════════════════════════════════
        skip_ai, mcs_reason, mcs_score = should_auto_neutral(
            symbol, session, data, twelve_data, calendar
        )

        if skip_ai:
            # ── AUTO-NEUTRAL: no AI calls, instant response ──────────
            session_u = session.upper()
            cached_key = f"{clean_symbol(symbol)}_{session}"
            streak = neutral_cache.get(cached_key, {}).get("auto_count", 0) + 1

            send_telegram(
                f"⚪ NEUTRAL | {symbol} | {session_u}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Mode:       Auto (no AI)\n"
                f"MCS:        {mcs_score:.1f}% (threshold: {MCS_THRESHOLD}%)\n"
                f"Streak:     {streak}/{MAX_AUTO_NEUTRAL_STREAK}\n"
                f"Reason:     {mcs_reason}"
            )

            # Update cache — increment streak
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            increment_auto_streak(symbol, session)
            return "OK", 200

        # MCS gate cleared — reset streak and run full AI pipeline
        reset_auto_streak(symbol, session)

        send_telegram(
            f"🔄 MCS: {mcs_score:.1f}% ≥ {MCS_THRESHOLD}% | {mcs_reason}\n"
            f"⏳ Running independent AI analysis..."
        )

        # ══════════════════════════════════════════════════════════════
        # GATE 4 — PARALLEL INDEPENDENT AI ANALYSIS (Round 1)
        # Gemini and ChatGPT run simultaneously and BLINDLY
        # Neither receives the other's output in this round
        # ══════════════════════════════════════════════════════════════
        with ThreadPoolExecutor(max_workers=2) as ex:
            fg = ex.submit(gemini_analysis, data, news, macro)
            fc = ex.submit(chatgpt_analysis, symbol, price, data, news, macro, calendar)
            gemini  = fg.result()
            chatgpt = fc.result()

        # Handle total AI failure
        if "GEMINI UNAVAILABLE" in gemini and "CHATGPT ERROR" in chatgpt:
            send_telegram(f"❌ Both AI systems failed for {symbol}. Signal aborted.")
            return "AI failure", 500

        # ══════════════════════════════════════════════════════════════
        # GATE 5 — DIRECTION PARSING (regex label-only extraction)
        # Prevents "do NOT BUY" from being parsed as BUY
        # ══════════════════════════════════════════════════════════════
        gemini_dir  = extract_direction(gemini)  if "GEMINI UNAVAILABLE" not in gemini  else "UNAVAILABLE"
        chatgpt_dir = extract_direction(chatgpt) if "CHATGPT ERROR"      not in chatgpt else "UNAVAILABLE"

        print(f"Directions — Gemini: {gemini_dir} | ChatGPT: {chatgpt_dir}")

        # ── Gemini-only fallback ─────────────────────────────────────
        if "CHATGPT ERROR" in chatgpt:
            send_telegram(f"⚠️ ChatGPT unavailable. Gemini-only analysis for {symbol}.")
            # Cannot produce SL/TP without ChatGPT final — abort
            send_telegram(f"❌ Cannot produce execution parameters without ChatGPT. Signal skipped.")
            return "ChatGPT unavailable", 200

        # ── Gemini unavailable — ChatGPT solo ───────────────────────
        if "GEMINI UNAVAILABLE" in gemini:
            send_telegram(f"⚠️ Gemini unavailable. Proceeding with ChatGPT only.")
            gemini_dir = "UNAVAILABLE"

            if chatgpt_dir in ("BUY", "SELL"):
                final_out = chatgpt_final(
                    symbol, price, data, chatgpt, "Gemini unavailable",
                    agreed=True, agreed_direction=chatgpt_dir
                )
                final_dir = extract_direction(final_out)
                sl = extract_sl(final_out)
                tp = extract_tp(final_out)
                rr = extract_rr(final_out)
                conf = extract_confidence(final_out)
                tech = extract_multiline_field(final_out, "TECHNICAL VIEW")
                fund = extract_multiline_field(final_out, "FUNDAMENTAL VIEW")
                reason = extract_multiline_field(final_out, "WHY THIS DECISION")

                sl_valid, sl_msg = validate_levels(final_dir, price, sl, tp)
                if not sl_valid:
                    send_telegram(f"⚠️ SL/TP INVALID ({symbol}): {sl_msg}\nSignal aborted.")
                    return "Invalid levels", 200

                msg = build_final_telegram(
                    symbol, price, final_dir, price, sl, tp, rr,
                    conf, tech, fund, reason,
                    "N/A", chatgpt_dir, "SOLO", session
                )
                send_telegram(msg)
                register_trade(symbol, session)
            else:
                reason = extract_neutral_reason(gemini, chatgpt, calendar)
                conf   = extract_confidence(chatgpt)
                send_neutral(symbol, conf, reason, session, "UNAVAILABLE", chatgpt_dir)
                send_neutral_audit(symbol, "UNAVAILABLE", chatgpt, reason)
                update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return "OK", 200

        # ══════════════════════════════════════════════════════════════
        # GATE 6 — AGREEMENT CHECK
        # NEUTRAL requires BOTH to agree → only then skip trade
        # One NEUTRAL → soft disagreement → enters debate
        # ══════════════════════════════════════════════════════════════

        # ── Case A: BOTH NEUTRAL ─────────────────────────────────────
        if gemini_dir == "NEUTRAL" and chatgpt_dir == "NEUTRAL":
            reason = extract_neutral_reason(gemini, chatgpt, calendar)
            conf   = extract_confidence(chatgpt)
            send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
            send_neutral_audit(symbol, gemini, chatgpt, reason)
            update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
            return "OK", 200

        # ── Case B: FULL AGREEMENT (BUY+BUY or SELL+SELL) ────────────
        elif gemini_dir == chatgpt_dir and gemini_dir in ("BUY", "SELL"):
            agreed_dir = gemini_dir
            send_telegram(
                f"✅ AGREEMENT | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Gemini:  {gemini_dir}\n"
                f"ChatGPT: {chatgpt_dir}\n"
                f"Both agree independently. Producing execution parameters..."
            )

            # ════════════════════════════════════════════════════════
            # GATE 7 — FINAL EXECUTION (SL/TP ONLY HERE)
            # ════════════════════════════════════════════════════════
            final_out = chatgpt_final(
                symbol, price, data, chatgpt, gemini,
                agreed=True, agreed_direction=agreed_dir
            )
            final_dir = extract_direction(final_out)
            sl   = extract_sl(final_out)
            tp   = extract_tp(final_out)
            rr   = extract_rr(final_out)
            conf = extract_confidence(final_out)
            tech = extract_multiline_field(final_out, "TECHNICAL VIEW")
            fund = extract_multiline_field(final_out, "FUNDAMENTAL VIEW")
            reason = extract_multiline_field(final_out, "WHY THIS DECISION")

            # ════════════════════════════════════════════════════════
            # GATE 8 — SL/TP SANITY VALIDATION
            # Confirms levels are on correct side of entry
            # ════════════════════════════════════════════════════════
            sl_valid, sl_msg = validate_levels(final_dir, price, sl, tp)
            if not sl_valid:
                send_telegram(
                    f"⚠️ SL/TP INVALID | {symbol}\n"
                    f"Direction: {final_dir} | Entry: {price}\n"
                    f"SL: {sl} | TP: {tp}\n"
                    f"Issue: {sl_msg}\n"
                    f"Signal aborted."
                )
                return "Invalid levels", 200

            gemini_tech  = extract_multiline_field(gemini, "TECHNICAL VIEW")
            chatgpt_tech = extract_multiline_field(chatgpt, "TECHNICAL VIEW")
            combined_tech = f"Gemini: {gemini_tech}\nChatGPT: {chatgpt_tech}"

            msg = build_final_telegram(
                symbol, price, final_dir, price, sl, tp, rr,
                conf, combined_tech, fund, reason,
                gemini_dir, chatgpt_dir, "AGREED", session
            )
            send_telegram(msg)
            register_trade(symbol, session)
            update_neutral_cache(symbol, session, final_dir, data, twelve_data)
            return "OK", 200

        # ── Case C: DISAGREEMENT or ONE NEUTRAL ──────────────────────
        else:
            send_telegram(
                f"⚔️ DEBATE | {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Gemini:  {gemini_dir}\n"
                f"ChatGPT: {chatgpt_dir}\n"
                f"Initiating debate round..."
            )

            # ════════════════════════════════════════════════════════
            # GATE 9 — DEBATE ROUND
            # ChatGPT challenges → Gemini defends → ChatGPT final verdict
            # ════════════════════════════════════════════════════════
            challenge    = chatgpt_challenge(symbol, chatgpt, gemini)
            gemini_reply = gemini_defend(data, news, macro, gemini, challenge)

            # Final decision after debate — SL/TP produced here
            final_out = chatgpt_final(
                symbol, price, data, chatgpt, gemini,
                challenge_msg=challenge,
                gemini_defense=gemini_reply,
                agreed=False
            )
            final_dir = extract_direction(final_out)

            # ════════════════════════════════════════════════════════
            # GATE 10 — POST-DEBATE NEUTRAL
            # ════════════════════════════════════════════════════════
            if final_dir == "NEUTRAL":
                reason = extract_neutral_reason(gemini, chatgpt, calendar)
                conf   = extract_confidence(final_out)
                send_neutral(symbol, conf, reason, session, gemini_dir, chatgpt_dir)
                send_neutral_audit(symbol, gemini, chatgpt, reason, post_debate=True)
                update_neutral_cache(symbol, session, "NEUTRAL", data, twelve_data)
                return "OK", 200

            sl   = extract_sl(final_out)
            tp   = extract_tp(final_out)
            rr   = extract_rr(final_out)
            conf = extract_confidence(final_out)
            tech = extract_multiline_field(final_out, "TECHNICAL VIEW")
            fund = extract_multiline_field(final_out, "FUNDAMENTAL VIEW")
            reason = extract_multiline_field(final_out, "WHY THIS DECISION")

            # ════════════════════════════════════════════════════════
            # GATE 11 — SL/TP VALIDATION (post-debate)
            # ════════════════════════════════════════════════════════
            sl_valid, sl_msg = validate_levels(final_dir, price, sl, tp)
            if not sl_valid:
                send_telegram(
                    f"⚠️ SL/TP INVALID (Post-Debate) | {symbol}\n"
                    f"Direction: {final_dir} | Entry: {price}\n"
                    f"SL: {sl} | TP: {tp}\n"
                    f"Issue: {sl_msg}\n"
                    f"Signal aborted."
                )
                return "Invalid levels", 200

            msg = build_final_telegram(
                symbol, price, final_dir, price, sl, tp, rr,
                conf, tech, fund, reason,
                gemini_dir, chatgpt_dir, "DEBATE", session
            )
            send_telegram(msg)
            register_trade(symbol, session)
            update_neutral_cache(symbol, session, final_dir, data, twelve_data)
            return "OK", 200

    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        print(error_msg)
        send_telegram(f"❌ System Error | {error_msg}")
        return error_msg, 500


# ==========================================
# HEALTH CHECK
# ==========================================
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/", methods=["GET"])
def root():
    return "Trading Bot running. POST signals to /webhook", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
