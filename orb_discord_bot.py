#!/usr/bin/env python3
"""
ORB DISCORD TRADING BOT
Discord bot that handles ORB trading automation via commands and webhooks
Runs in the cloud - no local files needed!
"""

import discord
from discord.ext import commands, tasks
import asyncio
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import aiohttp
import yfinance as yf
from flask import Flask, request, jsonify
import threading
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ORBSignal:
    """ORB trading signal"""
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    orb_high: float
    orb_low: float
    timestamp: datetime
    signal_strength: str = "medium"

@dataclass
class Position:
    """Trading position"""
    symbol: str
    direction: str
    entry_price: float
    quantity: int
    stop_loss: float
    take_profit: float
    entry_time: datetime
    unrealized_pnl: float = 0.0

class ORBTradingBot(commands.Bot):
    """Discord bot for ORB trading automation"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        
        # Trading settings
        self.account_balance = 10000.0
        self.risk_per_trade = 0.02  # 2%
        self.max_position_size = 1000  # $1000 max per trade
        self.max_daily_trades = 10
        self.daily_trades = 0
        
        # Active positions
        self.positions: Dict[str, Position] = {}
        
        # Trading status
        self.trading_enabled = False
        self.paper_trading = True
        
        # Flask app for webhooks
        self.flask_app = Flask(__name__)
        self.setup_flask_routes()
        
        # Channel for trading alerts
        self.trading_channel_id = None
        
    async def on_ready(self):
        """Bot startup"""
        logger.info(f'{self.user} has connected to Discord!')
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="📊 ORB Signals"))
        
        # Start background tasks
        self.monitor_positions.start()
        self.daily_reset.start()
        
        # Send startup notification
        if self.trading_channel_id:
            channel = self.get_channel(self.trading_channel_id)
            if channel:
                embed = discord.Embed(
                    title="🤖 ORB Trading Bot Online",
                    description="Ready to receive TradingView signals and execute trades!",
                    color=0x00ff00
                )
                embed.add_field(name="Status", value="✅ Online", inline=True)
                embed.add_field(name="Mode", value="📝 Paper Trading" if self.paper_trading else "💰 Live Trading", inline=True)
                embed.add_field(name="Account", value=f"${self.account_balance:,.2f}", inline=True)
                await channel.send(embed=embed)
    
    def setup_flask_routes(self):
        """Setup Flask routes for TradingView webhooks"""
        
        @self.flask_app.route('/webhook', methods=['POST'])
        def webhook():
            try:
                data = request.get_json()
                if data:
                    asyncio.run_coroutine_threadsafe(
                        self.process_webhook(data), 
                        self.loop
                    )
                    return jsonify({"status": "success"})
                return jsonify({"status": "error", "message": "No data"}), 400
            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500
        
        @self.flask_app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                "status": "healthy",
                "positions": len(self.positions),
                "balance": self.account_balance,
                "trading_enabled": self.trading_enabled
            })
    
    async def process_webhook(self, data):
        """Process incoming TradingView webhook"""
        try:
            signal = self.parse_signal(data)
            if signal and self.trading_enabled:
                await self.handle_orb_signal(signal)
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
    
    def parse_signal(self, data: dict) -> Optional[ORBSignal]:
        """Parse TradingView signal data"""
        required_fields = ['symbol', 'direction', 'entry_price', 'stop_loss', 'take_profit']
        
        if not all(field in data for field in required_fields):
            return None
        
        return ORBSignal(
            symbol=data['symbol'],
            direction=data['direction'].lower(),
            entry_price=float(data['entry_price']),
            stop_loss=float(data['stop_loss']),
            take_profit=float(data['take_profit']),
            orb_high=float(data.get('orb_high', 0)),
            orb_low=float(data.get('orb_low', 0)),
            timestamp=datetime.now(),
            signal_strength=data.get('signal_strength', 'medium')
        )
    
    async def handle_orb_signal(self, signal: ORBSignal):
        """Handle incoming ORB signal"""
        # Validate signal
        if not self.validate_signal(signal):
            return
        
        # Check if already have position
        if signal.symbol in self.positions:
            return
        
        # Calculate position size
        position_size = self.calculate_position_size(signal)
        if position_size == 0:
            return
        
        # Execute trade
        await self.execute_trade(signal, position_size)
    
    def validate_signal(self, signal: ORBSignal) -> bool:
        """Validate trading signal"""
        # Check daily trade limit
        if self.daily_trades >= self.max_daily_trades:
            return False
        
        # Check account balance
        if self.account_balance <= 0:
            return False
        
        # Validate stop/target levels
        if signal.direction == 'long':
            if signal.stop_loss >= signal.entry_price or signal.take_profit <= signal.entry_price:
                return False
        else:
            if signal.stop_loss <= signal.entry_price or signal.take_profit >= signal.entry_price:
                return False
        
        # Check risk/reward ratio
        risk = abs(signal.entry_price - signal.stop_loss)
        reward = abs(signal.take_profit - signal.entry_price)
        if risk > 0 and reward / risk < 1.5:
            return False
        
        return True
    
    def calculate_position_size(self, signal: ORBSignal) -> int:
        """Calculate position size based on risk"""
        risk_per_share = abs(signal.entry_price - signal.stop_loss)
        if risk_per_share == 0:
            return 0
        
        risk_amount = self.account_balance * self.risk_per_trade
        max_shares_by_risk = int(risk_amount / risk_per_share)
        max_shares_by_value = int(self.max_position_size / signal.entry_price)
        
        return min(max_shares_by_risk, max_shares_by_value)
    
    async def execute_trade(self, signal: ORBSignal, quantity: int):
        """Execute the trade"""
        # Create position
        position = Position(
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            quantity=quantity,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            entry_time=datetime.now()
        )
        
        # Add to positions
        self.positions[signal.symbol] = position
        self.daily_trades += 1
        
        # Send Discord notification
        await self.send_trade_notification(signal, position)
    
    async def send_trade_notification(self, signal: ORBSignal, position: Position):
        """Send trade notification to Discord"""
        if not self.trading_channel_id:
            return
        
        channel = self.get_channel(self.trading_channel_id)
        if not channel:
            return
        
        # Calculate values
        position_value = position.quantity * position.entry_price
        risk_amount = abs(position.entry_price - position.stop_loss) * position.quantity
        reward_amount = abs(position.take_profit - position.entry_price) * position.quantity
        
        embed = discord.Embed(
            title=f"🚀 ORB Trade Executed - {signal.symbol}",
            description=f"**{signal.direction.upper()}** position opened",
            color=0x00ff00 if signal.direction == 'long' else 0xff0000
        )
        
        embed.add_field(name="📊 Entry", value=f"${signal.entry_price}", inline=True)
        embed.add_field(name="🛑 Stop Loss", value=f"${signal.stop_loss}", inline=True)
        embed.add_field(name="🎯 Take Profit", value=f"${signal.take_profit}", inline=True)
        
        embed.add_field(name="📈 Quantity", value=f"{position.quantity} shares", inline=True)
        embed.add_field(name="💰 Position Value", value=f"${position_value:.2f}", inline=True)
        embed.add_field(name="⚖️ Risk/Reward", value=f"${risk_amount:.0f}/${reward_amount:.0f}", inline=True)
        
        embed.add_field(name="🎪 ORB High", value=f"${signal.orb_high}", inline=True)
        embed.add_field(name="🎪 ORB Low", value=f"${signal.orb_low}", inline=True)
        embed.add_field(name="💪 Strength", value=signal.signal_strength.title(), inline=True)
        
        embed.set_footer(text=f"Trade #{self.daily_trades} • Paper Trading" if self.paper_trading else f"Trade #{self.daily_trades} • Live Trading")
        
        await channel.send(embed=embed)
    
    @tasks.loop(minutes=1)
    async def monitor_positions(self):
        """Monitor active positions"""
        if not self.positions:
            return
        
        for symbol, position in list(self.positions.items()):
            current_price = await self.get_current_price(symbol)
            if current_price is None:
                continue
            
            # Update unrealized P&L
            if position.direction == 'long':
                position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
            else:
                position.unrealized_pnl = (position.entry_price - current_price) * position.quantity
            
            # Check exit conditions
            should_exit, reason = self.check_exit_conditions(position, current_price)
            if should_exit:
                await self.close_position(symbol, reason, current_price)
    
    def check_exit_conditions(self, position: Position, current_price: float) -> tuple:
        """Check if position should be closed"""
        if position.direction == 'long':
            if current_price <= position.stop_loss:
                return True, "stop_loss"
            elif current_price >= position.take_profit:
                return True, "take_profit"
        else:
            if current_price >= position.stop_loss:
                return True, "stop_loss"
            elif current_price <= position.take_profit:
                return True, "take_profit"
        
        # End of day closure (4 PM EST)
        now = datetime.now()
        if now.hour >= 16:
            return True, "market_close"
        
        return False, ""
    
    async def close_position(self, symbol: str, reason: str, exit_price: float):
        """Close a position"""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        # Calculate realized P&L
        if position.direction == 'long':
            realized_pnl = (exit_price - position.entry_price) * position.quantity
        else:
            realized_pnl = (position.entry_price - exit_price) * position.quantity
        
        # Update account balance
        self.account_balance += realized_pnl
        
        # Send exit notification
        await self.send_exit_notification(position, reason, exit_price, realized_pnl)
        
        # Remove position
        del self.positions[symbol]
    
    async def send_exit_notification(self, position: Position, reason: str, exit_price: float, pnl: float):
        """Send position exit notification"""
        if not self.trading_channel_id:
            return
        
        channel = self.get_channel(self.trading_channel_id)
        if not channel:
            return
        
        color = 0x00ff00 if pnl > 0 else 0xff0000
        emoji = "📈" if pnl > 0 else "📉"
        
        embed = discord.Embed(
            title=f"{emoji} Position Closed - {position.symbol}",
            description=f"**{reason.replace('_', ' ').title()}**",
            color=color
        )
        
        embed.add_field(name="💰 P&L", value=f"${pnl:+.2f}", inline=True)
        embed.add_field(name="📊 Exit Price", value=f"${exit_price}", inline=True)
        embed.add_field(name="⏱️ Duration", value=str(datetime.now() - position.entry_time).split('.')[0], inline=True)
        
        embed.add_field(name="📈 Entry", value=f"${position.entry_price}", inline=True)
        embed.add_field(name="🔚 Exit", value=f"${exit_price}", inline=True)
        embed.add_field(name="💼 New Balance", value=f"${self.account_balance:.2f}", inline=True)
        
        await channel.send(embed=embed)
    
    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current stock price"""
        try:
            ticker = yf.Ticker(symbol)
            data = ticker.history(period="1d", interval="1m")
            if not data.empty:
                return float(data['Close'].iloc[-1])
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
        return None
    
    @tasks.loop(hours=24)
    async def daily_reset(self):
        """Reset daily counters"""
        now = datetime.now()
        if now.hour == 9 and now.minute == 30:  # 9:30 AM
            self.daily_trades = 0
            logger.info("Daily counters reset")
    
    # Discord Commands
    
    @commands.command(name='orb_start')
    async def start_trading(self, ctx):
        """Start ORB trading"""
        self.trading_enabled = True
        self.trading_channel_id = ctx.channel.id
        
        embed = discord.Embed(
            title="🟢 ORB Trading Started",
            description="Bot is now accepting TradingView signals",
            color=0x00ff00
        )
        embed.add_field(name="Mode", value="📝 Paper Trading" if self.paper_trading else "💰 Live Trading", inline=True)
        embed.add_field(name="Webhook URL", value="Use `/webhook` endpoint for TradingView", inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='orb_stop')
    async def stop_trading(self, ctx):
        """Stop ORB trading"""
        self.trading_enabled = False
        
        embed = discord.Embed(
            title="🔴 ORB Trading Stopped",
            description="Bot will no longer accept new signals",
            color=0xff0000
        )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='orb_status')
    async def trading_status(self, ctx):
        """Get trading status"""
        embed = discord.Embed(
            title="📊 ORB Trading Status",
            color=0x00ff00 if self.trading_enabled else 0xff0000
        )
        
        embed.add_field(name="🤖 Status", value="🟢 Active" if self.trading_enabled else "🔴 Stopped", inline=True)
        embed.add_field(name="💰 Balance", value=f"${self.account_balance:.2f}", inline=True)
        embed.add_field(name="📈 Positions", value=len(self.positions), inline=True)
        
        embed.add_field(name="📊 Today's Trades", value=f"{self.daily_trades}/{self.max_daily_trades}", inline=True)
        embed.add_field(name="🎭 Mode", value="📝 Paper" if self.paper_trading else "💰 Live", inline=True)
        embed.add_field(name="⚡ Risk/Trade", value=f"{self.risk_per_trade*100}%", inline=True)
        
        if self.positions:
            position_list = []
            for symbol, pos in self.positions.items():
                pnl_emoji = "📈" if pos.unrealized_pnl > 0 else "📉"
                position_list.append(f"{pnl_emoji} {symbol} {pos.direction.upper()} ${pos.unrealized_pnl:+.2f}")
            
            embed.add_field(name="🎯 Active Positions", value="\n".join(position_list), inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='orb_settings')
    async def show_settings(self, ctx, setting=None, value=None):
        """View or change trading settings"""
        if setting and value:
            # Update setting
            if setting == 'risk':
                self.risk_per_trade = float(value) / 100
                await ctx.send(f"✅ Risk per trade set to {value}%")
            elif setting == 'max_position':
                self.max_position_size = float(value)
                await ctx.send(f"✅ Max position size set to ${value}")
            elif setting == 'mode':
                if value.lower() in ['paper', 'live']:
                    self.paper_trading = value.lower() == 'paper'
                    await ctx.send(f"✅ Trading mode set to {value.title()}")
            return
        
        # Show current settings
        embed = discord.Embed(title="⚙️ ORB Trading Settings", color=0x0099ff)
        
        embed.add_field(name="💰 Account Balance", value=f"${self.account_balance:,.2f}", inline=True)
        embed.add_field(name="⚡ Risk per Trade", value=f"{self.risk_per_trade*100}%", inline=True)
        embed.add_field(name="📊 Max Position Size", value=f"${self.max_position_size:,.2f}", inline=True)
        embed.add_field(name="🎯 Max Daily Trades", value=self.max_daily_trades, inline=True)
        embed.add_field(name="🎭 Trading Mode", value="📝 Paper" if self.paper_trading else "💰 Live", inline=True)
        
        embed.add_field(name="📋 Usage", value="`!orb_settings risk 2` - Set 2% risk\n`!orb_settings max_position 1500` - Set $1500 max\n`!orb_settings mode paper` - Paper trading", inline=False)
        
        await ctx.send(embed=embed)
    
    @commands.command(name='orb_help')
    async def help_command(self, ctx):
        """Show ORB bot help"""
        embed = discord.Embed(title="🤖 ORB Trading Bot Commands", color=0x0099ff)
        
        commands_list = [
            "🟢 `!orb_start` - Start accepting TradingView signals",
            "🔴 `!orb_stop` - Stop accepting signals", 
            "📊 `!orb_status` - View trading status and positions",
            "⚙️ `!orb_settings` - View/change trading settings",
            "❓ `!orb_help` - Show this help message"
        ]
        
        embed.add_field(name="Commands", value="\n".join(commands_list), inline=False)
        
        embed.add_field(name="🔗 Webhook URL", value="Point TradingView alerts to:\n`https://your-bot-url.com/webhook`", inline=False)
        
        await ctx.send(embed=embed)

def run_flask_app(bot):
    """Run Flask app in separate thread"""
    bot.flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)

def main():
    """Main function"""
    # Create bot instance
    bot = ORBTradingBot()
    
    # Start Flask server in background thread
    flask_thread = threading.Thread(target=run_flask_app, args=(bot,), daemon=True)
    flask_thread.start()
    
    # Run Discord bot
    discord_token = os.environ.get('DISCORD_BOT_TOKEN')
    if not discord_token:
        print("❌ DISCORD_BOT_TOKEN environment variable not set!")
        return
    
    bot.run(discord_token)

if __name__ == "__main__":
    main()
