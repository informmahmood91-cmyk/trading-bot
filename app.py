import json
import os

import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY")
CHATGPT_KEY = os.environ.get("CHATGPT_KEY")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
CHATGPT_URL = "https://api.openai.com/v1/chat/completions"


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(msg) > 4000:
        msg = msg[:4000]
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"Telegram sent: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")


def safe_float(value):
    """Safely convert any value to float."""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def safe_str(value, default="N/A"):
    """Safely convert value to string."""
    if value is None or value == "" or value == 0:
        return default
    return str(value).strip()


def parse_incoming_data(request_form, request_json, request_text):
    """Try every possible way to get the data."""

    # Try 1: Clean JSON
    if request_json and isinstance(request_json, dict):
        print(f"Parsed as JSON: {request_json}")
        return request_json

    # Try 2: Raw text as JSON
    if request_text:
        cleaned = request_text.strip()
        try:
            parsed = json.loads(cleaned)
            print(f"Raw text parsed as JSON: {parsed}")
            return parsed
        except Exception:
            pass

    # Try 3: Form data
    if request_form and isinstance(request_form, dict) and len(request_form) > 0:
        print(f"Form data: {request_form}")
        return request_form

    print(f"Fallback - raw text: {request_text}")
    return {"raw_message": str(request_text) if request_text else "No data"}


def deepseek_analysis(symbol, price, timeframe, script_data):
    prompt = (
        f"You are a professional trading analyst with knowledge of news and fundamentals.\n\n"
        f"SIGNAL DATA FROM TRADINGVIEW:\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n"
        f"TIMEFRAME: {timeframe}\n"
        f"DIRECTION: {safe_str(script_data.get('direction'))}\n"
        f"SIGNAL SCORE: {safe_str(script_data.get('score'))}/100\n"
        f"ADX TREND STRENGTH: {safe_str(script_data.get('adx'))}\n"
        f"MARKET STRUCTURE: {safe_str(script_data.get('structure'))}\n"
        f"SESSION: {safe_str(script_data.get('session'))}\n"
        f"VWAP PILLAR: {safe_str(script_data.get('pillar_vwap'))}\n"
        f"PIVOT PILLAR: {safe_str(script_data.get('pillar_pivot'))}\n"
        f"EMA PILLAR: {safe_str(script_data.get('pillar_ema'))}\n"
        f"HHHL PILLAR: {safe_str(script_data.get('pillar_hhhl'))}\n"
        f"AUCTION PILLAR: {safe_str(script_data.get('pillar_auction'))}\n"
        f"EA SCORE: {safe_str(script_data.get('ea_score'))}\n"
        f"EA GATE: {safe_str(script_data.get('ea_gate'))}\n"
        f"VALIDATOR STATUS: {safe_str(script_data.get('validator_status'))}\n"
        f"VALIDATOR CONFIDENCE: {safe_str(script_data.get('validator_confidence'))}\n"
        f"VALIDATOR T1 RATE: {safe_str(script_data.get('validator_t1_rate'))}\n"
        f"VWAP VALUE: {safe_str(script_data.get('vwap'))}\n"
        f"PIVOT VALUE: {safe_str(script_data.get('pivot'))}\n\n"
        f"YOUR TASKS:\n"
        f"1. Use your knowledge of recent NEWS and FUNDAMENTALS for {symbol}\n"
        f"2. Assess current market sentiment for {symbol}\n"
        f"3. Validate the technical signal data above\n"
        f"4. Calculate Stop Loss and Take Profit from price {price}\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must always be exactly: {price}\n"
        f"- STOP LOSS must be a real number calculated from {price}\n"
        f"- TAKE PROFIT must be a real number calculated from {price}\n"
        f"- NEVER write Unknown, N/A or blank for Entry, SL or TP\n"
        f"- If direction is LONG: SL below price, TP above price\n"
        f"- If direction is SHORT: SL above price, TP below price\n\n"
        f"Output EXACTLY in this format with no extra text:\n"
        f"DIRECTION: [BULLISH/BEARISH/NEUTRAL]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"NEWS SENTIMENT: [POSITIVE/NEGATIVE/NEUTRAL]\n"
        f"FUNDAMENTAL BIAS: [BULLISH/BEARISH/NEUTRAL]\n"
        f"REASONING: [2-3 sentences covering technical + news + fundamentals]"
    )
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1000,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"DEEPSEEK ERROR: {str(e)}"


def chatgpt_analysis(symbol, price, timeframe, script_data):
    prompt = (
        f"You are a professional trading analyst with knowledge of news and fundamentals.\n\n"
        f"SIGNAL DATA FROM TRADINGVIEW:\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n"
        f"TIMEFRAME: {timeframe}\n"
        f"DIRECTION: {safe_str(script_data.get('direction'))}\n"
        f"SIGNAL SCORE: {safe_str(script_data.get('score'))}/100\n"
        f"ADX TREND STRENGTH: {safe_str(script_data.get('adx'))}\n"
        f"MARKET STRUCTURE: {safe_str(script_data.get('structure'))}\n"
        f"SESSION: {safe_str(script_data.get('session'))}\n"
        f"VWAP PILLAR: {safe_str(script_data.get('pillar_vwap'))}\n"
        f"PIVOT PILLAR: {safe_str(script_data.get('pillar_pivot'))}\n"
        f"EMA PILLAR: {safe_str(script_data.get('pillar_ema'))}\n"
        f"HHHL PILLAR: {safe_str(script_data.get('pillar_hhhl'))}\n"
        f"AUCTION PILLAR: {safe_str(script_data.get('pillar_auction'))}\n"
        f"EA SCORE: {safe_str(script_data.get('ea_score'))}\n"
        f"EA GATE: {safe_str(script_data.get('ea_gate'))}\n"
        f"VALIDATOR STATUS: {safe_str(script_data.get('validator_status'))}\n"
        f"VALIDATOR CONFIDENCE: {safe_str(script_data.get('validator_confidence'))}\n"
        f"VALIDATOR T1 RATE: {safe_str(script_data.get('validator_t1_rate'))}\n"
        f"VWAP VALUE: {safe_str(script_data.get('vwap'))}\n"
        f"PIVOT VALUE: {safe_str(script_data.get('pivot'))}\n\n"
        f"YOUR TASKS:\n"
        f"1. Use your knowledge of recent NEWS and FUNDAMENTALS for {symbol}\n"
        f"2. Assess current market sentiment for {symbol}\n"
        f"3. Validate the technical signal data above\n"
        f"4. Calculate Stop Loss and Take Profit from price {price}\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must always be exactly: {price}\n"
        f"- STOP LOSS must be a real number calculated from {price}\n"
        f"- TAKE PROFIT must be a real number calculated from {price}\n"
        f"- NEVER write Unknown, N/A or blank for Entry, SL or TP\n"
        f"- If direction is LONG: SL below price, TP above price\n"
        f"- If direction is SHORT: SL above price, TP below price\n\n"
        f"Output EXACTLY in this format with no extra text:\n"
        f"DIRECTION: [BULLISH/BEARISH/NEUTRAL]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"NEWS SENTIMENT: [POSITIVE/NEGATIVE/NEUTRAL]\n"
        f"FUNDAMENTAL BIAS: [BULLISH/BEARISH/NEUTRAL]\n"
        f"REASONING: [2-3 sentences covering technical + news + fundamentals]"
    )
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1000,
    }
    headers = {
        "Authorization": f"Bearer {CHATGPT_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(CHATGPT_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CHATGPT ERROR: {str(e)}"


def final_consensus(symbol, price, deepseek_result, chatgpt_result):
    prompt = (
        f"You are the final decision maker. Two AI analysts reviewed a trade signal.\n\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n\n"
        f"DEEPSEEK ANALYSIS:\n{deepseek_result}\n\n"
        f"CHATGPT ANALYSIS:\n{chatgpt_result}\n\n"
        f"YOUR TASKS:\n"
        f"1. Compare both analyses\n"
        f"2. Check if they agree on direction\n"
        f"3. Pick the best SL and TP from both\n"
        f"4. Give a clear final verdict\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must always be exactly: {price}\n"
        f"- STOP LOSS must be a real number\n"
        f"- TAKE PROFIT must be a real number\n"
        f"- NEVER write Unknown or blank\n\n"
        f"Output EXACTLY in this format:\n"
        f"AGREEMENT: [YES/NO]\n"
        f"ACTION: [LONG/SHORT/WAIT]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"FINAL VERDICT: [2-3 sentences on why to take or skip this trade]"
    )
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"CONSENSUS ERROR: {str(e)}"


@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Webhook working. Send POST requests with trading data.", 200

    try:
        request_json = request.get_json(silent=True, force=True)
        request_form = request.form.to_dict() if request.form else None
        request_text = request.get_data(as_text=True)

        print(f"Content-Type: {request.content_type}")
        print(f"Raw data: {request_text[:500]}")

        data = parse_incoming_data(request_form, request_json, request_text)

        if not data:
            send_telegram("No data received from TradingView")
            return "No data received", 400

        print(f"Final parsed data: {json.dumps(data)[:500]}")

        # DEBUG — send raw data to Telegram so we can see what arrived
        send_telegram(f"*DEBUG RAW DATA:*\n{request_text[:1000]}")

        # Extract all fields safely
        symbol   = safe_str(data.get("symbol") or data.get("ticker") or data.get("pair"), "Unknown")
        raw_price = data.get("price") or data.get("close") or data.get("current_price") or 0
        price    = safe_float(raw_price)
        score    = safe_str(data.get("score") or data.get("strength") or data.get("raw_signal"), "N/A")
        timeframe = safe_str(data.get("timeframe") or data.get("interval"), "1H")
        direction = safe_str(data.get("direction") or data.get("signal"), "Unknown")
        validator = safe_str(data.get("validator_status"), "N/A")
        validator_conf = safe_str(data.get("validator_confidence"), "N/A")
        structure = safe_str(data.get("structure"), "N/A")
        session  = safe_str(data.get("session"), "N/A")
        adx      = safe_str(data.get("adx"), "N/A")
        ea_gate  = safe_str(data.get("ea_gate"), "N/A")
        ea_score = safe_str(data.get("ea_score"), "N/A")

        print(f"Symbol: {symbol} | Price: {price} | Direction: {direction} | Score: {score}")

        # Send signal notification — no backticks to avoid Markdown issues
        send_telegram(
            f"*SIGNAL TRIGGERED*\n"
            f"---------------------------\n"
            f"Symbol: {symbol}\n"
            f"Price: {price}\n"
            f"Direction: {direction}\n"
            f"Score: {score}/100\n"
            f"Timeframe: {timeframe}\n"
            f"Session: {session}\n"
            f"Structure: {structure}\n"
            f"ADX: {adx}\n"
            f"EA Score: {ea_score}\n"
            f"EA Gate: {ea_gate}\n"
            f"Validator: {validator}\n"
            f"Validator Conf: {validator_conf}\n"
            f"---------------------------\n"
            f"*Analyzing with AI...*"
        )

        deepseek = deepseek_analysis(symbol, price, timeframe, data)
        send_telegram(f"*DEEPSEEK ANALYSIS:*\n---------------------------\n{deepseek}")

        chatgpt = chatgpt_analysis(symbol, price, timeframe, data)
        send_telegram(f"*CHATGPT ANALYSIS:*\n---------------------------\n{chatgpt}")

        consensus = final_consensus(symbol, price, deepseek, chatgpt)
        send_telegram(f"*FINAL CONSENSUS:*\n---------------------------\n{consensus}")

        return "OK", 200

    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        print(f"{error_msg}")
        send_telegram(f"*System Error:*\n{error_msg}")
        return error_msg, 500


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/", methods=["GET"])
def root():
    return "Trading Bot is running. Webhook at /webhook", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
