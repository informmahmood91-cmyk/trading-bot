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


def parse_incoming_data(request_form, request_json, request_text):
    if request_json and isinstance(request_json, dict):
        print("Data parsed as JSON")
        return request_json
    if request_text:
        try:
            parsed = json.loads(request_text)
            print("Raw text parsed as JSON")
            return parsed
        except Exception:
            pass
    if request_form and isinstance(request_form, dict):
        print("Data from form")
        return request_form
    print("Using raw text fallback")
    return {"raw_message": str(request_text) if request_text else "No data"}


def deepseek_analysis(symbol, price, timeframe, script_data):
    prompt = (
        f"You are a professional trading analyst with access to live news and fundamentals.\n\n"
        f"SIGNAL DATA FROM TRADINGVIEW:\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n"
        f"TIMEFRAME: {timeframe}\n"
        f"DIRECTION: {script_data.get('direction', 'N/A')}\n"
        f"SIGNAL SCORE: {script_data.get('score', 'N/A')}/100\n"
        f"ADX TREND STRENGTH: {script_data.get('adx', 'N/A')}\n"
        f"MARKET STRUCTURE: {script_data.get('structure', 'N/A')}\n"
        f"SESSION: {script_data.get('session', 'N/A')}\n"
        f"VWAP PILLAR: {script_data.get('pillar_vwap', 'N/A')}\n"
        f"PIVOT PILLAR: {script_data.get('pillar_pivot', 'N/A')}\n"
        f"EMA PILLAR: {script_data.get('pillar_ema', 'N/A')}\n"
        f"HHHL PILLAR: {script_data.get('pillar_hhhl', 'N/A')}\n"
        f"AUCTION PILLAR: {script_data.get('pillar_auction', 'N/A')}\n"
        f"EA SCORE: {script_data.get('ea_score', 'N/A')}\n"
        f"EA GATE: {script_data.get('ea_gate', 'N/A')}\n"
        f"VALIDATOR STATUS: {script_data.get('validator_status', 'N/A')}\n"
        f"VALIDATOR CONFIDENCE: {script_data.get('validator_confidence', 'N/A')}\n"
        f"VALIDATOR T1 RATE: {script_data.get('validator_t1_rate', 'N/A')}\n"
        f"VWAP VALUE: {script_data.get('vwap', 'N/A')}\n"
        f"PIVOT VALUE: {script_data.get('pivot', 'N/A')}\n\n"
        f"YOUR TASKS:\n"
        f"1. Consider recent NEWS and FUNDAMENTALS for {symbol}\n"
        f"2. Consider current market sentiment\n"
        f"3. Validate the technical signal above\n"
        f"4. Calculate SL and TP based STRICTLY on price {price}\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must be exactly: {price}\n"
        f"- STOP LOSS must be calculated from {price}\n"
        f"- TAKE PROFIT must be calculated from {price}\n"
        f"- Never write Unknown for entry, always use: {price}\n\n"
        f"Output EXACTLY in this format:\n"
        f"DIRECTION: [BULLISH/BEARISH/NEUTRAL]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"NEWS SENTIMENT: [POSITIVE/NEGATIVE/NEUTRAL]\n"
        f"FUNDAMENTAL BIAS: [BULLISH/BEARISH/NEUTRAL]\n"
        f"REASONING: [technical + news + fundamental explanation]"
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
        f"You are a professional trading analyst with access to live news and fundamentals.\n\n"
        f"SIGNAL DATA FROM TRADINGVIEW:\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n"
        f"TIMEFRAME: {timeframe}\n"
        f"DIRECTION: {script_data.get('direction', 'N/A')}\n"
        f"SIGNAL SCORE: {script_data.get('score', 'N/A')}/100\n"
        f"ADX TREND STRENGTH: {script_data.get('adx', 'N/A')}\n"
        f"MARKET STRUCTURE: {script_data.get('structure', 'N/A')}\n"
        f"SESSION: {script_data.get('session', 'N/A')}\n"
        f"VWAP PILLAR: {script_data.get('pillar_vwap', 'N/A')}\n"
        f"PIVOT PILLAR: {script_data.get('pillar_pivot', 'N/A')}\n"
        f"EMA PILLAR: {script_data.get('pillar_ema', 'N/A')}\n"
        f"HHHL PILLAR: {script_data.get('pillar_hhhl', 'N/A')}\n"
        f"AUCTION PILLAR: {script_data.get('pillar_auction', 'N/A')}\n"
        f"EA SCORE: {script_data.get('ea_score', 'N/A')}\n"
        f"EA GATE: {script_data.get('ea_gate', 'N/A')}\n"
        f"VALIDATOR STATUS: {script_data.get('validator_status', 'N/A')}\n"
        f"VALIDATOR CONFIDENCE: {script_data.get('validator_confidence', 'N/A')}\n"
        f"VALIDATOR T1 RATE: {script_data.get('validator_t1_rate', 'N/A')}\n"
        f"VWAP VALUE: {script_data.get('vwap', 'N/A')}\n"
        f"PIVOT VALUE: {script_data.get('pivot', 'N/A')}\n\n"
        f"YOUR TASKS:\n"
        f"1. Consider recent NEWS and FUNDAMENTALS for {symbol}\n"
        f"2. Consider current market sentiment\n"
        f"3. Validate the technical signal above\n"
        f"4. Calculate SL and TP based STRICTLY on price {price}\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must be exactly: {price}\n"
        f"- STOP LOSS must be calculated from {price}\n"
        f"- TAKE PROFIT must be calculated from {price}\n"
        f"- Never write Unknown for entry, always use: {price}\n\n"
        f"Output EXACTLY in this format:\n"
        f"DIRECTION: [BULLISH/BEARISH/NEUTRAL]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"NEWS SENTIMENT: [POSITIVE/NEGATIVE/NEUTRAL]\n"
        f"FUNDAMENTAL BIAS: [BULLISH/BEARISH/NEUTRAL]\n"
        f"REASONING: [technical + news + fundamental explanation]"
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
        f"You are the final decision maker. Two AI analysts have reviewed a trade.\n\n"
        f"SYMBOL: {symbol}\n"
        f"CURRENT PRICE: {price}\n\n"
        f"DEEPSEEK ANALYSIS:\n{deepseek_result}\n\n"
        f"CHATGPT ANALYSIS:\n{chatgpt_result}\n\n"
        f"YOUR TASKS:\n"
        f"1. Compare both analyses\n"
        f"2. Check if they agree on direction\n"
        f"3. Give final verdict with best SL and TP\n\n"
        f"STRICT RULES:\n"
        f"- ENTRY must be exactly: {price}\n"
        f"- Never write Unknown\n\n"
        f"Output EXACTLY in this format:\n"
        f"AGREEMENT: [YES/NO]\n"
        f"ACTION: [LONG/SHORT/WAIT]\n"
        f"ENTRY: {price}\n"
        f"STOP LOSS: [number only]\n"
        f"TAKE PROFIT: [number only]\n"
        f"RISK:REWARD: [1:X]\n"
        f"CONFIDENCE: [0-100%]\n"
        f"FINAL VERDICT: [explanation of why to take or skip this trade]"
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
        request_json = request.get_json(silent=True)
        request_form = request.form.to_dict() if request.form else None
        request_text = request.get_data(as_text=True)

        print(f"Webhook received. Content-Type: {request.content_type}")
        print(f"Raw data (first 200 chars): {request_text[:200]}")

        data = parse_incoming_data(request_form, request_json, request_text)

        if not data:
            send_telegram("No data received from TradingView")
            return "No data received", 400

        symbol = data.get("symbol") or data.get("ticker") or data.get("pair") or "Unknown"
        raw_price = data.get("price") or data.get("close") or data.get("current_price") or 0
        score = data.get("score") or data.get("strength") or data.get("raw_signal") or "N/A"
        timeframe = data.get("timeframe") or data.get("interval") or "1H"
        direction = data.get("direction") or data.get("signal") or "Unknown"
        validator = data.get("validator_status") or "N/A"
        structure = data.get("structure") or "N/A"
        session = data.get("session") or "N/A"
        adx = data.get("adx") or "N/A"

        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            price = 0.0

        print(f"Signal: {symbol} at {price} | Score: {score}")

        send_telegram(
            f"*SIGNAL TRIGGERED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Symbol: `{symbol}`\n"
            f"Price: `{price}`\n"
            f"Direction: `{direction}`\n"
            f"Score: `{score}/100`\n"
            f"Timeframe: `{timeframe}`\n"
            f"Session: `{session}`\n"
            f"Structure: `{structure}`\n"
            f"ADX: `{adx}`\n"
            f"Validator: `{validator}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Analyzing with AI...*"
        )

        deepseek = deepseek_analysis(symbol, price, timeframe, data)
        send_telegram(f"*DEEPSEEK ANALYSIS:*\n━━━━━━━━━━━━━━━━━━━━━━\n{deepseek}")

        chatgpt = chatgpt_analysis(symbol, price, timeframe, data)
        send_telegram(f"*CHATGPT ANALYSIS:*\n━━━━━━━━━━━━━━━━━━━━━━\n{chatgpt}")

        consensus = final_consensus(symbol, price, deepseek, chatgpt)
        send_telegram(f"*FINAL CONSENSUS:*\n━━━━━━━━━━━━━━━━━━━━━━\n{consensus}")

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
