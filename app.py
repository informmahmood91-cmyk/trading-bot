import json
import os
import time
import requests
from flask import Flask, request
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# ==========================================
# ENV VARIABLES
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
CHATGPT_KEY = os.environ.get("CHATGPT_KEY")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY")
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY")

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
# UTILITIES
# ==========================================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing Telegram config")
        return
    if len(msg) > 4000:
        msg = msg[:4000]
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")


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
# DAILY RESET
# ==========================================
def reset_day():
    global current_day, pair_session_tracker
    today = datetime.now(timezone.utc).date()
    if current_day != today:
        current_day = today
        pair_session_tracker = {}  # Clear all trades
        print(f"Day reset: {today}")
# ==========================================
# TIME BASED SESSION DETECTION
# ==========================================
# Store trades with timestamps instead of just True
pair_session_tracker = {}  # Format: {key: [timestamp1, timestamp2]}

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
    
    key = f"{symbol}_{session}"
    now = datetime.now(timezone.utc)
    
    # Get existing timestamps for this symbol/session
    timestamps = pair_session_tracker.get(key, [])
    
    # Check if already has 2 trades
    if len(timestamps) >= 2:
        return False, f"Already traded {symbol} 2 times in {session} today", session
    
    # Check if last trade was within 1 hour (3600 seconds)
    if len(timestamps) == 1:
        last_trade_time = timestamps[0]
        time_diff = (now - last_trade_time).total_seconds()
        if time_diff < 3600:
            remaining = int(3600 - time_diff)
            return False, f"Last trade {symbol} in {session} was {remaining} seconds ago. Need 1 hour gap", session
    
    return True, "OK", session


def register_trade(symbol, session):
    global pair_session_tracker
    key = f"{symbol}_{session}"
    now = datetime.now(timezone.utc)
    
    # Get existing timestamps
    timestamps = pair_session_tracker.get(key, [])
    
    # Add new timestamp
    timestamps.append(now)
    
    # Keep only last 2 timestamps
    if len(timestamps) > 2:
        timestamps = timestamps[-2:]
    
    pair_session_tracker[key] = timestamps
    print(f"Trade registered: {symbol} {session} at {now}. Total trades: {len(timestamps)}/2")

# ==========================================
# FINNHUB — SYMBOL SPECIFIC NEWS (UPGRADE 1)
# ==========================================
def fetch_finnhub(symbol):
    if not FINNHUB_KEY:
        return {"sentiment": "NEUTRAL", "text": "No Finnhub key", "headlines": []}

    # Clean symbol — remove broker prefix like FX: or BINANCE:
    symbol = symbol.upper().replace("FX:", "").replace("OANDA:", "").replace("BINANCE:", "").strip()

    # Symbol search keyword mapping
    SYMBOL_KEYWORDS = {
        # Forex majors
        "EURUSD": ["EUR", "Euro", "ECB", "European"],
        "GBPUSD": ["GBP", "Pound", "Sterling", "BOE"],
        "USDJPY": ["JPY", "Yen", "BOJ", "Japan"],
        "USDCHF": ["CHF", "Franc", "SNB", "Swiss"],
        "AUDUSD": ["AUD", "Aussie", "RBA", "Australia"],
        "NZDUSD": ["NZD", "Kiwi", "RBNZ", "New Zealand"],
        "USDCAD": ["CAD", "Loonie", "BOC", "Canada"],

        # Forex crosses
        "GBPJPY": ["GBP", "Pound", "JPY", "Yen", "BOE", "BOJ"],
        "EURJPY": ["EUR", "Euro", "JPY", "Yen", "ECB", "BOJ"],
        "EURGBP": ["EUR", "Euro", "GBP", "Pound", "ECB", "BOE"],
        "GBPCHF": ["GBP", "Pound", "CHF", "Franc"],
        "AUDCAD": ["AUD", "Aussie", "CAD", "Canada"],
        "AUDNZD": ["AUD", "Aussie", "NZD", "Kiwi"],
        "CADJPY": ["CAD", "Canada", "JPY", "Yen"],
        "CHFJPY": ["CHF", "Swiss", "JPY", "Yen"],

        # Gold and Silver
        "XAUUSD": ["Gold", "XAU", "bullion", "precious metals", "Fed"],
        "XAGUSD": ["Silver", "XAG", "precious metals"],
        "GOLD":   ["Gold", "XAU", "bullion", "precious metals"],

        # Oil
        "USOIL":  ["Oil", "WTI", "crude", "OPEC", "energy"],
        "UKOIL":  ["Oil", "Brent", "crude", "OPEC", "energy"],
        "XTIUSD": ["Oil", "WTI", "crude", "OPEC"],
        "XBRUSD": ["Oil", "Brent", "crude", "OPEC"],

        # Indices
        "US30":   ["Dow Jones", "DJIA", "Wall Street"],
        "DJ30":   ["Dow Jones", "DJIA", "Wall Street"],
        "NAS100": ["Nasdaq", "tech stocks", "S&P"],
        "SPX500": ["S&P 500", "SPX", "Wall Street"],
        "UK100":  ["FTSE", "UK stocks", "London"],
        "GER40":  ["DAX", "Germany", "German stocks"],

        # Crypto
        "BTCUSD": ["Bitcoin", "BTC", "crypto"],
        "ETHUSD": ["Ethereum", "ETH", "crypto"],
        "BTCUSDT":["Bitcoin", "BTC", "crypto"],
    }

    # Get keywords for this symbol
    keywords = SYMBOL_KEYWORDS.get(symbol, [])

    # If not in mapping try to build from symbol name
    if not keywords and len(symbol) == 6:
        base  = symbol[:3]
        quote = symbol[3:]
        keywords = [base, quote]
    elif not keywords:
        keywords = [symbol]

    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        all_news = r.json()[:30]

        # Filter headlines that contain any of our keywords
        matched_headlines = []
        for n in all_news:
            headline = n.get("headline", "")
            if any(kw.lower() in headline.lower() for kw in keywords):
                matched_headlines.append(headline)

        # Fallback to general news if nothing found
        if not matched_headlines:
            print(f"No symbol specific news for {symbol} — using general news")
            matched_headlines = [n.get("headline", "") for n in all_news[:5]]

        text = " ".join(matched_headlines).lower()
        bull = sum(w in text for w in ["rise", "bull", "gain", "up", "growth", "hawkish", "strong"])
        bear = sum(w in text for w in ["fall", "bear", "drop", "crash", "recession", "dovish", "weak"])
        sentiment = "POSITIVE" if bull > bear else "NEGATIVE" if bear > bull else "NEUTRAL"

        print(f"Finnhub: {symbol} | Keywords: {keywords} | Headlines: {len(matched_headlines)} | Sentiment: {sentiment}")

        return {
            "sentiment": sentiment,
            "text": text[:500],
            "headlines": matched_headlines[:5],
            "symbol_matched": len(matched_headlines) > 0
        }

    except Exception as e:
        print(f"Finnhub error: {e}")
        return {"sentiment": "NEUTRAL", "text": "News unavailable", "headlines": [], "symbol_matched": False}
# ==========================================
# ECONOMIC CALENDAR (UPGRADE 2)
# ==========================================
def fetch_economic_calendar():
    if not FINNHUB_KEY:
        return {"high_impact_soon": False, "events": []}
    try:
        now = datetime.now(timezone.utc)
        from_date = now.strftime("%Y-%m-%d")
        to_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        url = f"https://finnhub.io/api/v1/calendar/economic?from={from_date}&to={to_date}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        events = r.json().get("economicCalendar", [])

        high_impact = [e for e in events if e.get("impact") == "high"]

        print(f"Economic calendar: {len(high_impact)} high impact events")
        return {
            "high_impact_soon": len(high_impact) > 0,
            "events": [e.get("event", "") for e in high_impact[:3]]
        }
    except Exception as e:
        print(f"Calendar error: {e}")
        return {"high_impact_soon": False, "events": []}


# ==========================================
# GDELT — LIVE MACRO
# ==========================================
def fetch_gdelt():
    try:
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=global%20economy&mode=ArtList&format=json"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        articles = r.json().get("articles", [])[:5]
        risk_words = ["war", "inflation", "crisis", "recession", "sanctions"]
        score = 0
        for a in articles:
            if any(w in a.get("title", "").lower() for w in risk_words):
                score += 1
        risk = "HIGH" if score >= 3 else "MEDIUM" if score >= 1 else "LOW"
        print(f"GDELT macro risk: {risk}")
        return {"risk": risk, "score": score}
    except Exception as e:
        print(f"GDELT error: {e}")
        return {"risk": "LOW", "score": 0}


# ==========================================
# TWELVE DATA — 10 TECHNICAL INDICATORS
# ==========================================
def fetch_twelve_data(symbol):
    if not TWELVE_DATA_KEY:
        print("Twelve Data: No API key")
        return {}

    if "/" not in symbol and len(symbol) == 6:
        symbol = f"{symbol[:3]}/{symbol[3:]}"

    base_url = "https://api.twelvedata.com"
    api_key = TWELVE_DATA_KEY

    indicators = {
        "rsi":      f"{base_url}/rsi?symbol={symbol}&interval=1h&time_period=14&apikey={api_key}",
        "macd":     f"{base_url}/macd?symbol={symbol}&interval=1h&apikey={api_key}",
        "stoch":    f"{base_url}/stoch?symbol={symbol}&interval=1h&apikey={api_key}",
        "cci":      f"{base_url}/cci?symbol={symbol}&interval=1h&time_period=20&apikey={api_key}",
        "mfi":      f"{base_url}/mfi?symbol={symbol}&interval=1h&time_period=14&apikey={api_key}",
        "willr":    f"{base_url}/willr?symbol={symbol}&interval=1h&time_period=14&apikey={api_key}",
        "obv":      f"{base_url}/obv?symbol={symbol}&interval=1h&apikey={api_key}",
        "aroon":    f"{base_url}/aroon?symbol={symbol}&interval=1h&time_period=25&apikey={api_key}",
        "ichimoku": f"{base_url}/ichimoku?symbol={symbol}&interval=1h&apikey={api_key}",
        "psar":     f"{base_url}/psar?symbol={symbol}&interval=1h&apikey={api_key}",
    }

    results = {}
    for name, url in indicators.items():
        try:
            resp = requests.get(url, timeout=8).json()
            if resp.get("values"):
                results[name] = resp["values"][0]
            elif resp.get("value"):
                results[name] = resp["value"]
            else:
                results[name] = "N/A"
        except Exception as e:
            print(f"Twelve Data {name} error: {e}")
            results[name] = "N/A"
        time.sleep(0.1)

    return {
        "rsi": results.get("rsi", {}).get("rsi", "N/A") if isinstance(results.get("rsi"), dict) else results.get("rsi", "N/A"),
        "macd": results.get("macd", {}).get("macd", "N/A") if isinstance(results.get("macd"), dict) else results.get("macd", "N/A"),
        "macd_signal": results.get("macd", {}).get("macd_signal", "N/A") if isinstance(results.get("macd"), dict) else "N/A",
        "macd_histogram": results.get("macd", {}).get("macd_histogram", "N/A") if isinstance(results.get("macd"), dict) else "N/A",
        "stoch_k": results.get("stoch", {}).get("slowk", "N/A") if isinstance(results.get("stoch"), dict) else results.get("stoch", "N/A"),
        "stoch_d": results.get("stoch", {}).get("slowd", "N/A") if isinstance(results.get("stoch"), dict) else "N/A",
        "cci": results.get("cci", {}).get("cci", "N/A") if isinstance(results.get("cci"), dict) else results.get("cci", "N/A"),
        "mfi": results.get("mfi", {}).get("mfi", "N/A") if isinstance(results.get("mfi"), dict) else results.get("mfi", "N/A"),
        "willr": results.get("willr", {}).get("willr", "N/A") if isinstance(results.get("willr"), dict) else results.get("willr", "N/A"),
        "obv": results.get("obv", {}).get("obv", "N/A") if isinstance(results.get("obv"), dict) else results.get("obv", "N/A"),
        "aroon_up": results.get("aroon", {}).get("aroon_up", "N/A") if isinstance(results.get("aroon"), dict) else "N/A",
        "aroon_down": results.get("aroon", {}).get("aroon_down", "N/A") if isinstance(results.get("aroon"), dict) else "N/A",
        "ichimoku_conversion": results.get("ichimoku", {}).get("tenkan_sen", "N/A") if isinstance(results.get("ichimoku"), dict) else "N/A",
        "ichimoku_base": results.get("ichimoku", {}).get("kijun_sen", "N/A") if isinstance(results.get("ichimoku"), dict) else "N/A",
        "ichimoku_span_a": results.get("ichimoku", {}).get("senkou_span_a", "N/A") if isinstance(results.get("ichimoku"), dict) else "N/A",
        "ichimoku_span_b": results.get("ichimoku", {}).get("senkou_span_b", "N/A") if isinstance(results.get("ichimoku"), dict) else "N/A",
        "psar": results.get("psar", {}).get("psar", "N/A") if isinstance(results.get("psar"), dict) else results.get("psar", "N/A"),
    }


# ==========================================
# GEMINI — FULL ANALYSIS (WITH RETRY)
# ==========================================
def gemini_analysis(signal, news, macro):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    signal_clean = {k: v for k, v in signal.items() if k not in ["twelve_data", "calendar"]}

    cal_warning = ""
    if calendar.get("high_impact_soon"):
        cal_warning = f"WARNING — HIGH IMPACT EVENTS TODAY: {', '.join(calendar.get('events', []))}\n"

    prompt = (
        f"You are a professional MARKET ANALYST with deep knowledge of global markets.\n\n"
        f"Analyze ALL of the following data sources together:\n\n"
        f"1. TRADINGVIEW SIGNAL:\n{json.dumps(signal_clean, indent=2)}\n\n"
        f"2. LIVE NEWS (Finnhub — Symbol Specific):\n"
        f"   Sentiment: {news.get('sentiment', 'N/A')}\n"
        f"   Headlines: {json.dumps(news.get('headlines', []), indent=2)}\n\n"
        f"3. MACRO RISK (GDELT):\n{json.dumps(macro, indent=2)}\n\n"
        f"4. ECONOMIC CALENDAR:\n"
        f"   {cal_warning if cal_warning else 'No high impact events today'}\n\n"
        f"5. TWELVE DATA LIVE TECHNICAL INDICATORS:\n"
        f"   RSI: {twelve.get('rsi', 'N/A')}\n"
        f"   MACD: {twelve.get('macd', 'N/A')} | Signal: {twelve.get('macd_signal', 'N/A')} | Histogram: {twelve.get('macd_histogram', 'N/A')}\n"
        f"   Stochastic K/D: {twelve.get('stoch_k', 'N/A')} / {twelve.get('stoch_d', 'N/A')}\n"
        f"   CCI: {twelve.get('cci', 'N/A')}\n"
        f"   MFI: {twelve.get('mfi', 'N/A')}\n"
        f"   Williams %R: {twelve.get('willr', 'N/A')}\n"
        f"   OBV: {twelve.get('obv', 'N/A')}\n"
        f"   Aroon Up/Down: {twelve.get('aroon_up', 'N/A')} / {twelve.get('aroon_down', 'N/A')}\n"
        f"   Ichimoku Conversion/Base: {twelve.get('ichimoku_conversion', 'N/A')} / {twelve.get('ichimoku_base', 'N/A')}\n"
        f"   Ichimoku Span A/B: {twelve.get('ichimoku_span_a', 'N/A')} / {twelve.get('ichimoku_span_b', 'N/A')}\n"
        f"   Parabolic SAR: {twelve.get('psar', 'N/A')}\n\n"
        f"6. YOUR OWN MARKET KNOWLEDGE:\n"
        f"   - Use your knowledge of this asset, current trends, correlations\n"
        f"   - Consider interest rates, geopolitical factors, market sentiment\n\n"
        f"TECHNICAL OVERRIDE RULES:\n"
        f"- If structure_bias is BEAR and direction is SHORT — do NOT recommend BUY\n"
        f"- If structure_bias is BULL and direction is LONG — do NOT recommend SELL\n"
        f"- Technical signal is PRIMARY — news and fundamentals are SUPPORTING only\n"
        f"- You may increase/decrease confidence based on news but cannot reverse technical direction\n"
        f"- Only recommend NEUTRAL if technical and fundamental are completely opposite\n"
        f"- If HIGH IMPACT EVENT is imminent — recommend NEUTRAL regardless of signal\n\n"
        f"STRICT RULES:\n"
        f"- Combine all 6 sources into one decision\n"
        f"- Never leave any field blank\n\n"
        f"Output EXACTLY:\n"
        f"DIRECTION: [BUY/SELL/NEUTRAL]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"NEWS IMPACT: [POSITIVE/NEGATIVE/NEUTRAL]\n"
        f"MACRO RISK: [HIGH/MEDIUM/LOW]\n"
        f"CALENDAR WARNING: [YES/NO — any high impact events]\n"
        f"TECHNICAL VIEW: [comment on RSI, MACD, Stochastic, structure and EMA alignment]\n"
        f"FUNDAMENTAL VIEW: [interest rate policy, central bank stance, economic factors]\n"
        f"REASON: [2-3 sentences combining all sources]"
    )
    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_KEY
                },
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Server busy, ChatGPT will decide alone"


# ==========================================
# GEMINI — DEFEND POSITION (WITH RETRY)
# ==========================================
def gemini_defend(signal, news, macro, gemini_first, chatgpt_question):
    if not GEMINI_KEY:
        return "GEMINI UNAVAILABLE: No API key"

    twelve = signal.get("twelve_data", {})
    signal_clean = {k: v for k, v in signal.items() if k not in ["twelve_data", "calendar"]}

    prompt = (
        f"You are a MARKET ANALYST. ChatGPT disagrees with your analysis.\n\n"
        f"YOUR ORIGINAL ANALYSIS:\n{gemini_first}\n\n"
        f"CHATGPT QUESTION/CHALLENGE:\n{chatgpt_question}\n\n"
        f"ORIGINAL DATA FOR REFERENCE:\n"
        f"Signal: {json.dumps(signal_clean, indent=2)}\n"
        f"Twelve Data: {json.dumps(twelve, indent=2)}\n"
        f"News Headlines: {json.dumps(news.get('headlines', []), indent=2)}\n"
        f"Macro: {json.dumps(macro, indent=2)}\n\n"
        f"Give ONE clear response explaining your reasoning.\n"
        f"Be concise and specific. Maximum 5 sentences.\n\n"
        f"Output EXACTLY:\n"
        f"DEFENDING DIRECTION: [BUY/SELL/NEUTRAL]\n"
        f"KEY REASON 1: [most important reason]\n"
        f"KEY REASON 2: [second reason]\n"
        f"KEY REASON 3: [third reason if any]\n"
        f"SUMMARY: [1 sentence final defense]"
    )
    for attempt in range(3):
        try:
            r = requests.post(
                GEMINI_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_KEY
                },
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini defend attempt {attempt+1} failed: {e}")
            if attempt < 2:
                time.sleep(3)
    return "GEMINI UNAVAILABLE: Could not defend position"


# ==========================================
# CHATGPT — FULL ANALYSIS + GEMINI REVIEW
# ==========================================
def chatgpt_analysis(symbol, price, timeframe, signal, news, macro, calendar, gemini_out):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve   = signal.get("twelve_data", {})
    calendar = signal.get("calendar", {})
    signal_clean = {k: v for k, v in signal.items() if k not in ["twelve_data", "calendar"]}

    cal_warning = ""
    if calendar.get("high_impact_soon"):
        cal_warning = f"WARNING — HIGH IMPACT EVENTS TODAY: {', '.join(calendar.get('events', []))}\n"

    prompt = (
        f"You are a SENIOR EXECUTION TRADER with deep market knowledge.\n\n"
        f"Analyze ALL of the following data sources:\n\n"
        f"1. TRADINGVIEW SIGNAL:\n{json.dumps(signal_clean, indent=2)}\n\n"
        f"2. LIVE NEWS (Finnhub — Symbol Specific):\n"
        f"   Sentiment: {news.get('sentiment', 'N/A')}\n"
        f"   Headlines: {json.dumps(news.get('headlines', []), indent=2)}\n\n"
        f"3. MACRO RISK (GDELT):\n{json.dumps(macro, indent=2)}\n\n"
        f"4. ECONOMIC CALENDAR:\n"
        f"   {cal_warning if cal_warning else 'No high impact events today'}\n\n"
        f"5. TWELVE DATA LIVE TECHNICAL INDICATORS:\n"
        f"   RSI: {twelve.get('rsi', 'N/A')}\n"
        f"   MACD: {twelve.get('macd', 'N/A')} | Signal: {twelve.get('macd_signal', 'N/A')} | Histogram: {twelve.get('macd_histogram', 'N/A')}\n"
        f"   Stochastic K/D: {twelve.get('stoch_k', 'N/A')} / {twelve.get('stoch_d', 'N/A')}\n"
        f"   CCI: {twelve.get('cci', 'N/A')}\n"
        f"   MFI: {twelve.get('mfi', 'N/A')}\n"
        f"   Williams %R: {twelve.get('willr', 'N/A')}\n"
        f"   OBV: {twelve.get('obv', 'N/A')}\n"
        f"   Aroon Up/Down: {twelve.get('aroon_up', 'N/A')} / {twelve.get('aroon_down', 'N/A')}\n"
        f"   Ichimoku Conversion/Base: {twelve.get('ichimoku_conversion', 'N/A')} / {twelve.get('ichimoku_base', 'N/A')}\n"
        f"   Ichimoku Span A/B: {twelve.get('ichimoku_span_a', 'N/A')} / {twelve.get('ichimoku_span_b', 'N/A')}\n"
        f"   Parabolic SAR: {twelve.get('psar', 'N/A')}\n\n"
        f"6. GEMINI ANALYST VIEW:\n{gemini_out}\n\n"
        f"7. YOUR OWN MARKET KNOWLEDGE:\n"
        f"   SYMBOL: {symbol}\n"
        f"   CURRENT PRICE: {price}\n"
        f"   TIMEFRAME: {timeframe}\n"
        f"   Use your own knowledge of this asset, interest rates,\n"
        f"   correlations, market structure and sentiment\n\n"
        f"TECHNICAL OVERRIDE RULES:\n"
        f"- If structure_bias is BEAR and direction is SHORT — do NOT recommend BUY\n"
        f"- If structure_bias is BULL and direction is LONG — do NOT recommend SELL\n"
        f"- Technical signal is PRIMARY — news and fundamentals are SUPPORTING only\n"
        f"- You may increase/decrease confidence based on news but cannot reverse technical direction\n"
        f"- Only recommend NEUTRAL if technical and fundamental are completely opposite\n"
        f"- If HIGH IMPACT EVENT is imminent — recommend NEUTRAL regardless of signal\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must be exactly: {price}\n"
        f"- STOP LOSS must be calculated from {price}\n"
        f"- TAKE PROFIT must be calculated from {price}\n"
        f"- If BUY: SL below price, TP above price\n"
        f"- If SELL: SL above price, TP below price\n"
        f"- Use ATR value from signal for SL/TP sizing if available\n"
        f"- Never write Unknown or blank\n\n"
        f"Output EXACTLY:\n"
        f"DIRECTION: [BUY/SELL/NEUTRAL]\n"
        f"AGREEMENT WITH GEMINI: [YES/NO]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"OWN REASONING: [2-3 sentences from your own analysis]\n"
        f"GEMINI COMPARISON: [1 sentence on why you agree or disagree]"
    )
    try:
        r = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {CHATGPT_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 1500
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT ERROR: {str(e)}"


# ==========================================
# CHATGPT — CHALLENGE GEMINI (1 MESSAGE)
# ==========================================
def chatgpt_challenge(symbol, price, chatgpt_view, gemini_view):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"
    prompt = (
        f"You are a SENIOR TRADER. You disagree with Gemini analyst.\n\n"
        f"YOUR ANALYSIS:\n{chatgpt_view}\n\n"
        f"GEMINI ANALYSIS:\n{gemini_view}\n\n"
        f"Send Gemini ONE clear challenge message explaining:\n"
        f"- Why you disagree\n"
        f"- What specific data points support your view\n"
        f"- What you need Gemini to clarify\n\n"
        f"Be direct and concise. Maximum 4 sentences.\n"
        f"Do NOT make a final decision yet. Just challenge Gemini."
    )
    try:
        r = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {CHATGPT_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 400
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT CHALLENGE ERROR: {str(e)}"


# ==========================================
# CHATGPT — FINAL DECISION AFTER GEMINI REPLY
# ==========================================
def chatgpt_final(symbol, price, signal, chatgpt_view, gemini_first,
                  chatgpt_challenge_msg, gemini_defense):
    if not CHATGPT_KEY:
        return "CHATGPT ERROR: No API key"

    twelve = signal.get("twelve_data", {})
    signal_clean = {k: v for k, v in signal.items() if k not in ["twelve_data", "calendar"]}

    prompt = (
        f"You are a SENIOR EXECUTION TRADER making the ABSOLUTE FINAL decision.\n\n"
        f"SYMBOL: {symbol}\n"
        f"PRICE: {price}\n\n"
        f"Your original analysis:\n{chatgpt_view}\n\n"
        f"Gemini original analysis:\n{gemini_first}\n\n"
        f"Your challenge to Gemini:\n{chatgpt_challenge_msg}\n\n"
        f"Gemini defense:\n{gemini_defense}\n\n"
        f"Original signal:\n{json.dumps(signal_clean, indent=2)}\n\n"
        f"Twelve Data indicators:\n{json.dumps(twelve, indent=2)}\n\n"
        f"You have heard both sides. Make the FINAL call.\n"
        f"No more discussion after this.\n\n"
        f"TECHNICAL OVERRIDE RULES:\n"
        f"- If structure_bias is BEAR and direction is SHORT — do NOT recommend BUY\n"
        f"- If structure_bias is BULL and direction is LONG — do NOT recommend SELL\n"
        f"- Technical signal is PRIMARY — news and fundamentals are SUPPORTING only\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must be exactly: {price}\n"
        f"- STOP LOSS must be a real number from {price}\n"
        f"- TAKE PROFIT must be a real number from {price}\n"
        f"- If BUY: SL below price, TP above price\n"
        f"- If SELL: SL above price, TP below price\n"
        f"- Use ATR from signal for SL/TP sizing if available\n"
        f"- Never write Unknown or blank\n\n"
        f"Output EXACTLY:\n"
        f"FINAL DIRECTION: [BUY/SELL/NEUTRAL]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"WHY THIS DECISION: [2-3 sentences final reasoning]"
    )
    try:
        r = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {CHATGPT_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 800
            },
            timeout=45
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT FINAL ERROR: {str(e)}"


# ==========================================
# WEBHOOK
# ==========================================
@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Webhook working. Send POST requests with trading data.", 200

    try:
        request_json = request.get_json(silent=True, force=True)
        request_text = request.get_data(as_text=True)

        print(f"Content-Type: {request.content_type}")
        print(f"Raw data: {request_text[:500]}")

        data = parse_incoming_data(request_json, request_text)

        if not data:
            send_telegram("No data received from TradingView")
            return "No data received", 400

        # Extract fields
        symbol     = safe_str(data.get("symbol") or data.get("ticker"), "Unknown")
        price      = safe_float(data.get("price") or data.get("close") or 0)
        timeframe  = safe_str(data.get("tf") or data.get("timeframe") or data.get("interval"), "1H")
        direction  = safe_str(data.get("direction"), "Unknown")
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
        signal_score = safe_int(data.get("score") or 0)
        signal_ea    = safe_int(data.get("ea_score") or data.get("ea_filter") or 0)

        print(f"Symbol: {symbol} | Price: {price} | Score: {signal_score} | EA: {signal_ea}")

        # STEP 1 — Session gate
        allowed, reason, session = session_gate(symbol)
        if not allowed:
            send_telegram(
                f"SIGNAL BLOCKED\n"
                f"---------------------------\n"
                f"Symbol: {symbol}\n"
                f"Session: {session}\n"
                f"Reason: {reason}"
            )
            return "Blocked", 200

        # STEP 2 — Signal notification
        send_telegram(
            f"NEW SIGNAL RECEIVED\n"
            f"---------------------------\n"
            f"Symbol: {symbol}\n"
            f"Price: {price}\n"
            f"Direction: {direction}\n"
            f"Score: {score}/100\n"
            f"EA Score: {signal_ea}/8\n"
            f"Timeframe: {timeframe}\n"
            f"Session: {session}\n"
            f"ADX: {adx}\n"
            f"ATR: {atr}\n"
            f"Structure: {structure}\n"
            f"EMA 21/50/200: {ema_21} / {ema_50} / {ema_200}\n"
            f"Volume Ratio: {volume}\n"
            f"Validator: {validator}\n"
            f"Confidence: {confidence}\n"
            f"---------------------------\n"
            f"Fetching live news, macro and indicators..."
        )

        # STEP 3 — Fetch all live data
        news     = fetch_finnhub(symbol)
        macro    = fetch_gdelt()
        calendar = fetch_economic_calendar()

        # STEP 3.5 — Fetch Twelve Data
        twelve_data = fetch_twelve_data(symbol)
        data["twelve_data"] = twelve_data
        data["calendar"]    = calendar

        # Build Twelve Data summary
        if twelve_data:
            twelve_summary = (
                f"RSI: {twelve_data.get('rsi', 'N/A')}\n"
                f"MACD: {twelve_data.get('macd', 'N/A')}\n"
                f"Stoch K/D: {twelve_data.get('stoch_k', 'N/A')} / {twelve_data.get('stoch_d', 'N/A')}\n"
                f"CCI: {twelve_data.get('cci', 'N/A')}\n"
                f"MFI: {twelve_data.get('mfi', 'N/A')}\n"
                f"Williams %R: {twelve_data.get('willr', 'N/A')}\n"
                f"Aroon Up/Down: {twelve_data.get('aroon_up', 'N/A')} / {twelve_data.get('aroon_down', 'N/A')}\n"
                f"PSAR: {twelve_data.get('psar', 'N/A')}"
            )
        else:
            twelve_summary = "Twelve Data: No API key or unavailable"

        # Calendar warning
        cal_warning = ""
        if calendar.get("high_impact_soon"):
            events_list = ", ".join(calendar.get("events", []))
            cal_warning = f"HIGH IMPACT EVENTS: {events_list}\n"

        send_telegram(
            f"LIVE DATA FETCHED\n"
            f"---------------------------\n"
            f"News Sentiment: {news['sentiment']}\n"
            f"Headlines: {len(news.get('headlines', []))} found\n"
            f"Macro Risk: {macro['risk']}\n"
            f"{cal_warning}"
            f"---------------------------\n"
            f"TWELVE DATA INDICATORS:\n"
            f"{twelve_summary}\n"
            f"---------------------------\n"
            f"Running Gemini analysis..."
        )

        # STEP 4 — Gemini full analysis
        gemini = gemini_analysis(data, news, macro)
        send_telegram(f"GEMINI ANALYSIS:\n---------------------------\n{gemini}")

        # STEP 5 — Handle Gemini failure
        if "GEMINI UNAVAILABLE" in gemini:
            send_telegram("Gemini unavailable. ChatGPT analyzing alone...")
            chatgpt = chatgpt_analysis(
                symbol, price, timeframe, data, news, macro,
                "Gemini unavailable. Use your own analysis only."
            )
            send_telegram(f"CHATGPT ANALYSIS:\n---------------------------\n{chatgpt}")
            send_telegram(f"FINAL DECISION (CHATGPT ONLY)\n---------------------------\n{chatgpt}")
            register_trade(symbol, session)
            return "OK", 200

        # STEP 6 — ChatGPT full analysis
        send_telegram("Running ChatGPT analysis...")
        chatgpt = chatgpt_analysis(
            symbol, price, timeframe, data, news, macro, calendar, gemini
        )
        send_telegram(f"CHATGPT ANALYSIS:\n---------------------------\n{chatgpt}")

        # STEP 7 — Agreement check
        gemini_buy      = "buy" in gemini.lower()
        gemini_sell     = "sell" in gemini.lower()
        gemini_neutral  = "neutral" in gemini.lower()
        chatgpt_buy     = "buy" in chatgpt.lower()
        chatgpt_sell    = "sell" in chatgpt.lower()
        chatgpt_neutral = "neutral" in chatgpt.lower()

        if gemini_neutral or chatgpt_neutral:
            send_telegram(
                f"FINAL DECISION (CHATGPT ONLY — NEUTRAL DETECTED)\n"
                f"---------------------------\n"
                f"{chatgpt}"
            )

        elif (gemini_buy and chatgpt_buy) or (gemini_sell and chatgpt_sell):
            send_telegram(
                f"FINAL DECISION (BOTH AGREED)\n"
                f"---------------------------\n"
                f"{chatgpt}"
            )

        else:
            send_telegram("Disagreement detected. ChatGPT challenging Gemini...")

            challenge = chatgpt_challenge(symbol, price, chatgpt, gemini)
            send_telegram(
                f"CHATGPT CHALLENGE TO GEMINI:\n"
                f"---------------------------\n"
                f"{challenge}"
            )

            gemini_reply = gemini_defend(data, news, macro, gemini, challenge)
            send_telegram(
                f"GEMINI DEFENSE:\n"
                f"---------------------------\n"
                f"{gemini_reply}"
            )

            final = chatgpt_final(
                symbol, price, data,
                chatgpt, gemini,
                challenge, gemini_reply
            )
            send_telegram(
                f"FINAL DECISION (CHATGPT AFTER DEBATE)\n"
                f"---------------------------\n"
                f"{final}"
            )

        # STEP 8 — Register trade
        register_trade(symbol, session)

        return "OK", 200

    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        print(error_msg)
        send_telegram(f"System Error:\n{error_msg}")
        return error_msg, 500


# ==========================================
# HEALTH CHECK
# ==========================================
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/", methods=["GET"])
def root():
    return "Trading Bot is running. Webhook at /webhook", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
