#!/usr/bin/env python3
"""
SIMPLE TRADINGVIEW → DISCORD NOTIFICATIONS
Just receives TradingView alerts and sends Discord messages
No complex bot commands needed!
"""

from flask import Flask, request, jsonify
import requests
import os
from datetime import datetime

app = Flask(__name__)

# Discord webhook URL (you'll set this in Railway environment variables)
DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL', '')

def send_discord_notification(message, color=0x00ff00):
    """Send message to Discord channel via webhook"""
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord webhook URL not set")
        return
    
    data = {
        "embeds": [{
            "title": "📊 ORB Trading Alert",
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        if response.status_code == 204:
            print("✅ Discord notification sent")
        else:
            print(f"❌ Discord error: {response.status_code}")
    except Exception as e:
        print(f"❌ Discord notification failed: {e}")

@app.route('/webhook', methods=['POST'])
def trading_webhook():
    """Receive TradingView alerts and send Discord notifications"""
    try:
        data = request.get_json()
        print(f"📨 Received: {data}")
        
        # Parse the alert data
        symbol = data.get('symbol', 'Unknown')
        action = data.get('action', 'signal')  # signal, entry, exit
        direction = data.get('direction', '')  # long/short
        price = data.get('price', 0)
        
        # Create different messages based on action type
        if action == 'signal':
            # ORB signal detected
            message = f"🚨 **ORB Signal Detected - {symbol}**\n"
            message += f"📊 Direction: **{direction.upper()}**\n" 
            message += f"💰 Price: **${price}**\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            color = 0xffaa00  # Orange for signals
            
        elif action == 'entry':
            # Trade executed
            stop_loss = data.get('stop_loss', 0)
            take_profit = data.get('take_profit', 0)
            
            message = f"🚀 **Trade Executed - {symbol}**\n"
            message += f"📊 **{direction.upper()}** position opened\n"
            message += f"💰 Entry: **${price}**\n"
            message += f"🛑 Stop Loss: **${stop_loss}**\n"
            message += f"🎯 Take Profit: **${take_profit}**\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            color = 0x00ff00  # Green for entries
            
        elif action == 'exit':
            # Trade closed
            entry_price = data.get('entry_price', 0)
            exit_reason = data.get('reason', 'Unknown')
            pnl = data.get('pnl', 0)
            
            pnl_emoji = "📈" if pnl > 0 else "📉"
            color = 0x00ff00 if pnl > 0 else 0xff0000
            
            message = f"{pnl_emoji} **Position Closed - {symbol}**\n"
            message += f"📊 Reason: **{exit_reason}**\n"
            message += f"💰 Entry: **${entry_price}** → Exit: **${price}**\n"
            message += f"💵 P&L: **${pnl:+.2f}**\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            
        else:
            # Generic message
            message = f"📊 **{symbol}** - {action}\n"
            message += f"💰 Price: **${price}**\n"
            message += f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
            color = 0x0099ff
        
        # Send to Discord
        send_discord_notification(message, color)
        
        return jsonify({"status": "success", "message": "Alert processed"})
        
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "discord_webhook_configured": bool(DISCORD_WEBHOOK_URL),
        "timestamp": datetime.utcnow().isoformat()
    })

@app.route('/')
def home():
    """Home page"""
    return jsonify({
        "message": "TradingView → Discord Webhook Service",
        "status": "running",
        "endpoints": {
            "webhook": "/webhook (POST) - Receive TradingView alerts",
            "health": "/health (GET) - Health check"
        },
        "discord_configured": bool(DISCORD_WEBHOOK_URL)
    })

@app.route('/test', methods=['POST', 'GET'])
def test_notification():
    """Test Discord notification"""
    if request.method == 'POST':
        # Test with custom message
        data = request.get_json() or {}
        message = data.get('message', 'Test notification from webhook service')
    else:
        # Simple GET test
        message = "🧪 **Test Alert**\nWebhook service is working correctly!"
    
    send_discord_notification(message)
    return jsonify({"status": "success", "message": "Test notification sent"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("🚀 Starting TradingView → Discord Webhook Service")
    print(f"📡 Webhook URL: http://localhost:{port}/webhook")
    print(f"🔍 Health check: http://localhost:{port}/health")
    print(f"🧪 Test endpoint: http://localhost:{port}/test")
    
    if DISCORD_WEBHOOK_URL:
        print("✅ Discord webhook configured")
    else:
        print("⚠️  Discord webhook URL not set - add DISCORD_WEBHOOK_URL environment variable")
    
    app.run(host='0.0.0.0', port=port, debug=False)
