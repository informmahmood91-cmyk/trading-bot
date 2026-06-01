import json, requests, os
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

# ============================================================
# READ KEYS FROM ENVIRONMENT VARIABLES
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
# PARSE INCOMING DATA (FLEXIBLE)
# ============================================================
def parse_incoming_data(request_data, request_json, request_text):
    """Try multiple ways to parse incoming webhook data"""
    
    # Try 1: Already parsed JSON
    if request_json and isinstance(request_json, dict):
        print("✅ Data parsed as JSON")
        return request_json
    
    # Try 2: Parse raw text as JSON
    if request_text:
        try:
            parsed = json.loads(request_text)
            print("✅ Raw text parsed as JSON")
            return parsed
        except:
            pass
    
    # Try 3: Look for form data
    if request_data and isinstance(request_data, dict):
        print("✅ Data from form")
        return request_data
    
    # Try 4: Extract from request.data
    if request_data:
        try:
            if isinstance(request_data, bytes):
                decoded = request_data.decode('utf-8')
                parsed = json.loads(decoded)
                print("✅ Bytes decoded and parsed")
                return parsed
        except:
            pass
    
    # Last resort: Return raw text
    print("⚠️ Using raw text fallback")
    return {"raw_message": str(request_text) if request_text else "No data"}

# ============================================================
# DEEPSEEK ANALYSIS
# ============================================================
def deepseek_analysis(symbol, price, timeframe, script_data):
    prompt = f"""You are a trading analyst with web search. CRITICAL: Use ONLY the price provided below.

CURRENT SIGNAL PRICE: {price}
SYMBOL: {symbol}
TIMEFRAME: {timeframe}

TRADER'S SCRIPT DATA:
{json.dumps(script_data, indent=2)}

IMPORTANT RULES:
1. Entry price MUST be exactly: {price}
2. Stop Loss MUST be calculated FROM {price}
3. Take Profit MUST be calculated FROM {price}
4. DO NOT use old prices from your training data

Output EXACTLY:
DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
ENTRY: {price}
STOP LOSS: [number]
TAKE PROFIT: [number]
RISK:REWARD: [1:X]
REASONING: [short explanation]"""
    
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 800}
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"ERROR: {str(e)}"

# ============================================================
# CHATGPT ANALYSIS
# ============================================================
def chatgpt_analysis(symbol, price, timeframe, script_data):
    prompt = f"""You are a trading analyst. CRITICAL: Use ONLY the price provided below.

CURRENT SIGNAL PRICE: {price}
SYMBOL: {symbol}
TIMEFRAME: {timeframe}

TRADER'S SCRIPT DATA:
{json.dumps(script_data, indent=2)}

IMPORTANT RULES:
1. Entry price MUST be exactly: {price}
2. Stop Loss MUST be calculated FROM {price}
3. Take Profit MUST be calculated FROM {price}
4. DO NOT use old prices from your training data

Output EXACTLY:
DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
ENTRY: {price}
STOP LOSS: [number]
TAKE PROFIT: [number]
RISK:REWARD: [1:X]
REASONING: [short explanation]"""
    
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 800}
    headers = {"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(CHATGPT_URL, headers=headers, json=payload, timeout=60)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"ERROR: {str(e)}"

# ============================================================
# FINAL CONSENSUS
# ============================================================
def final_consensus(symbol, price, deepseek_result, chatgpt_result):
    prompt = f"""Make FINAL TRADING DECISION.

Symbol: {symbol}
Current Price: {price}

DEEPSEEK: {deepseek_result}
CHATGPT: {chatgpt_result}

Output EXACTLY:
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
# WEBHOOK ENDPOINT
# ============================================================
@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Webhook endpoint is working. Send POST requests with trading data.", 200
    
    try:
        # Get data in multiple formats
        request_json = request.get_json(silent=True)
        request_data = request.form.to_dict() if request.form else None
        request_text = request.get_data(as_text=True)
        
        print(f"📥 Webhook received. Content-Type: {request.content_type}")
        print(f"📥 Raw data (first 200 chars): {request_text[:200]}")
        
        # Parse the data
        data = parse_incoming_data(request_data, request_json, request_text)
        
        if not data:
            send_telegram("❌ No data received from TradingView")
            return "No data received", 400
        
        print(f"📊 Parsed data: {json.dumps(data, indent=2)[:500]}")
        
        # Extract signal data (try multiple field names)
        symbol = data.get('symbol') or data.get('ticker') or data.get('pair') or 'Unknown'
        price = data.get('price') or data.get('close') or data.get('current_price') or 'Unknown'
        score = data.get('score') or data.get('strength') or data.get('script_strength') or data.get('raw_signal') or 'Unknown'
        timeframe = data.get('timeframe') or data.get('interval') or '1H'
        direction = data.get('direction') or data.get('signal') or 'Unknown'
        
        # Convert price to number if possible
        try:
            price = float(price)
        except:
            pass
        
        print(f"🎯 Signal: {symbol} at {price} | Score: {score}")
        
        # Send initial notification
        send_telegram(f"🎯 *SIGNAL TRIGGERED*\n━━━━━━━━━━━━━━━━━━━━━━\nSymbol: {symbol}\nPrice: {price}\nScore: {score}/100\nDirection: {direction}\nTimeframe: {timeframe}\n\n*Analyzing with AI...*")
        
        # Get AI analyses
        deepseek = deepseek_analysis(symbol, price, timeframe, data)
        send_telegram(f"🤖 *DEEPSEEK:*\n{deepseek}")
        
        chatgpt = chatgpt_analysis(symbol, price, timeframe, data)
        send_telegram(f"🧠 *CHATGPT:*\n{chatgpt}")
        
        consensus = final_consensus(symbol, price, deepseek, chatgpt)
        send_telegram(f"✅ *FINAL CONSENSUS:*\n{consensus}")
        
        return "OK", 200
        
    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        print(f"❌ {error_msg}")
        send_telegram(f"❌ *System Error:*\n{error_msg}")
        return error_msg, 500

# ============================================================
# HEALTH CHECK
# ============================================================
@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

@app.route('/', methods=['GET'])
def root():
    return "Trading Bot is running. Webhook endpoint at /webhook", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
