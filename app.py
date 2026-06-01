import json, requests, os, urllib.parse
from flask import Flask, request
from datetime import datetime

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
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        print("Telegram sent OK")
    except Exception as e:
        print(f"Telegram error: {e}")

def deepseek_analysis(symbol, price, timeframe, script_data):
    prompt = f"""Analyze this trading signal:

TRADER'S SCRIPT (score 80+ triggered):
{json.dumps(script_data, indent=2)}

Symbol: {symbol} | Price: {price} | Timeframe: {timeframe}

1. SEARCH WEB for news, economic calendar
2. DO YOUR OWN technical analysis
3. REVIEW their script (pillars, EA score, validator)
4. Give DIRECTION and PROPOSED TRADE

Output:
DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
PROPOSED TRADE: Entry X, SL X, TP X
REASONING: [short explanation]"""
    
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 1000}
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=90)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"ERROR: {str(e)}"

def chatgpt_analysis(symbol, price, timeframe, script_data):
    prompt = f"""Analyze this trading signal:

TRADER'S SCRIPT (score 80+ triggered):
{json.dumps(script_data, indent=2)}

Symbol: {symbol} | Price: {price} | Timeframe: {timeframe}

1. DO YOUR OWN technical analysis
2. REVIEW their script
3. Give DIRECTION and PROPOSED TRADE

Output:
DIRECTION: [BULLISH/BEARISH/NEUTRAL]
CONFIDENCE: [0-100%]
PROPOSED TRADE: Entry X, SL X, TP X
REASONING: [short explanation]"""
    
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 800}
    headers = {"Authorization": f"Bearer {CHATGPT_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(CHATGPT_URL, headers=headers, json=payload, timeout=60)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        return f"ERROR: {str(e)}"

def final_consensus(symbol, price, deepseek_result, chatgpt_result):
    prompt = f"""Make FINAL TRADING DECISION.

Symbol: {symbol} | Price: {price}

DEEPSEEK: {deepseek_result}
CHATGPT: {chatgpt_result}

Output EXACTLY:
AGREEMENT: [YES/NO]
IF YES:
ACTION: [LONG/SHORT]
ENTRY: [price]
STOP: [price]
TARGET: [price]
RR: [1:X]
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

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Webhook endpoint is working. Send POST requests with trading data.", 200
    
    try:
        data = request.get_json()
        if not data and request.data:
            try:
                data = json.loads(request.data)
            except:
                data = {"raw_message": request.data.decode('utf-8')}
        
        if not data:
            return "No data received", 400
        
        symbol = data.get('symbol', 'Unknown')
        price = data.get('price', 'Unknown')
        score = data.get('script_strength', 'Unknown')
        timeframe = data.get('timeframe', '1H')
        
        send_telegram(f"🎯 SIGNAL TRIGGERED\nSymbol: {symbol}\nPrice: {price}\nScore: {score}/100\nTimeframe: {timeframe}\n\nAnalyzing...")
        
        deepseek = deepseek_analysis(symbol, price, timeframe, data)
        send_telegram(f"🤖 DEEPSEEK:\n{deepseek}")
        
        chatgpt = chatgpt_analysis(symbol, price, timeframe, data)
        send_telegram(f"🧠 CHATGPT:\n{chatgpt}")
        
        consensus = final_consensus(symbol, price, deepseek, chatgpt)
        send_telegram(f"✅ FINAL CONSENSUS:\n{consensus}")
        
        return "OK", 200
    except Exception as e:
        error_msg = f"ERROR: {str(e)[:200]}"
        send_telegram(f"❌ {error_msg}")
        return error_msg, 500

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
