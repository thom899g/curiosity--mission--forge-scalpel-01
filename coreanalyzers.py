"""
Analysis module for generating contextual intelligence reports
"""
import asyncio
from typing import Dict, Optional
from datetime import datetime, timedelta
from firebase_admin import firestore

from core.fetchers import (
    fetch_token_price,
    fetch_liquidity_data,
    fetch_whale_transactions,
    fetch_twitter_sentiment
)
from utils.logger import log_to_firestore

async def generate_pulse_report(token_address: str, price_change_pct: float) -> Dict:
    """
    Generate a comprehensive 3-point context check report
    Runs all checks in parallel with timeout
    """
    try:
        # Run all checks in parallel
        tasks = [
            fetch_liquidity_data(token_address),
            fetch_whale_transactions(token_address),
            fetch_twitter_sentiment(token_address[:6])  # Use first 6 chars as symbol
        ]
        
        # Execute with timeout
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            await log_to_firestore("WARNING", "generate_pulse_report", f"Timeout generating report for {token_address}")
            return generate_fallback_report(token_address, price_change_pct)
        
        # Process results with graceful degradation
        liquidity_ok, liquidity_value = results[0] if not isinstance(results[0], Exception) else (False, 0.0)
        whale_data = results[1] if not isinstance(results[1], Exception) else {"count": 0, "direction": "neutral"}
        twitter_data = results[2] if not isinstance(results[2], Exception) else {"mentions": 0, "sentiment": "neutral"}
        
        # Generate verdict
        verdict = generate_verdict(price_change_pct, liquidity_ok, whale_data, twitter_data)
        
        return {
            "price_change_pct": price_change_pct,
            "liquidity": {
                "ok": liquidity_ok,
                "value_usd": round(liquidity_value, 2),
                "status": "✅ Adequate" if liquidity_ok else "⚠️ Low" if liquidity_value