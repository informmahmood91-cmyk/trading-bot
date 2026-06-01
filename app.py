import json, requests, os
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

# ============================================================
# READ KEYS FROM ENVIRONMENT VARIABLES (SECURE)
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY")
CHATGPT_KEY = os.environ.get("CHATGPT_KEY")

# ============================================================
# API ENDPOINTS
# ============================================================
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
CHATGPT_URL = "https://api.openai.com/v1/chat/completions"

# ============================================================
# TELEGRAM SENDER
# ============================================================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(msg) > 4000:
        msg = msg[:4000]
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        print(f"Telegram sent: {r.status_code}")
    except Exception as e:
        print(f"Telegram error: {e}")

# ============================================================
# DEEPSEEK ANALYSIS (FORCED TO USE SIGNAL PRICE)
# ============================================================
def deepseek_analysis(symbol, price, timeframe, script_data):
    prompt = f"""You are a trading analyst with web search. CRITICAL: Use ONLY the price provided below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT SIGNAL PRICE: {price}
SYMBOL: {symbol}
TIMEFRAME: {timeframe}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRADER'S SCRIPT DATA:
{json.dumps(script_data, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT RULES (MUST FOLLOW):
1. Entry price MUST be exactly: {price}
2. Stop Loss MUST be calculated FROM {price} (not from your memory)
3. Take Profit MUST be calculated FROM {price}
4. DO NOT use old prices from your training data
5. For BTC: Current price is {price}, NOT $90,000
6. For Gold: Current price is {price}
7. For Forex: Current price is {price}

CALCULATION GUIDELINES:
- Crypto (BTC, ETH): SL within 2-5% of {price}, TP within 5-10%
- Gold (XAUUSD): SL within 0.5-1%, TP within 1-2%
- Forex (EURUSD, GBPJPY, etc.): SL within 0.2-0.5%, TP within 0.5-1%

Based on the script data (pillars, EA score, ADX, auction state), determine direction.

Output EXACTLY this format:

DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
ENTRY: {price}
STOP LOSS: [number]
TAKE PROFIT: [number]
RISK:REWARD: [1:X]
REASONING: [2-3 sentences using script data]"""
    
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 800}
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        result = r.json()['choices'][0]['message']['content']
        print(f"DeepSeek response for {symbol} at {price}: OK")
        return result
    except Exception as e:
        return f"ERROR: {str(e)}"

# ============================================================
# CHATGPT ANALYSIS (FORCED TO USE SIGNAL PRICE)
# ============================================================
def chatgpt_analysis(symbol, price, timeframe, script_data):
    prompt = f"""You are a trading analyst. CRITICAL: Use ONLY the price provided below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CURRENT SIGNAL PRICE: {price}
SYMBOL: {symbol}
TIMEFRAME: {timeframe}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRADER'S SCRIPT DATA:
{json.dumps(script_data, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT RULES (MUST FOLLOW):
1. Entry price MUST be exactly: {price}
2. Stop Loss MUST be calculated FROM {price} (not from your memory)
3. Take Profit MUST be calculated FROM {price}
4. DO NOT use old prices from your training data
5. For BTC: Current price is {price}, NOT $90,000
6. For Gold: Current price is {price}
7. For Forex: Current price is {price}

CALCULATION GUIDELINES:
- Crypto (BTC, ETH): SL within 2-5% of {price}, TP within 5-10%
- Gold (XAUUSD): SL within 0.5-1%, TP within 1-2%
- Forex (EURUSD, GBPJPY, etc.): SL within 0.2-0.5%, TP within 0.5-1%

Based on the script data (pillars, EA score, ADX, auction state), determine direction.

Output EXACTLY this format:

DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
ENTRY: {price}
STOP LOSS: [number]
TAKE PROFIT: [number]
RISK:REWARD: [1:X]
REASONING: [2-3 sentences using script data]"""
    
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 800}
    headers = {"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(CHATGPT_URL, headers=headers, json=payload, timeout=60)
        result = r.json()['choices'][0]['message']['content']
        print(f"ChatGPT response for {symbol} at {price}: OK")
        return result
    except Exception as e:
        return f"ERROR: {str(e)}"

# ============================================================
# FINAL CONSENSUS (DeepSeek + ChatGPT agree)
# ============================================================
def final_consensus(symbol, price, deepseek_result, chatgpt_result):
    prompt = f"""Make FINAL TRADING DECISION.

Symbol: {symbol}
Current Price: {price}

DEEPSEEK ANALYSIS:
{deepseek_result}

CHATGPT ANALYSIS:
{chatgpt_result}

Output EXACTLY this format:

AGREEMENT: [YES/NO]

IF YES:
ACTION: [LONG/SHORT]
ENTRY: [price]
STOP LOSS: [number]
TAKE PROFIT: [number]
RISK:REWARD: [1:X]

IF NO:
REASON: [why]
WAIT FOR: [condition]"""
    
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 600}
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"ERROR: {str(e)}"

# ============================================================
# WEBHOOK ENDPOINT (Receives TradingView Alerts)
# ============================================================
@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Webhook endpoint is working. Send POST requests with trading data.", 200
    
    try:
        # Get data from TradingView
        data = request.get_json()
        if not data and request.data:
            try:
                data = json.loads(request.data)
            except:
                data = {"raw_message": request.data.decode('utf-8')}
        
        if not data:
            return "No data received", 400
        
        # Extract signal data
        symbol = data.get('symbol', 'Unknown')
        price = data.get('price', 'Unknown')
        score = data.get('score', data.get('script_strength', 'Unknown'))
        timeframe = data.get('timeframe', '1H')
        direction = data.get('direction', 'Unknown')
        
        print(f"📥 Signal received: {symbol} at {price} | Score: {score} | Direction: {direction}")
        
        # Send initial notification
        send_telegram(f"🎯 *SIGNAL TRIGGERED*\n━━━━━━━━━━━━━━━━━━━━━━\nSymbol: {symbol}\nPrice: {price}\nScore: {score}/100\nDirection: {direction}\nTimeframe: {timeframe}\n\n*Analyzing with AI...*")
        
        # Get DeepSeek analysis
        deepseek = deepseek_analysis(symbol, price, timeframe, data)
        send_telegram(f"🤖 *DEEPSEEK (with news):*\n{deepseek}")
        
        # Get ChatGPT analysis
        chatgpt = chatgpt_analysis(symbol, price, timeframe, data)
        send_telegram(f"🧠 *CHATGPT (technical):*\n{chatgpt}")
        
        # Get final consensus
        consensus = final_consensus(symbol, price, deepseek, chatgpt)
        send_telegram(f"✅ *FINAL CONSENSUS:*\n{consensus}")
        
        return "OK", 200
        
    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        print(f"❌ {error_msg}")
        send_telegram(f"❌ *System Error:*\n{error_msg}")
        return error_msg, 500

# ============================================================
# HEALTH CHECK (For Render)
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

# ============================================================
# ROOT ENDPOINT
# ============================================================
@app.route('/', methods=['GET'])
def root():
    return "Trading Bot is running. Webhook endpoint at /webhook", 200

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
