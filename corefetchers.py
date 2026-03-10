"""
Data fetching module with retry logic and fallback mechanisms
"""
import asyncio
import aiohttp
from typing import Dict, Optional, Tuple
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import os
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
import requests
from requests.exceptions import RequestException, Timeout
import json

from utils.logger import log_to_firestore

# Initialize Web3 with fallback RPCs
RPC_URLS = [
    os.getenv('ALCHEMY_RPC_URL'),
    os.getenv('INFURA_RPC_URL'),
    'https://eth.llamarpc.com',  # Public fallback
]
w3 = None
for rpc in RPC_URLS:
    if rpc:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 10}))
            if w3.is_connected():
                break
        except:
            continue

# Uniswap V2 Pool ABI (simplified)
UNISWAP_V2_POOL_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    }
]

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((RequestException, Timeout)),
    before_sleep=lambda retry_state: log_to_firestore("WARNING", "fetch_price", f"Retry {retry_state.attempt_number} for price fetch")
)
async def fetch_token_price(token_address: str) -> Optional[float]:
    """Fetch token price from decentralized oracle with fallback to centralized API"""
    # Try decentralized oracle first (Chainlink)
    try:
        # Simplified - would need actual Chainlink oracle addresses per token
        # For MVP, use CoinGecko API
        pass
    
    # Fallback to CoinGecko
    async with aiohttp.ClientSession() as session:
        try:
            # Get token ID from contract (simplified)
            async with session.get(
                f"https://api.coingecko.com/api/v3/coins/ethereum/contract/{token_address}",
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('market_data', {}).get('current_price', {}).get('usd')
        except:
            pass
        
        # Final fallback to CoinMarketCap
        cmc_key = os.getenv('COINMARKETCAP_API_KEY')
        if cmc_key:
            headers = {'X-CMC_PRO_API_KEY': cmc_key}
            async with session.get(
                f"https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest?address={token_address}",
                headers=headers,
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    # Parse CMC response
                    pass
    
    await log_to_firestore("ERROR", "fetch_token_price", f"All price fetches failed for {token_address}")
    return None

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=5),
    retry=retry_if_exception_type((BadFunctionCallOutput, ContractLogicError))
)
async def fetch_liquidity_data(token_address: str) -> Tuple[bool, float]:
    """Fetch liquidity data from Uniswap V2 pools"""
    if not w3 or not w3.is_connected():
        await log_to_firestore("ERROR", "fetch_liquidity_data", "Web3 not connected")
        return False, 0.0
    
    try:
        # Find WETH pair (simplified - would need factory contract)
        # For MVP, check common pair addresses
        weth_address = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        
        # Uniswap V2 Factory
        factory_address = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
        
        # Calculate pair address
        token0 = token_address.lower() if int(token_address, 16) < int(weth_address, 16) else weth_address
        token1 = weth_address if token0 == token_address.lower() else token_address.lower()
        
        # Create pair address
        pair_address = Web3.to_checksum_address(
            Web3.keccak(
                hexstr=f"0xff{factory_address[2:]}{Web3.keccak(hexstr=f'{token0[2:]}{token1[2:]}').hex()[2:]}ffe5e69d0e8a8b8c8c"
            )[-20:]
        )
        
        # Get pool contract
        pool_contract = w3.eth.contract(address=pair_address, abi=UNISWAP_V2_POOL_ABI)
        
        # Get reserves
        reserves = pool_contract.functions.getReserves().call()
        
        # Calculate liquidity in USD (simplified)
        eth_price = await fetch_token_price(weth_address)
        if eth_price:
            if token0 == weth_address:
                liquidity_usd = (reserves[0] / 1e18) * eth_price * 2
            else:
                liquidity_usd = (reserves[1] / 1e18) * eth_price * 2
            
            liquidity_ok = liquidity_usd > 10000  # $10k minimum
            return liquidity_ok, liquidity_usd
    
    except Exception as e:
        await log_to_firestore("ERROR", "fetch_liquidity_data", f"Liquidity fetch failed: {str(e)}")
    
    return False, 0.0

async def fetch_whale_transactions(token_address: str, time_window_minutes: int = 5) -> Dict:
    """Fetch recent large transactions for whale activity"""
    try:
        # Use Etherscan API
        etherscan_key = os.getenv('ETHERSCAN_API_KEY')
        if not etherscan_key:
            return {"count": 0, "direction": "neutral"}
        
        # Get recent transactions
        async with aiohttp.ClientSession() as session:
            url = f"https://api.etherscan.io/api?module=account&action=tokentx&contractaddress={token_address}&page=1&offset=10&sort=desc&apikey={etherscan_key}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    if data['status'] == '1':
                        txs = data['result'][:10]  # Last 10 transactions
                        
                        # Analyze for whale activity (>5 ETH worth)
                        whale_buys = 0
                        whale_sells = 0
                        
                        for tx in txs:
                            value_eth = float(tx['value']) / 1e18  # Assuming 18 decimals
                            token_price = await fetch_token_price(token_address)
                            
                            if token_price:
                                usd_value = value_eth * token_price
                                
                                if usd_value > 5000:  # $5k threshold
                                    if tx['to'].lower() != token_address.lower():
                                        whale_buys += 1
                                    else:
                                        whale_sells += 1
                        
                        return {
                            "count": whale_buys + whale_sells,
                            "direction": "buy" if whale_buys > whale_sells else "sell" if whale_sells > whale_buys else "neutral",
                            "buy_count": whale_buys,
                            "sell_count": whale_sells
                        }
    
    except Exception as e:
        await log_to_firestore("ERROR", "fetch_whale_transactions", f"Whale tx fetch failed: {str(e)}")
    
    return {"count": 0, "direction": "neutral", "buy_count": 0, "sell_count": 0}

async def fetch_twitter_sentiment(token_symbol: str) -> Dict:
    """Fetch Twitter sentiment data (simplified)"""
    # Note: Twitter API v2 requires elevated access
    # For MVP, use a simpler approach or mock data
    
    bearer_token = os.getenv('TWITTER_BEARER_TOKEN')
    if not bearer_token:
        # Return neutral sentiment if no API key
        return {"mentions": 0, "sentiment": "neutral", "change_pct": 0}
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {bearer_token}"}
            
            # Search recent tweets
            url = f"https://api.twitter.com/2/tweets/search/recent?query={token_symbol} crypto&max_results=100"
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Simplified sentiment analysis
                    tweet_count = len(data.get('data', []))
                    
                    # Mock sentiment calculation (would use NLP in production)
                    sentiment = "neutral"
                    if tweet_count > 50:
                        sentiment = "positive"
                    elif tweet_count > 20:
                        sentiment = "neutral"
                    else:
                        sentiment = "negative"
                    
                    return {
                        "mentions": tweet_count,
                        "sentiment": sentiment,
                        "change_pct": 0  # Would compare to baseline
                    }
    
    except Exception as e:
        await log_to_firestore("ERROR", "fetch_twitter_sentiment", f"Twitter fetch failed: {str(e)}")
    
    return {"mentions": 0, "sentiment": "neutral", "change_pct": 0}