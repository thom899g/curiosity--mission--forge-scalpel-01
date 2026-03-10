"""
SCALPEL-01 Main Application
Primary entry point running both Discord bot and Flask server for Stripe webhooks
"""
import asyncio
import logging
import os
from threading import Thread

import discord
from discord.ext import tasks
from flask import Flask, request, jsonify
from dotenv import load_dotenv

from firebase.client import db, initialize_firebase
from discord_bot.commands import setup_bot_commands
from stripe_handler.webhooks import handle_stripe_webhook
from utils.logger import log_to_firestore

# Load environment variables
load_dotenv()

# Initialize Firebase
initialize_firebase()

# Flask app for Stripe webhooks
app = Flask(__name__)

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    return handle_stripe_webhook(request)

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for deployment platform"""
    return jsonify({"status": "healthy", "service": "scalpel-01"})

class MonitoringLoop:
    """Background monitoring loop for price alerts"""
    
    def __init__(self, bot):
        self.bot = bot
        self.is_running = False
    
    @tasks.loop(seconds=30.0)
    async def check_alerts(self):
        """Check all active alerts for price movements"""
        if not self.is_running:
            return
            
        try:
            from core.analyzers import check_all_alerts
            await check_all_alerts(self.bot, db)
        except Exception as e:
            await log_to_firestore("ERROR", "monitoring_loop", f"Monitoring loop failed: {str(e)}")

@bot.event
async def on_ready():
    """Initialize bot when ready"""
    print(f'{bot.user} has connected to Discord!')
    
    # Setup slash commands
    await setup_bot_commands(bot, tree)
    
    # Sync commands to guilds
    for guild in bot.guilds:
        await tree.sync(guild=guild)
    
    # Start monitoring loop
    monitoring_loop = MonitoringLoop(bot)
    monitoring_loop.is_running = True
    monitoring_loop.check_alerts.start()
    
    await log_to_firestore("INFO", "bot_startup", f"Bot started successfully in {len(bot.guilds)} guilds")

def run_flask():
    """Run Flask server in separate thread"""
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

async def main():
    """Main async entry point"""
    # Start Flask server in background thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Start Discord bot
    bot_token = os.getenv('DISCORD_BOT_TOKEN')
    if not bot_token:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment variables")
    
    await bot.start(bot_token)

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Run the main async function
    asyncio.run(main())