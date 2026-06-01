# Trading Bot
Webhook bot: TradingView → DeepSeek + ChatGPT → Telegram

## Setup
1. Add these 4 environment variables on Render:
   - TELEGRAM_TOKEN
   - TELEGRAM_CHAT_ID
   - DEEPSEEK_KEY
   - CHATGPT_KEY

2. TradingView webhook URL:
   https://your-app.onrender.com/webhook

3. TradingView alert message format:
{
  "symbol": "{{ticker}}",
  "price": {{close}},
  "timeframe": "{{interval}}",
  "direction": "BULLISH",
  "score": 85
}
