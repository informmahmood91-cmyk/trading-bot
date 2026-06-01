import json, requests
from flask import Flask, request
from datetime import datetime

app = Flask(__name__)

# ============================================================
# ✅ PASTE YOUR KEYS HERE
# ============================================================

TELEGRAM_TOKEN = "8957910002:AAEv8HiMaVwc29jCVPKMHoEy4t1z7Y6rRdc"
TELEGRAM_CHAT_ID = "8736138224"
DEEPSEEK_KEY = "sk-6d64640d8d6b443bb8cc3beef9961ee3"
CHATGPT_KEY = "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"

# ============================================================
# DO NOT CHANGE BELOW THIS LINE
# ============================================================

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
CHATGPT_URL = "https://api.openai.com/v1/chat/completions"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(msg) > 4000:
        msg = msg[:4000]
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        print(f"Telegram response: {r.status_code}")
        if r.status_code == 200:
            print("✅ Message sent to Telegram")
        else:
            print(f"❌ Telegram error: {r.text}")
    except Exception as e:
        print(f"❌ Telegram exception: {e}")

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    # Handle GET request (for testing)
    if request.method == 'GET':
        send_telegram("✅ Webhook endpoint is working. Send POST requests with trading data.")
        return "Webhook endpoint is working. Send POST requests with trading data.", 200
    
    # Handle POST request (TradingView alerts)
    try:
        print("📥 Webhook received!")
        
        # Get the data from TradingView
        data = request.get_json()
        
        # If no JSON, try to get raw data
        if not data and request.data:
            try:
                data = json.loads(request.data)
            except:
                data = {"raw_message": request.data.decode('utf-8')}
        
        print(f"📊 Data received: {data}")
        
        if not data:
            send_telegram("❌ No data received from TradingView")
            return "No data received", 400
        
        # Extract data
        symbol = data.get('symbol', 'Unknown')
        price = data.get('price', 'Unknown')
        score = data.get('script_strength', 'Unknown')
        timeframe = data.get('timeframe', '1H')
        
        # Send initial notification
        send_telegram(f"🎯 *SIGNAL TRIGGERED*\n━━━━━━━━━━━━━━━━━━━━━━\nSymbol: {symbol}\nPrice: {price}\nScore: {score}/100\nTimeframe: {timeframe}\n\n*Analyzing with AI...*")
        
        # Simple response without DeepSeek/ChatGPT (for testing)
        send_telegram(f"✅ *Bot is working!*\n\nYour TradingView alert reached the bot successfully.\n\nSymbol: {symbol}\nPrice: {price}\n\n*Next step:* Add $5 to DeepSeek and ChatGPT for real AI analysis.")
        
        return "OK", 200
        
    except Exception as e:
        error_msg = f"❌ Error: {str(e)[:200]}"
        print(error_msg)
        send_telegram(error_msg)
        return error_msg, 500

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

@app.route('/', methods=['GET'])
def root():
    return "Trading Bot is running. Webhook endpoint at /webhook", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
