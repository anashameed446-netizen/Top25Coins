import os
import time
import threading
from datetime import datetime
from typing import Dict, Optional, List
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import numpy as np
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import json
from dotenv import load_dotenv
import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import requests

# Load environment variables from .env file
load_dotenv()

# Configuration file path
config_file = 'config.ini'

# Configuration - Load from config file or environment
def load_config():
    """Load configuration from config.ini or environment variables"""
    config = configparser.ConfigParser()
    api_key = os.getenv('BINANCE_API_KEY', '')
    api_secret = os.getenv('BINANCE_API_SECRET', '')
    testnet = os.getenv('BINANCE_TESTNET', 'False').lower() == 'true'
    
    if os.path.exists(config_file):
        config.read(config_file)
        if 'BINANCE' in config:
            api_key = config['BINANCE'].get('api_key', api_key)
            api_secret = config['BINANCE'].get('api_secret', api_secret)
            testnet = config['BINANCE'].getboolean('testnet', testnet)
    
    return api_key, api_secret, testnet

def save_config(api_key, api_secret, testnet=False, rsi_buy=None, rsi_sell=None, take_profit_rsi=None):
    """Save configuration to config.ini"""
    try:
        config = configparser.ConfigParser()
        config['BINANCE'] = {
            'api_key': api_key,
            'api_secret': api_secret,
            'testnet': str(testnet)
        }
        # Add RSI settings if provided, otherwise preserve existing values
        if rsi_buy is not None:
            rsi_config = {
                'buy_rsi': str(rsi_buy),
                'sell_rsi': str(rsi_sell)
            }
            if take_profit_rsi is not None:
                rsi_config['take_profit_rsi'] = str(take_profit_rsi)
            else:
                existing = load_rsi_config()
                rsi_config['take_profit_rsi'] = str(existing.get('take_profit_rsi', 100.0))
            config['RSI'] = rsi_config
        else:
            existing = load_rsi_config()
            config['RSI'] = {
                'buy_rsi': str(existing['buy_rsi']),
                'sell_rsi': str(existing['sell_rsi']),
                'take_profit_rsi': str(existing.get('take_profit_rsi', 100.0))
            }
        with open(config_file, 'w') as f:
            config.write(f)
        print(f"✅ Configuration saved to {config_file}")
        return True
    except Exception as e:
        print(f"❌ Error saving config to {config_file}: {e}")
        return False

def load_rsi_config():
    """Load RSI configuration from config.ini"""
    config = configparser.ConfigParser()
    if os.path.exists(config_file):
        config.read(config_file)
        if 'RSI' in config:
            return {
                'buy_rsi': float(config['RSI'].get('buy_rsi', 70.0)),
                'sell_rsi': float(config['RSI'].get('sell_rsi', 69.0)),
                'take_profit_rsi': float(config['RSI'].get('take_profit_rsi', 100.0))  # Default to 100 (disabled)
            }
    return {
        'buy_rsi': 70.0,
        'sell_rsi': 69.0,
        'take_profit_rsi': 100.0  # Default to 100 (disabled)
    }

API_KEY, API_SECRET, TESTNET = load_config()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False, 
                    ping_timeout=60, ping_interval=25, async_mode='threading', 
                    allow_upgrades=True, transports=['polling', 'websocket'],
                    max_http_buffer_size=1e8)

# Global state
coins_data = {}
active_trade = None
trade_history = []
bot_running = False
trading_enabled = False  # Trading control flag

# Trade data persistence
TRADE_DATA_FILE = 'trade_data.json'
IGNORED_SYMBOLS = {'AEURUSDT', 'EURIUSDT'}
IGNORED_ASSETS = {'AEUR', 'EURI'}


def load_trade_data():
    """Load trade history and active trade from disk"""
    global trade_history, active_trade
    if os.path.exists(TRADE_DATA_FILE):
        try:
            with open(TRADE_DATA_FILE, 'r') as f:
                data = json.load(f)
                trade_history = data.get('trade_history', [])
                active_trade = data.get('active_trade')
                print(f"📁 Loaded {len(trade_history)} trades from {TRADE_DATA_FILE}")
        except Exception as e:
            print(f"⚠️  Could not load trade data from {TRADE_DATA_FILE}: {e}")


def save_trade_data():
    """Persist trade history and active trade to disk"""
    try:
        data = {
            'trade_history': trade_history,
            'active_trade': active_trade
        }
        with open(TRADE_DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️  Could not save trade data to {TRADE_DATA_FILE}: {e}")


def _trade_key(trade: Dict) -> tuple:
    """Create a hashable key for a trade entry"""
    symbol = trade.get('symbol')
    buy_time = trade.get('buy_time') or ''
    sell_time = trade.get('sell_time') or ''
    quantity = round(float(trade.get('quantity', 0) or 0), 8)
    return (symbol, buy_time, sell_time, quantity)


def merge_trade_histories(remote_trades: Optional[List[Dict]], local_trades: List[Dict]) -> List[Dict]:
    """Merge remote Binance trades with locally stored trades, preserving RSI data from local store."""
    remote_trades = remote_trades or []
    local_map = {_trade_key(t): t for t in local_trades}
    seen_keys = set()
    merged = []

    for trade in remote_trades:
        key = _trade_key(trade)
        local = local_map.get(key)
        if local:
            trade = {**trade}  # shallow copy
            trade['buy_rsi'] = local.get('buy_rsi')
            trade['sell_rsi'] = local.get('sell_rsi')
            if not trade.get('buy_time') and local.get('buy_time'):
                trade['buy_time'] = local['buy_time']
            if not trade.get('sell_time') and local.get('sell_time'):
                trade['sell_time'] = local['sell_time']
        else:
            trade = {**trade}
            trade.setdefault('buy_rsi', None)
            trade.setdefault('sell_rsi', None)
        merged.append(trade)
        seen_keys.add(key)

    for key, local in local_map.items():
        if key not in seen_keys:
            merged.append(local.copy())

    merged.sort(key=lambda t: t.get('sell_time') or t.get('buy_time') or '', reverse=True)
    return merged


# Load trade data at startup (before bot starts)
load_trade_data()

class BinanceRSIBot:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, rsi_config: dict = None):
        """Initialize Binance client"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client = None
        self.geo_blocked = False
        
        # Try to initialize client, handle geographic restrictions gracefully
        try:
            # Configure client with optimized settings for server environments
            client_config = {
                'requests_params': {
                    'timeout': 10,  # 10 second timeout
                }
            }
            
            if testnet:
                self.client = Client(api_key, api_secret, testnet=True, **client_config)
            else:
                self.client = Client(api_key, api_secret, **client_config)
            
            # Optimize session for connection pooling and retries
            # python-binance uses requests.Session internally
            try:
                # Access the session through the client's internal request method
                if hasattr(self.client, 'session') and self.client.session:
                    # Configure retry strategy
                    retry_strategy = Retry(
                        total=3,
                        backoff_factor=0.3,
                        status_forcelist=[429, 500, 502, 503, 504],
                    )
                    adapter = HTTPAdapter(
                        max_retries=retry_strategy,
                        pool_connections=10,  # Connection pool size
                        pool_maxsize=20,  # Max connections in pool
                    )
                    self.client.session.mount("http://", adapter)
                    self.client.session.mount("https://", adapter)
                elif hasattr(self.client, '_client') and hasattr(self.client._client, 'session'):
                    # Alternative access path for some binance client versions
                    retry_strategy = Retry(
                        total=3,
                        backoff_factor=0.3,
                        status_forcelist=[429, 500, 502, 503, 504],
                    )
                    adapter = HTTPAdapter(
                        max_retries=retry_strategy,
                        pool_connections=10,
                        pool_maxsize=20,
                    )
                    self.client._client.session.mount("http://", adapter)
                    self.client._client.session.mount("https://", adapter)
            except Exception as e:
                # If we can't optimize session, continue anyway
                print(f"⚠️  Could not optimize session for connection pooling: {e}")
            
            print("✅ Binance API client initialized successfully (optimized for server)")
        except BinanceAPIException as e:
            if e.code == 0 and ("restricted location" in str(e).lower() or "eligibility" in str(e).lower()):
                self.geo_blocked = True
                print("❌ ERROR: Binance API is blocked from this server location")
                print("   Binance does not allow API access from this geographic region")
                print("   Solution: Use a server/VPS in a location where Binance API is accessible")
                print("   The bot cannot function without Binance API access")
                # Don't set client - bot will fail gracefully in run() method
            else:
                print(f"❌ ERROR: Binance API connection failed: {e}")
                print(f"   Code: {e.code}")
        except Exception as e:
            print(f"❌ ERROR: Failed to initialize Binance client: {e}")
            print(f"   The bot cannot function without Binance API access")
        
        self.rsi_period = 14
        # RSI thresholds - single values only
        if rsi_config:
            self.rsi_buy = float(rsi_config.get('buy_rsi', 70.0))
            self.rsi_sell = float(rsi_config.get('sell_rsi', 69.0))
            self.take_profit_rsi = float(rsi_config.get('take_profit_rsi', 100.0))  # Default 100 = disabled
        else:
            rsi_cfg = load_rsi_config()
            self.rsi_buy = rsi_cfg['buy_rsi']
            self.rsi_sell = rsi_cfg['sell_rsi']
            self.take_profit_rsi = rsi_cfg.get('take_profit_rsi', 100.0)  # Default 100 = disabled
        self.active_trade = None
        if active_trade:
            self.active_trade = active_trade.copy()
        self.update_interval = 1  # seconds - near real-time updates (1 second refresh)
        # Track previous RSI values to detect crossing above buy threshold
        self.previous_rsi = {}  # {symbol: previous_rsi_value}
        # Track API permission status
        self.has_account_permissions = None  # None = unknown, True = has permissions, False = no permissions
        # Cache for klines data to reduce API calls (using 1-hour candles for RSI calculation, like TradingView 1h interval)
        self.klines_cache = {}  # {symbol: {'data': prices, 'timestamp': time}}
        self.klines_cache_ttl = 300  # Cache klines for 5 minutes (1h candles are stable, we update with live price for real-time RSI)
        # Track last klines fetch time to avoid fetching every second
        self.last_klines_fetch = 0
        self.klines_fetch_interval = 300  # Fetch klines every 5 minutes (1h candles change hourly, but we use live price for updates)
        # Cache for exchange info (rarely changes, cache for 1 hour)
        self.exchange_info_cache = None
        self.exchange_info_cache_timestamp = 0
        self.exchange_info_cache_ttl = 3600  # Cache for 1 hour
        # Cache for ticker data (changes frequently, cache for 5 seconds)
        self.ticker_cache = None
        self.ticker_cache_timestamp = 0
        self.ticker_cache_ttl = 5  # Cache for 5 seconds
        # Thread pool for parallel API calls
        self.executor = ThreadPoolExecutor(max_workers=10)  # Reduced workers to avoid rate limits
        
        # Test API permissions only if client is available
        if self.client:
            self.check_api_permissions()
        else:
            print("⚠️  Skipping API permission check - client not initialized")
            self.has_account_permissions = False
    
    def update_rsi_settings(self, buy_rsi, sell_rsi, take_profit_rsi=None):
        """Update RSI buy/sell thresholds"""
        self.rsi_buy = float(buy_rsi)
        self.rsi_sell = float(sell_rsi)
        if take_profit_rsi is not None:
            self.take_profit_rsi = float(take_profit_rsi)
        take_profit_str = f", Take Profit at RSI {self.take_profit_rsi}" if self.take_profit_rsi < 100 else ", Take Profit: Disabled"
        print(f"📊 RSI settings updated: Buy when RSI crosses above {self.rsi_buy}, Sell when RSI drops to {self.rsi_sell}{take_profit_str}")
    
    def check_buy_condition(self, symbol: str, current_rsi: float) -> bool:
        """Check if RSI crosses above buy threshold (from below)"""
        previous_rsi = self.previous_rsi.get(symbol)
        if previous_rsi is not None:
            # Check if RSI crossed above buy threshold (was below, now above)
            if previous_rsi < self.rsi_buy and current_rsi >= self.rsi_buy:
                return True
        # Also check if current RSI is above threshold and we don't have previous data yet
        # This handles the case where a coin first appears in monitoring and is already above threshold
        # But we only buy if it's clearly above the threshold (not just at threshold)
        elif current_rsi > self.rsi_buy:
            # If we don't have previous RSI, assume it was below threshold if current is significantly above
            # This is a safety check - we prefer to wait for a clear crossing signal
            return False
        return False
    
    def check_sell_condition(self, rsi: float):
        """Check if RSI meets sell condition (either drops to sell threshold or reaches take profit)
        Returns: (should_sell: bool, reason: str)
        """
        # Check take profit condition (if enabled, i.e., < 100)
        if self.take_profit_rsi < 100 and rsi >= self.take_profit_rsi:
            return True, f"Take Profit reached at RSI {self.take_profit_rsi}"
        # Check regular sell condition
        if rsi <= self.rsi_sell:
            return True, f"RSI dropped to {self.rsi_sell}"
        return False, ""
    
    def check_api_permissions(self):
        """Check if API key has trading permissions"""
        try:
            # Try to get account info (requires read permission)
            account = self.client.get_account()
            print("✅ API connection successful - Read permissions OK")
            
            # Try a test order permission check (this will fail if no trading permission)
            # We'll check by trying to get account permissions
            # Note: Binance API doesn't have a direct permission check endpoint
            # So we'll catch the error when trying to trade
            print("⚠️  Trading permissions will be verified when first trade is attempted")
        except BinanceAPIException as e:
            if e.code == -2015:
                print("❌ API Error: Invalid API-key, IP, or permissions")
                print("   Please check:")
                print("   1. API key has 'Enable Spot & Margin Trading' permission")
                print("   2. Your IP address is whitelisted (if IP restrictions enabled)")
                print("   3. API key and secret are correct")
            else:
                print(f"❌ API Error: {e}")
        except Exception as e:
            print(f"❌ Error checking API permissions: {e}")
        
    def get_top_coins(self, limit: int = 25) -> List[str]:
        """Get top 25 USDT-paired coins by 24-hour price change (top gainers)
        
        Uses Binance ticker data which includes 24h priceChangePercent (same as Binance market)
        Only includes USDT pairs with TRADING status (active coins on Binance market)
        """
        if not self.client:
            print("❌ Cannot get top coins: Binance API client is not available")
            return []
        
        try:
            # Get exchange info with caching (rarely changes, cache for 1 hour)
            current_time = time.time()
            if (self.exchange_info_cache is None or 
                current_time - self.exchange_info_cache_timestamp > self.exchange_info_cache_ttl):
                exchange_info = self.client.get_exchange_info()
                self.exchange_info_cache = exchange_info
                self.exchange_info_cache_timestamp = current_time
            else:
                exchange_info = self.exchange_info_cache
            
            active_usdt_symbols = set()
            for symbol_info in exchange_info['symbols']:
                # Only include pairs with TRADING status and USDT as quote currency
                if symbol_info['status'] == 'TRADING' and symbol_info['quoteAsset'] == 'USDT':
                    active_usdt_symbols.add(symbol_info['symbol'])
            
            # Get ticker data with caching (changes frequently, cache for 5 seconds)
            ticker_current_time = time.time()
            if (self.ticker_cache is None or 
                ticker_current_time - self.ticker_cache_timestamp > self.ticker_cache_ttl):
                ticker = self.client.get_ticker()
                self.ticker_cache = ticker
                self.ticker_cache_timestamp = ticker_current_time
            else:
                ticker = self.ticker_cache
            
            # Filter USDT pairs with positive 24h price change (gainers) that are active
            # Binance ticker already includes priceChangePercent (24h change)
            coins_with_change = []
            for ticker_data in ticker:
                symbol = ticker_data['symbol']
                
                # Only process active USDT trading pairs
                if symbol not in active_usdt_symbols:
                    continue
                
                # Get 24h price change percentage from ticker data
                change_percent = float(ticker_data.get('priceChangePercent', 0))
                
                if change_percent > 0:  # Only gainers
                    coins_with_change.append({
                        'symbol': symbol,
                        'change_percent': change_percent,
                        'volume': float(ticker_data.get('quoteVolume', 0))  # For fallback sorting
                    })
            
            # Sort by 24-hour price change percentage (highest first)
            sorted_coins = sorted(coins_with_change, key=lambda x: x['change_percent'], reverse=True)
            
            # Return top coins
            result = [coin['symbol'] for coin in sorted_coins[:limit]]
            return result
        except Exception as e:
            print(f"❌ Error getting top coins: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate RSI indicator (Relative Strength Index)
        
        Formula:
        1. Calculate price changes (delta)
        2. Separate gains and losses
        3. Calculate average gain and average loss over period
        4. RS = Average Gain / Average Loss
        5. RSI = 100 - (100 / (1 + RS))
        
        Returns RSI value between 0 and 100
        """
        if len(prices) < period + 1:
            return None
        
        try:
            df = pd.DataFrame(prices, columns=['close'])
            
            # Step 1: Calculate price changes
            delta = df['close'].diff()
            
            # Step 2: Separate gains (positive changes) and losses (negative changes)
            gain = delta.where(delta > 0, 0.0)  # Set negative changes to 0
            loss = -delta.where(delta < 0, 0.0)  # Set positive changes to 0, make negative positive
            
            # Step 3: Wilder's smoothing (exponential moving average with alpha = 1/period)
            avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
            avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
            
            # Step 4: Calculate RS (Relative Strength)
            avg_loss_safe = avg_loss.replace(0, np.nan)
            rs = avg_gain / avg_loss_safe
            
            # Step 5: Calculate RSI
            rsi = 100 - (100 / (1 + rs))
            # Handle cases where avg_loss is zero (RSI should be 100)
            rsi = rsi.fillna(100.0)
            
            # Get the last (most recent) RSI value
            rsi_value = rsi.iloc[-1]
            
            # Validate and return
            if pd.isna(rsi_value):
                return None
            
            rsi_value = float(rsi_value)
            # Ensure RSI is between 0 and 100 (should always be, but safety check)
            rsi_value = max(0.0, min(100.0, rsi_value))
            
            return round(rsi_value, 2)
        except Exception as e:
            # Silently handle errors to avoid console spam
            return None
    
    def get_klines(self, symbol: str, interval: str = '1h', limit: int = 100, use_cache: bool = True) -> List[float]:
        """Get klines (candlestick data) for RSI calculation with caching"""
        if not self.client:
            return []
        
        # Check cache first (klines are hourly, so we can cache them)
        cache_key = f"{symbol}_{interval}_{limit}"
        if use_cache and cache_key in self.klines_cache:
            cached = self.klines_cache[cache_key]
            if time.time() - cached['timestamp'] < self.klines_cache_ttl:
                return cached['data']
        
        try:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            prices = [float(k[4]) for k in klines]  # Close prices
            
            # Cache the result
            if use_cache:
                self.klines_cache[cache_key] = {
                    'data': prices,
                    'timestamp': time.time()
                }
            
            return prices
        except Exception as e:
            # Return cached data if available even on error
            if use_cache and cache_key in self.klines_cache:
                return self.klines_cache[cache_key]['data']
            return []
    
    def get_current_price(self, symbol: str, silent: bool = False) -> Optional[float]:
        """Get current price of a symbol"""
        if not self.client:
            return None
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except BinanceAPIException as e:
            if not silent:
                if e.code == -1121:
                    # Invalid symbol - don't log this as it's expected for some assets
                    pass
                else:
                    print(f"⚠️  Error getting price for {symbol}: {e.message} (Code: {e.code})")
            return None
        except Exception as e:
            if not silent:
                print(f"⚠️  Error getting price for {symbol}: {e}")
            return None
    
    def detect_existing_positions(self) -> Optional[Dict]:
        """Detect existing positions from Binance account balances
        Returns active_trade dict if position found, None otherwise
        Only treats positions as active trades if there's a recent buy trade (not just a balance)
        """
        try:
            print("🔍 Checking for existing positions in Binance account...")
            account = self.client.get_account()
            balances = {b['asset']: float(b['free']) for b in account['balances']}
            
            # List of stablecoins to exclude (these are just balances, not trades)
            stablecoins = {'FDUSD', 'USDC', 'BUSD', 'TUSD', 'DAI', 'PAXG', 'USDP', 'USDD', 'PYUSD'}
            
            # Find USDT-paired coins with balance > 0 (excluding USDT itself and stablecoins)
            usdt_pairs = []
            for asset, balance in balances.items():
                # Skip USDT, stablecoins, and very small balances
                if asset == 'USDT' or asset in stablecoins or balance < 0.001:
                    continue
                if asset in IGNORED_ASSETS:
                    continue
                    
                symbol = f"{asset}USDT"
                if symbol in IGNORED_SYMBOLS:
                    continue
                # Check if this is a valid trading pair
                try:
                    # Try to get price to verify it's a valid pair (silently to avoid error spam)
                    price = self.get_current_price(symbol, silent=True)
                    if price and price > 0:
                        # Check if there's an open position (buy trades that haven't been fully sold)
                        # This ensures we only treat actual open positions as active trades, not leftover balances
                        try:
                            trades = self.client.get_my_trades(symbol=symbol, limit=100)
                            if trades:
                                # Sort trades by time (oldest first)
                                trades_sorted = sorted(trades, key=lambda x: x['time'])
                                
                                # Track position using FIFO (First In, First Out)
                                position_queue = []  # List of buy trades
                                open_position = False
                                latest_buy = None
                                
                                for trade in trades_sorted:
                                    is_buy = trade['isBuyer']
                                    qty = float(trade['qty'])
                                    
                                    if is_buy:
                                        # Add to position queue
                                        position_queue.append({
                                            'qty': qty,
                                            'price': float(trade['price']),
                                            'time': trade['time']
                                        })
                                    else:
                                        # Sell - match with buys (FIFO)
                                        remaining_sell_qty = qty
                                        while remaining_sell_qty > 0.0001 and position_queue:
                                            buy_trade = position_queue[0]
                                            if buy_trade['qty'] <= remaining_sell_qty:
                                                # Fully consumed
                                                remaining_sell_qty -= buy_trade['qty']
                                                position_queue.pop(0)
                                            else:
                                                # Partial consumption
                                                buy_trade['qty'] -= remaining_sell_qty
                                                remaining_sell_qty = 0
                                
                                # If there are unmatched buys in the queue, we have an open position
                                if position_queue:
                                    # Get the most recent buy that's still open
                                    latest_buy = max(position_queue, key=lambda x: x['time'])
                                    buy_time_ts = latest_buy['time'] / 1000
                                    days_ago = (time.time() - buy_time_ts) / 86400
                                    
                                    # Only consider positions from last 30 days
                                    if days_ago <= 30:
                                        # Calculate average buy price from open positions
                                        total_qty = sum(b['qty'] for b in position_queue)
                                        weighted_price = sum(b['qty'] * b['price'] for b in position_queue) / total_qty if total_qty > 0 else latest_buy['price']
                                        
                                        # Only add if the balance matches the open position (within 1% tolerance)
                                        if abs(balance - total_qty) / max(balance, total_qty) < 0.01:
                                            usdt_pairs.append({
                                                'symbol': symbol,
                                                'asset': asset,
                                                'quantity': balance,
                                                'current_price': price,
                                                'buy_price': weighted_price,
                                                'buy_time': datetime.fromtimestamp(buy_time_ts).isoformat()
                                            })
                        except Exception as e:
                            # If we can't check trade history, skip this position
                            print(f"   ⚠️  Could not verify position for {symbol}: {e}")
                            continue
                except:
                    # Not a valid USDT pair, skip
                    continue
            
            if not usdt_pairs:
                print("✅ No existing active trades found - starting fresh")
                return None
            
            # If multiple positions found, use the one with highest value
            if len(usdt_pairs) > 1:
                print(f"⚠️  Found {len(usdt_pairs)} active trades: {[p['symbol'] for p in usdt_pairs]}")
                print(f"   Using the position with highest value")
                # Sort by value (quantity * price)
                usdt_pairs.sort(key=lambda x: x['quantity'] * x['current_price'], reverse=True)
            
            position = usdt_pairs[0]
            symbol = position['symbol']
            quantity = position['quantity']
            current_price = position['current_price']
            buy_price = position['buy_price']
            buy_time = position['buy_time']
            
            print(f"📊 Found existing active trade: {symbol}")
            print(f"   Quantity: {quantity}")
            print(f"   Current price: {current_price:.4f}")
            print(f"   Buy price from history: {buy_price:.4f}")
            
            # Create active_trade dict
            active_trade = {
                'symbol': symbol,
                'buy_price': buy_price,
                'quantity': quantity,
                'buy_rsi': None,  # Unknown since we don't have historical RSI
                'buy_time': buy_time
            }
            
            print(f"✅ Restored active trade: {symbol}")
            return active_trade
            
        except BinanceAPIException as e:
            if e.code == -2015:
                # API permission error - suppress verbose logging after first occurrence
                if not hasattr(self, '_permission_error_logged'):
                    print(f"⚠️  API permissions insufficient for account access (code -2015)")
                    print(f"   Bot will continue in monitoring mode. Enable 'Spot & Margin Trading' permission to use trading features.")
                    self._permission_error_logged = True
            else:
                print(f"❌ Error detecting existing positions: {e} (Code: {e.code})")
            return None
        except Exception as e:
            print(f"❌ Error detecting existing positions: {e}")
            return None
    
    def fetch_trade_history_from_binance(self, limit: int = 1000) -> List[Dict]:
        """Fetch trade history from Binance spot trades and reconstruct completed trades
        Returns list of completed trades (buy + sell pairs)
        """
        try:
            print("📜 Fetching trade history from Binance...")
            
            # Get all recent trades from Binance
            all_trades = []
            symbols_traded = set()
            
            # Get exchange info to find all USDT pairs
            exchange_info = self.client.get_exchange_info()
            usdt_symbols = set()
            for symbol_info in exchange_info['symbols']:
                if symbol_info['status'] == 'TRADING' and symbol_info['quoteAsset'] == 'USDT':
                    usdt_symbols.add(symbol_info['symbol'])
            
            # Fetch trades for each USDT symbol (limit to avoid too many API calls)
            # We'll fetch for symbols that are likely to have been traded
            # First, try to get trades for common symbols or check account for balances
            account = self.client.get_account()
            balances = {b['asset']: float(b['free']) + float(b['locked']) for b in account['balances']}
            
            # Get trades for symbols where we have or had positions
            symbols_to_check = set()
            for asset, balance in balances.items():
                if asset == 'USDT' or balance <= 0:
                    continue
                if asset in IGNORED_ASSETS:
                    continue
                symbol = f"{asset}USDT"
                if symbol in IGNORED_SYMBOLS:
                    continue
                if symbol in usdt_symbols:
                    symbols_to_check.add(symbol)
            
            # Limit symbols_to_check to avoid too many API calls (max 30 symbols)
            if len(symbols_to_check) < 30:
                # Also check a few common trading pairs in case we traded them
                common_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'SOLUSDT', 
                                'XRPUSDT', 'DOGEUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT']
                for symbol in common_symbols:
                    if symbol in IGNORED_SYMBOLS:
                        continue
                    if symbol in usdt_symbols and symbol not in symbols_to_check:
                        symbols_to_check.add(symbol)
                        if len(symbols_to_check) >= 30:
                            break
            
            # If no symbols found, check a few common ones
            if not symbols_to_check:
                common_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'SOLUSDT']
                symbols_to_check = {s for s in common_symbols if s in usdt_symbols and s not in IGNORED_SYMBOLS}
            
            print(f"   Checking {len(symbols_to_check)} symbols for trade history...")
            
            # Fetch trades for each symbol
            for symbol in symbols_to_check:
                if symbol in IGNORED_SYMBOLS:
                    continue
                try:
                    trades = self.client.get_my_trades(symbol=symbol, limit=limit)
                    for trade in trades:
                        trade['symbol'] = symbol
                        all_trades.append(trade)
                        symbols_traded.add(symbol)
                except Exception as e:
                    # Some symbols might not have trades, skip silently
                    continue
            
            if not all_trades:
                print("   ✅ No trade history found in Binance")
                return []
            
            print(f"   Found {len(all_trades)} individual trades across {len(symbols_traded)} symbols")
            
            # Sort trades by time (oldest first)
            all_trades.sort(key=lambda x: x['time'])
            
            # Reconstruct completed trades by matching buy and sell orders
            completed_trades = []
            position_queue = {}  # Track open positions per symbol: {symbol: [buy_trades]}
            
            for trade in all_trades:
                symbol = trade['symbol']
                is_buy = trade['isBuyer']
                price = float(trade['price'])
                qty = float(trade['qty'])
                trade_time = datetime.fromtimestamp(trade['time'] / 1000)
                
                if is_buy:
                    # Add to position queue
                    if symbol not in position_queue:
                        position_queue[symbol] = []
                    position_queue[symbol].append({
                        'price': price,
                        'qty': qty,
                        'time': trade_time,
                        'trade_id': trade['id']
                    })
                else:
                    # This is a sell - match with buy orders (FIFO)
                    if symbol in position_queue and position_queue[symbol]:
                        remaining_qty = qty
                        
                        while remaining_qty > 0.0001 and position_queue[symbol]:
                            buy_trade = position_queue[symbol][0]
                            buy_qty = buy_trade['qty']
                            buy_price = buy_trade['price']
                            buy_time = buy_trade['time']
                            
                            if buy_qty <= remaining_qty:
                                # This buy is fully consumed
                                sell_qty = buy_qty
                                position_queue[symbol].pop(0)
                                remaining_qty -= buy_qty
                            else:
                                # Partial consumption
                                sell_qty = remaining_qty
                                buy_trade['qty'] -= remaining_qty
                                remaining_qty = 0
                            
                            # Create completed trade record
                            profit = (price - buy_price) * sell_qty
                            profit_pct = ((price - buy_price) / buy_price) * 100
                            
                            completed_trade = {
                                'symbol': symbol,
                                'buy_price': buy_price,
                                'sell_price': price,
                                'quantity': sell_qty,
                                'buy_rsi': None,  # Not available from Binance API
                                'sell_rsi': None,  # Not available from Binance API
                                'buy_time': buy_time.isoformat(),
                                'sell_time': trade_time.isoformat(),
                                'profit': profit,
                                'profit_pct': profit_pct
                            }
                            completed_trades.append(completed_trade)
            
            # Sort completed trades by sell_time (most recent first)
            completed_trades.sort(key=lambda x: x['sell_time'], reverse=True)
            
            print(f"   ✅ Reconstructed {len(completed_trades)} completed trades from Binance history")
            return completed_trades
            
        except BinanceAPIException as e:
            if e.code == -2015:
                # API permission error - suppress verbose logging after first occurrence
                if not hasattr(self, '_permission_error_logged'):
                    print(f"⚠️  API permissions insufficient for trade history (code -2015)")
                    print(f"   Bot will continue in monitoring mode. Enable 'Spot & Margin Trading' permission to view trade history.")
                    self._permission_error_logged = True
            else:
                print(f"❌ Error fetching trade history from Binance: {e} (Code: {e.code})")
            return []
        except Exception as e:
            print(f"❌ Error fetching trade history from Binance: {e}")
            return []
    
    def buy_order(self, symbol: str, quantity: float) -> Optional[Dict]:
        """Place a buy order using USDT from spot wallet"""
        usdt_balance = 0  # Initialize for error handling
        try:
            # Get account balance for USDT from spot wallet
            account = self.client.get_account()
            balances = {b['asset']: float(b['free']) for b in account['balances']}
            
            # Get USDT balance from spot wallet
            usdt_balance = balances.get('USDT', 0)
            
            if usdt_balance < 5:  # Minimum 5 USDT required
                print(f"❌ Insufficient USDT balance in spot wallet: {usdt_balance:.2f} USDT (minimum: 5 USDT)")
                return None
            
            print(f"💰 Spot wallet USDT balance: {usdt_balance:.2f} USDT")
            
            # Calculate quantity based on available USDT (use 99% to account for trading fees ~0.1% and price fluctuations)
            available_usdt = usdt_balance * 0.99
            current_price = self.get_current_price(symbol)
            
            if current_price is None:
                print(f"❌ Could not get current price for {symbol}")
                return None
            
            # Calculate quantity (with precision)
            quantity = available_usdt / current_price
            
            print(f"💵 Using {available_usdt:.2f} USDT (99% of balance) to buy {symbol}")
            
            # Get symbol info for precision
            exchange_info = self.client.get_symbol_info(symbol)
            if not exchange_info:
                return None
            
            # Get quantity precision and step size
            step_size = None
            quantity_precision = None
            for filter_item in exchange_info['filters']:
                if filter_item['filterType'] == 'LOT_SIZE':
                    step_size = float(filter_item['stepSize'])
                    # Calculate precision from stepSize (count decimal places)
                    # Handle scientific notation and trailing zeros
                    if step_size >= 1:
                        quantity_precision = 0
                    else:
                        step_str = f"{step_size:.10f}".rstrip('0').rstrip('.')
                        if '.' in step_str:
                            quantity_precision = len(step_str.split('.')[1])
                        else:
                            quantity_precision = 0
                    break
            
            if step_size and step_size > 0:
                # Truncate quantity to be a multiple of stepSize (floor division)
                quantity = (quantity // step_size) * step_size
                # Format to exact precision to avoid floating point issues
                if quantity_precision is not None:
                    # Format as string then convert back to float to ensure exact precision
                    quantity = float(f"{quantity:.{quantity_precision}f}")
            
            if quantity <= 0:
                print(f"❌ Calculated quantity is too small: {quantity}")
                return None
            
            # Place market buy order on spot
            order = self.client.create_order(
                symbol=symbol,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity
            )
            
            # Get executed price from order fills
            executed_price = current_price
            executed_qty = quantity
            if order.get('fills'):
                executed_price = float(order['fills'][0].get('price', current_price))
                executed_qty = float(order.get('executedQty', quantity))
            
            total_cost = executed_price * executed_qty
            print(f"✅ BUY ORDER EXECUTED: {symbol}")
            print(f"   Quantity: {executed_qty} | Price: {executed_price:.4f} | Total Cost: {total_cost:.2f} USDT")
            print(f"   Remaining USDT: {usdt_balance - total_cost:.2f} USDT")
            
            return order
            
        except BinanceAPIException as e:
            if e.code == -2015:
                print(f"❌ Binance API Error: Invalid API-key, IP, or permissions for action")
                print(f"   Error Code: -2015")
                print(f"   This means your API key doesn't have trading permissions enabled.")
                print(f"   🔧 Please fix this:")
                print(f"   1. Go to Binance → API Management → Edit your API key")
                print(f"   2. Enable 'Enable Spot & Margin Trading' permission")
                print(f"   3. If IP restrictions are enabled, add your IP address")
                print(f"   4. Save changes and restart the bot or update API credentials in web interface")
            elif e.code == -2010:
                print(f"❌ Binance API Error: Insufficient balance for requested action")
                print(f"   Error Code: -2010")
                print(f"   This means your account doesn't have enough USDT to complete the order.")
                print(f"   🔧 Possible reasons:")
                print(f"   1. Trading fees (~0.1%) need to be covered")
                print(f"   2. Price may have moved slightly (slippage)")
                print(f"   3. Minimum order size requirements")
                print(f"   💡 The bot uses 99% of your balance to account for fees.")
                print(f"   💰 Current balance: {usdt_balance:.2f} USDT")
                print(f"   💡 Try adding more USDT to your spot wallet or the bot will retry on next signal")
            else:
                print(f"❌ Binance API Error during buy: {e} (Code: {e.code})")
            return None
        except Exception as e:
            print(f"❌ Error placing buy order: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def cancel_open_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol"""
        try:
            open_orders = self.client.get_open_orders(symbol=symbol)
            if open_orders:
                print(f"   ⚠️  Found {len(open_orders)} open order(s) for {symbol}, cancelling...")
                for order in open_orders:
                    try:
                        self.client.cancel_order(symbol=symbol, orderId=order['orderId'])
                        print(f"   ✅ Cancelled order {order['orderId']} for {symbol}")
                    except Exception as e:
                        print(f"   ⚠️  Failed to cancel order {order['orderId']}: {e}")
                # Wait a moment for orders to be cancelled
                time.sleep(0.5)
                return True
            return False
        except Exception as e:
            print(f"   ⚠️  Error checking open orders for {symbol}: {e}")
            return False
    
    def sell_order(self, symbol: str, quantity: float) -> Optional[Dict]:
        """Place a sell order"""
        try:
            # Get account balance for the coin (check both free and locked)
            account = self.client.get_account()
            free_balances = {b['asset']: float(b['free']) for b in account['balances']}
            locked_balances = {b['asset']: float(b['locked']) for b in account['balances']}
            total_balances = {b['asset']: float(b['free']) + float(b['locked']) for b in account['balances']}
            
            coin_asset = symbol.replace('USDT', '')
            free_balance = free_balances.get(coin_asset, 0)
            locked_balance = locked_balances.get(coin_asset, 0)
            total_balance = total_balances.get(coin_asset, 0)
            
            # Check if we have any balance at all
            if total_balance < 0.0001:
                print(f"   ⚠️  No {coin_asset} balance found (free: {free_balance:.8f}, locked: {locked_balance:.8f})")
                return None
            
            # If balance is locked, try to cancel open orders first
            if locked_balance > 0.0001 and free_balance < 0.0001:
                print(f"   ⚠️  {coin_asset} balance is locked ({locked_balance:.8f}) - checking for open orders...")
                had_open_orders = self.cancel_open_orders(symbol)
                # Recheck balance after cancelling orders
                account = self.client.get_account()
                free_balances = {b['asset']: float(b['free']) for b in account['balances']}
                locked_balances_new = {b['asset']: float(b['locked']) for b in account['balances']}
                free_balance = free_balances.get(coin_asset, 0)
                locked_balance_new = locked_balances_new.get(coin_asset, 0)
                
                if free_balance < 0.0001:
                    if had_open_orders:
                        print(f"   ⚠️  {coin_asset} balance still locked after cancelling orders.")
                    else:
                        print(f"   ⚠️  {coin_asset} balance is locked but no open orders found.")
                    print(f"   📊 Balance details: Free: {free_balance:.8f}, Locked: {locked_balance_new:.8f}, Total: {total_balance:.8f}")
                    print(f"   💡 Balance may be locked in:")
                    print(f"      - Margin trading account")
                    print(f"      - Futures/derivatives account")
                    print(f"      - Staking or savings products")
                    print(f"      - Other Binance services")
                    print(f"   💡 Please manually transfer balance to Spot wallet or close positions in other accounts")
                    return None
                else:
                    print(f"   ✅ Balance freed after cancelling orders: {free_balance:.8f}")
                    # Update available quantity with freed balance
                    available_quantity = min(quantity, free_balance)
            
            # Check if we have free balance to sell
            if free_balance < 0.0001:
                print(f"   ⚠️  Insufficient free {coin_asset} balance: {free_balance:.8f} (locked: {locked_balance:.8f})")
                return None
            
            # Always use the actual free balance from account (not the passed quantity)
            # The quantity parameter is ignored - we use actual account balance
            # Apply a small safety margin (99.95%) to avoid precision/rounding issues
            # This accounts for any floating point precision errors
            available_quantity = free_balance * 0.9995
            
            # Get symbol info for precision and filters
            exchange_info = self.client.get_symbol_info(symbol)
            if not exchange_info:
                print(f"   ❌ Symbol {symbol} not found in exchange info")
                return None
            
            # Get quantity precision and minimum order size
            quantity_precision = None
            min_qty = 0
            min_notional = 0  # Minimum order value in USDT
            
            for filter_item in exchange_info['filters']:
                if filter_item['filterType'] == 'LOT_SIZE':
                    step_size = float(filter_item['stepSize'])
                    # Calculate precision more accurately
                    if step_size >= 1:
                        quantity_precision = 0
                    else:
                        # Count decimal places, handling scientific notation
                        step_str = f"{step_size:.10f}".rstrip('0')
                        if '.' in step_str:
                            quantity_precision = len(step_str.split('.')[1])
                        else:
                            quantity_precision = 0
                    min_qty = float(filter_item.get('minQty', 0))
                elif filter_item['filterType'] == 'MIN_NOTIONAL':
                    min_notional = float(filter_item.get('minNotional', 0))
            
            # Round quantity DOWN to proper precision (floor, not round)
            # This ensures we never request more than available
            if quantity_precision is not None:
                # Round down to avoid exceeding available balance
                multiplier = 10 ** quantity_precision
                available_quantity = int(available_quantity * multiplier) / multiplier
            else:
                # Round down to 8 decimal places
                available_quantity = int(available_quantity * 100000000) / 100000000
            
            # Final safety check: ensure we never exceed free balance
            available_quantity = min(available_quantity, free_balance)
            
            # Check minimum quantity
            if min_qty > 0 and available_quantity < min_qty:
                print(f"   ⚠️  Quantity {available_quantity} below minimum {min_qty} for {symbol}")
                return None
            
            # Check minimum notional (order value)
            current_price = self.get_current_price(symbol, silent=True)
            if current_price:
                order_value = available_quantity * current_price
                if min_notional > 0 and order_value < min_notional:
                    print(f"   ⚠️  Order value ${order_value:.2f} below minimum ${min_notional:.2f} for {symbol}")
                    return None
            
            if available_quantity <= 0:
                print(f"   ⚠️  Invalid quantity: {available_quantity}")
                return None
            
            # Place market sell order
            try:
                order = self.client.create_order(
                    symbol=symbol,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=available_quantity
                )
                
                executed_price = current_price if current_price else 0
                executed_qty = available_quantity
                
                if order.get('fills'):
                    # Calculate weighted average price from all fills (more accurate)
                    fills = order.get('fills', [])
                    if fills:
                        total_qty = sum(float(f['qty']) for f in fills)
                        total_cost = sum(float(f['price']) * float(f['qty']) for f in fills)
                        if total_qty > 0:
                            executed_price = total_cost / total_qty
                            executed_qty = total_qty
                elif order.get('executedQty'):
                    executed_qty = float(order.get('executedQty', available_quantity))
                else:
                    # Use the quantity we actually sent
                    executed_qty = available_quantity
                
                print(f"   ✅ SELL ORDER EXECUTED: {symbol} | Quantity: {executed_qty} | Price: {executed_price:.8f}")
                return order
                
            except BinanceAPIException as e:
                if e.code == -2010:
                    # Get current balance for better error message
                    try:
                        account_check = self.client.get_account()
                        balances_detail = {b['asset']: {'free': float(b['free']), 'locked': float(b['locked'])} 
                                         for b in account_check['balances'] if b['asset'] == coin_asset}
                        if balances_detail:
                            bal = balances_detail[coin_asset]
                            print(f"   ❌ Insufficient balance for {symbol}: {e.message}")
                            print(f"   📊 Current balance: Free: {bal['free']:.8f}, Locked: {bal['locked']:.8f}, Requested: {available_quantity:.8f}")
                            if bal['locked'] > 0.0001:
                                print(f"   💡 Balance is locked - may be in open orders, margin, futures, or staking")
                        else:
                            print(f"   ❌ Insufficient balance for {symbol}: {e.message}")
                            print(f"   📊 No {coin_asset} balance found in account")
                    except Exception:
                        print(f"   ❌ Insufficient balance for {symbol}: {e.message}")
                elif e.code == -1121:
                    print(f"   ❌ Invalid symbol {symbol}: {e.message}")
                elif e.code == -1013:
                    # Check if it's LOT_SIZE or MIN_NOTIONAL error
                    if "LOT_SIZE" in str(e.message) or "stepSize" in str(e.message):
                        print(f"   ❌ Order would violate LOT_SIZE filter for {symbol}: {e.message}")
                    elif "MIN_NOTIONAL" in str(e.message) or "notional" in str(e.message).lower():
                        print(f"   ❌ Order value too small for {symbol}: {e.message}")
                    else:
                        print(f"   ❌ Filter violation for {symbol}: {e.message}")
                else:
                    print(f"   ❌ Binance API Error selling {symbol}: {e.message} (Code: {e.code})")
                return None
                
        except BinanceAPIException as e:
            print(f"   ❌ Binance API Error during sell for {symbol}: {e.message} (Code: {e.code})")
            return None
        except Exception as e:
            print(f"   ❌ Error placing sell order for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def check_active_positions(self) -> List[Dict]:
        """Check for active positions in Binance account without selling
        Returns list of active positions with details
        Only includes positions with free balance that can actually be sold
        """
        active_positions = []
        
        if not self.client:
            return active_positions
        
        try:
            # Get account balances - separate free and locked
            account = self.client.get_account()
            free_balances = {b['asset']: float(b['free']) for b in account['balances']}
            locked_balances = {b['asset']: float(b['locked']) for b in account['balances']}
            total_balances = {b['asset']: float(b['free']) + float(b['locked']) for b in account['balances']}
            
            # List of stablecoins to exclude (these are just balances, not trades)
            stablecoins = {'FDUSD', 'USDC', 'BUSD', 'TUSD', 'DAI', 'PAXG', 'USDP', 'USDD', 'PYUSD'}
            
            # Get exchange info to verify valid symbols
            try:
                exchange_info = self.client.get_exchange_info()
                valid_symbols = {s['symbol'] for s in exchange_info['symbols'] if s['status'] == 'TRADING'}
            except Exception:
                valid_symbols = set()  # If we can't get exchange info, we'll verify by trying to get price
            
            # Find all non-USDT coins with balance > 0.001
            for asset, total_balance in total_balances.items():
                # Skip USDT, stablecoins, and very small balances
                if asset == 'USDT' or asset in stablecoins or total_balance < 0.0001:
                    continue
                if asset in IGNORED_ASSETS:
                    continue
                
                symbol = f"{asset}USDT"
                if symbol in IGNORED_SYMBOLS:
                    continue
                
                # First check if symbol is valid (if we have exchange info)
                if valid_symbols and symbol not in valid_symbols:
                    # Try alternative symbol format (some coins might have different quote asset)
                    continue
                
                # Verify this is a valid trading pair by checking if we can get price (silently)
                price = self.get_current_price(symbol, silent=True)
                if price and price > 0:
                    free_balance = free_balances.get(asset, 0)
                    locked_balance = locked_balances.get(asset, 0)
                    
                    # Prioritize free balance (can be sold immediately)
                    # But also include positions with locked balance (might be in open orders)
                    sellable_balance = free_balance
                    
                    # If we have locked balance but no free balance, check for open orders
                    if locked_balance > 0.0001 and free_balance < 0.0001:
                        try:
                            open_orders = self.client.get_open_orders(symbol=symbol)
                            if open_orders:
                                # There are open orders - we can try to cancel them and then sell
                                # Include this position but mark it as needing order cancellation
                                sellable_balance = 0  # Will need to cancel orders first
                            else:
                                # Locked but no open orders - might be in margin or other services
                                # Skip for now as we can't sell it
                                continue
                        except Exception:
                            # Can't check orders, skip this position
                            continue
                    
                    # Verify minimum order value (usually 5-10 USDT)
                    # Use free balance for order value calculation
                    if sellable_balance > 0:
                        order_value = sellable_balance * price
                        if order_value >= 5.0:  # Minimum order value check
                            active_positions.append({
                                'symbol': symbol,
                                'asset': asset,
                                'quantity': sellable_balance,  # Use free balance
                                'free_balance': free_balance,
                                'locked_balance': locked_balance,
                                'total_balance': total_balance,
                                'current_price': price,
                                'order_value': order_value,
                                'has_locked_balance': locked_balance > 0.0001
                            })
                    elif locked_balance > 0.0001:
                        # Has locked balance - include it but we'll need to cancel orders first
                        order_value = locked_balance * price
                        if order_value >= 5.0:
                            active_positions.append({
                                'symbol': symbol,
                                'asset': asset,
                                'quantity': locked_balance,  # Will cancel orders first
                                'free_balance': free_balance,
                                'locked_balance': locked_balance,
                                'total_balance': total_balance,
                                'current_price': price,
                                'order_value': order_value,
                                'has_locked_balance': True,
                                'needs_order_cancellation': True
                            })
                    # Note: Very small positions (< 5 USDT) are skipped as they may be below minimum trade size
            
            return active_positions
            
        except BinanceAPIException as e:
            if e.code == -2015:
                print(f"⚠️  API permissions insufficient to check positions (code -2015)")
            else:
                print(f"❌ Binance API Error while checking positions: {e} (Code: {e.code})")
            return active_positions
        except Exception as e:
            print(f"❌ Error checking positions: {e}")
            import traceback
            traceback.print_exc()
            return active_positions
    
    def close_all_positions(self) -> List[Dict]:
        """Close all active positions by selling any non-USDT coins in the account
        Returns list of sold positions with details
        """
        sold_positions = []
        
        if not self.client:
            print("❌ Cannot close positions - Binance client not initialized")
            return sold_positions
        
        try:
            print("🛑 Checking Binance account for active positions to close...")
            
            # Get active positions
            positions_to_close = self.check_active_positions()
            
            if not positions_to_close:
                print("✅ No active positions found in Binance account")
                return sold_positions
            
            print(f"📊 Found {len(positions_to_close)} active position(s) to close:")
            for pos in positions_to_close:
                value = pos.get('order_value', pos['quantity'] * pos['current_price'])
                print(f"   - {pos['symbol']}: {pos['quantity']} (${value:.2f} USDT)")
            
            # Close each position
            for position in positions_to_close:
                symbol = position['symbol']
                asset = position['asset']
                quantity = position['quantity']
                has_locked = position.get('has_locked_balance', False)
                needs_cancellation = position.get('needs_order_cancellation', False)
                
                try:
                    order_value = position.get('order_value', position['quantity'] * position['current_price'])
                    free_balance = position.get('free_balance', quantity)
                    locked_balance = position.get('locked_balance', 0)
                    
                    if needs_cancellation or has_locked:
                        print(f"💰 Attempting to sell {symbol} (quantity: {quantity}, value: ${order_value:.2f})...")
                        if locked_balance > 0:
                            print(f"   ⚠️  Balance is locked ({locked_balance:.8f}) - will cancel open orders first")
                    else:
                        print(f"💰 Attempting to sell {symbol} (quantity: {quantity}, value: ${order_value:.2f})...")
                    
                    # If balance is locked, cancel open orders first
                    if needs_cancellation or (has_locked and free_balance < 0.0001):
                        cancelled = self.cancel_open_orders(symbol)
                        if cancelled:
                            # Wait a moment and recheck balance
                            time.sleep(1)
                    
                    # Always get fresh balance from account before selling
                    # This ensures we use the actual available balance, not cached values
                    account = self.client.get_account()
                    fresh_free_balances = {b['asset']: float(b['free']) for b in account['balances']}
                    fresh_free_balance = fresh_free_balances.get(asset, 0)
                    
                    if fresh_free_balance < 0.0001:
                        print(f"   ⚠️  No free balance available for {symbol} (Free: {fresh_free_balance:.8f})")
                        continue
                    
                    print(f"   📊 Using actual free balance: {fresh_free_balance:.8f} {asset}")
                    
                    # Place sell order with actual free balance (quantity parameter is ignored, we use account balance)
                    order = self.sell_order(symbol, fresh_free_balance)
                    
                    if order:
                        # Get executed details
                        sell_price = position['current_price']
                        sell_quantity = fresh_free_balance  # Use the balance we actually tried to sell
                        
                        if order.get('fills'):
                            # Get average fill price from all fills
                            fills = order.get('fills', [])
                            if fills:
                                total_qty = sum(float(f['qty']) for f in fills)
                                total_cost = sum(float(f['price']) * float(f['qty']) for f in fills)
                                if total_qty > 0:
                                    sell_price = total_cost / total_qty
                        if order.get('executedQty'):
                            sell_quantity = float(order['executedQty'])
                        
                        # Try to get buy price from active_trade if available
                        buy_price = None
                        buy_time = None
                        buy_rsi = None
                        
                        if self.active_trade and self.active_trade.get('symbol') == symbol:
                            buy_price = self.active_trade.get('buy_price')
                            buy_time = self.active_trade.get('buy_time')
                            buy_rsi = self.active_trade.get('buy_rsi')
                        else:
                            # Try to get buy price from trade history
                            try:
                                trades = self.client.get_my_trades(symbol=symbol, limit=50)
                                if trades:
                                    # Get most recent buy trade
                                    buy_trades = [t for t in trades if t['isBuyer']]
                                    if buy_trades:
                                        latest_buy = max(buy_trades, key=lambda x: x['time'])
                                        buy_price = float(latest_buy['price'])
                                        buy_time = datetime.fromtimestamp(latest_buy['time'] / 1000).isoformat()
                            except Exception:
                                pass
                        
                        sold_position = {
                            'symbol': symbol,
                            'quantity': sell_quantity,
                            'sell_price': sell_price,
                            'buy_price': buy_price,
                            'buy_time': buy_time,
                            'buy_rsi': buy_rsi,
                            'sell_time': datetime.now().isoformat()
                        }
                        
                        # Calculate profit if we have buy price
                        if buy_price:
                            sold_position['profit'] = (sell_price - buy_price) * sell_quantity
                            sold_position['profit_pct'] = ((sell_price - buy_price) / buy_price) * 100
                        else:
                            sold_position['profit'] = None
                            sold_position['profit_pct'] = None
                        
                        sold_positions.append(sold_position)
                        
                        print(f"   ✅ Successfully closed {symbol} at {sell_price:.8f}")
                    else:
                        print(f"   ❌ Failed to sell {symbol} - See error messages above for details")
                        
                except Exception as e:
                    print(f"   ❌ Exception while selling {symbol}: {e}")
                    import traceback
                    traceback.print_exc()
                    continue
            
            # Clear active trade if we sold it
            if self.active_trade and any(pos['symbol'] == self.active_trade['symbol'] for pos in sold_positions):
                self.active_trade = None
                print("✅ Active trade cleared")
                save_trade_data()
            
            return sold_positions
            
        except BinanceAPIException as e:
            if e.code == -2015:
                print(f"❌ API permissions insufficient to close positions (code -2015)")
                print(f"   Enable 'Spot & Margin Trading' permission to close positions")
            else:
                print(f"❌ Binance API Error while closing positions: {e} (Code: {e.code})")
            return sold_positions
        except Exception as e:
            print(f"❌ Error closing positions: {e}")
            import traceback
            traceback.print_exc()
            return sold_positions
    
    def _fetch_klines_parallel(self, symbol: str) -> tuple:
        """Helper method to fetch klines for a symbol (for parallel execution)"""
        try:
            # Force fresh fetch (don't use cache in parallel fetch, we check cache separately)
            klines = self.client.get_klines(symbol=symbol, interval='1h', limit=100)
            prices = [float(k[4]) for k in klines]  # Close prices
            
            # Update cache
            cache_key = f"{symbol}_1h_100"
            self.klines_cache[cache_key] = {
                'data': prices,
                'timestamp': time.time()
            }
            
            return symbol, prices, None
        except Exception as e:
            # Return cached data if available on error
            cache_key = f"{symbol}_1h_100"
            if cache_key in self.klines_cache:
                cached = self.klines_cache[cache_key]
                if time.time() - cached['timestamp'] < 3600:  # Use cache up to 1 hour old
                    return symbol, cached['data'], None
            return symbol, [], str(e)
    
    def update_coins_data(self, symbols: List[str], emit_updates: bool = True):
        """Update RSI data for all coins, including 24h change percentage (optimized with parallel requests)"""
        global coins_data
        
        if not self.client:
            return
        
        start_time = time.time()
        
        # Get ticker data once for all symbols (contains prices and 24h change)
        # This is a single API call that gets ALL symbol data (with caching)
        try:
            ticker_data_map = {}
            current_time = time.time()
            # Use cached ticker if available and fresh (cache for 5 seconds)
            if (self.ticker_cache is None or 
                current_time - self.ticker_cache_timestamp > self.ticker_cache_ttl):
                # Retry logic for get_ticker() with exponential backoff
                ticker = None
                max_retries = 3
                retry_delay = 1  # Start with 1 second
                for attempt in range(max_retries):
                    try:
                        ticker = self.client.get_ticker()
                        self.ticker_cache = ticker
                        self.ticker_cache_timestamp = current_time
                        break  # Success, exit retry loop
                    except (ConnectionError, TimeoutError, requests.exceptions.ConnectionError, 
                            requests.exceptions.Timeout, requests.exceptions.ReadTimeout, Exception) as e:
                        if attempt < max_retries - 1:
                            print(f"⚠️  Ticker fetch attempt {attempt + 1}/{max_retries} failed: {type(e).__name__}. Retrying in {retry_delay}s...")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff: 1s, 2s, 4s
                        else:
                            # Last attempt failed, raise the exception
                            raise
                
                if ticker is None:
                    # If all retries failed, use cached data if available
                    if hasattr(self, 'ticker_cache') and self.ticker_cache:
                        print("⚠️  Using cached ticker data due to fetch failure")
                        ticker = self.ticker_cache
                    else:
                        raise Exception("Failed to fetch ticker data after retries and no cache available")
            else:
                ticker = self.ticker_cache
            for ticker_info in ticker:
                symbol = ticker_info['symbol']
                # Extract price (can be 'price' or 'lastPrice')
                price = ticker_info.get('lastPrice') or ticker_info.get('price', '0')
                # Extract 24h change percentage
                change_24h = ticker_info.get('priceChangePercent', 0)
                try:
                    change_24h = float(change_24h) if change_24h else 0
                except (ValueError, TypeError):
                    change_24h = 0
                
                ticker_data_map[symbol] = {
                    'price': float(price) if price else 0,
                    'change_24h': change_24h
                }
        except Exception as e:
            # Only print full traceback for non-timeout errors
            if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, 
                            requests.exceptions.ReadTimeout, TimeoutError)):
                print(f"⚠️  Error fetching ticker data: {type(e).__name__} - {e}")
            else:
                print(f"⚠️  Error fetching ticker data: {e}")
                import traceback
                traceback.print_exc()
            ticker_data_map = {}
            # If error, try to use cached ticker as fallback
            if hasattr(self, 'ticker_cache') and self.ticker_cache:
                try:
                    for ticker_info in self.ticker_cache:
                        symbol = ticker_info['symbol']
                        price = ticker_info.get('lastPrice') or ticker_info.get('price', '0')
                        change_24h = ticker_info.get('priceChangePercent', 0)
                        try:
                            change_24h = float(change_24h) if change_24h else 0
                        except (ValueError, TypeError):
                            change_24h = 0
                        ticker_data_map[symbol] = {
                            'price': float(price) if price else 0,
                            'change_24h': change_24h
                        }
                except Exception as e2:
                    print(f"⚠️  Error using cached ticker: {e2}")
        
        # Use cached klines data immediately for fast updates
        # Only fetch fresh klines if cache is stale (1-hour candles, fetch every 60 seconds)
        current_time = time.time()
        should_fetch_klines = (current_time - self.last_klines_fetch) >= self.klines_fetch_interval
        
        # First, try to get klines from cache for all symbols (fast, no API calls)
        klines_data = {}
        for symbol in symbols:
            cache_key = f"{symbol}_1h_100"
            if cache_key in self.klines_cache:
                cached = self.klines_cache[cache_key]
                # Use cache if less than cache TTL old (1h candles are stable)
                if current_time - cached['timestamp'] < self.klines_cache_ttl:
                    klines_data[symbol] = cached['data']
        
        # Only fetch fresh klines for symbols missing from cache (if enough time passed)
        missing_symbols = [s for s in symbols if s not in klines_data]
        if should_fetch_klines and missing_symbols:
            # Fetch in smaller batches to avoid rate limits (5 symbols at a time)
            batch_size = 5
            for i in range(0, len(missing_symbols), batch_size):
                batch = missing_symbols[i:i + batch_size]
                futures = {}
                
                # Submit batch requests
                for symbol in batch:
                    future = self.executor.submit(self._fetch_klines_parallel, symbol)
                    futures[future] = symbol
                
                # Collect results with shorter timeout (3 seconds per batch)
                try:
                    for future in as_completed(futures, timeout=3):
                        symbol, prices, error = future.result()
                        if not error and prices:
                            klines_data[symbol] = prices
                except FuturesTimeoutError:
                    # Process completed futures, ignore incomplete ones
                    for future in list(futures.keys()):
                        if future.done():
                            try:
                                symbol, prices, error = future.result(timeout=0.1)
                                if not error and prices:
                                    klines_data[symbol] = prices
                            except:
                                pass
                
                # Small delay between batches to avoid rate limits
                if i + batch_size < len(missing_symbols):
                    time.sleep(0.2)
            
            # Update last fetch time
            if missing_symbols:
                self.last_klines_fetch = current_time
        
        # Process all symbols with the fetched/cached data
        updated_count = 0
        for symbol in symbols:
            try:
                # Get prices from cache or fetched data
                prices = klines_data.get(symbol)
                if not prices:
                    # Try to get from cache one more time (might have been updated)
                    prices = self.get_klines(symbol, interval='1h', limit=100, use_cache=True)
                    # If still no prices and we have existing data, keep existing (don't break the update)
                    if not prices and symbol in coins_data:
                        # Keep existing data if API fails - this prevents data loss
                        updated_count += 1
                        continue
                
                if len(prices) >= self.rsi_period + 1:
                    # Get price and 24h change from ticker data (already fetched, no extra API call)
                    ticker_info = ticker_data_map.get(symbol, {})
                    current_price = ticker_info.get('price')
                    change_24h = ticker_info.get('change_24h')
                    
                    # Fallback to API call only if ticker data doesn't have price
                    if current_price is None or current_price == 0:
                        current_price = self.get_current_price(symbol, silent=True)
                    
                    # Calculate "live" RSI by replacing last candle price with current price for real-time updates
                    # This makes RSI update continuously (like TradingView), not just when a new 1-hour candle closes
                    # RSI is calculated from 1h candles (TradingView 1h interval), but updates with live price
                    live_prices = prices.copy()
                    if current_price and current_price > 0:
                        # Replace last candle price with current live price for live RSI calculation
                        live_prices[-1] = current_price
                    
                    # Calculate RSI using live prices (based on 1h candles, like TradingView 1h interval)
                    rsi = self.calculate_rsi(live_prices, self.rsi_period)
                    
                    if rsi is not None and current_price is not None:
                        # Store previous RSI before updating
                        previous_rsi = coins_data.get(symbol, {}).get('rsi')
                        if previous_rsi is not None:
                            self.previous_rsi[symbol] = previous_rsi
                        
                        coins_data[symbol] = {
                            'symbol': symbol,
                            'rsi': round(rsi, 2),
                            'price': round(current_price, 4),
                            'change_24h': round(change_24h, 2) if change_24h is not None else None,
                            'timestamp': datetime.now().isoformat()
                        }
                        updated_count += 1
            except Exception as e:
                # Silently skip errors to avoid spam, but log important ones
                if 'rate limit' in str(e).lower():
                    print(f"⚠️ Rate limit hit for {symbol}, slowing down...")
                continue
        
        # Emit update with all coins (optimized for server performance)
        if emit_updates:
            # Only emit if we have updated data
            if updated_count > 0:
                # Emit update immediately (broadcast to all clients)
                socketio.emit('coins_update', {'coins': list(coins_data.values())}, namespace='/')
        
        # Only log if update took unusually long
        elapsed = time.time() - start_time
        if elapsed > 5.0:  # Only log if it took longer than 5 seconds
            print(f"⚠️ Slow update: {updated_count}/{len(symbols)} coins in {elapsed:.2f}s")
    
    def check_trading_signals(self):
        """Check for buy/sell signals"""
        global active_trade, trade_history, trading_enabled, coins_data
        
        # Only execute trades if trading is enabled
        if not trading_enabled:
            # Monitoring mode - just log signals
            if self.active_trade:
                symbol = self.active_trade['symbol']
                if symbol in coins_data:
                    rsi = coins_data[symbol]['rsi']
                    # Check if RSI meets sell condition
                    should_sell, reason = self.check_sell_condition(rsi)
                    if should_sell:
                        print(f"🔍 [MONITORING] SELL SIGNAL: {symbol} RSI {rsi:.2f} ({reason}) - Trading disabled")
            else:
                for symbol, data in coins_data.items():
                    rsi = data['rsi']
                    if rsi is not None:
                        # Check if RSI crosses above buy threshold
                        if self.check_buy_condition(symbol, rsi):
                            price = data.get('price', 0)
                            change_24h = data.get('change_24h', 0)
                            change_str = f"+{change_24h:.2f}%" if change_24h else "N/A"
                            print(f"\n🟢 [BUY SIGNAL] {symbol} RSI crossed above {self.rsi_buy}!")
                            print(f"   Current RSI: {rsi:.2f} | Price: ${price:.4f} | 24h Change: {change_str}")
                            print(f"   ⚠️  Trading is DISABLED - Enable trading to execute buy order")
                            print()
                            break
            return
        
        # TRADING ENABLED - Execute trades
        # IMPORTANT: If there's an active trade, ONLY check for sell signals
        # No new trades will be started until the current trade closes
        if self.active_trade:
            symbol = self.active_trade['symbol']
            
            # Double-check that we still have an active trade (防止重复卖出)
            if not self.active_trade:
                print("⚠️  Active trade was cleared, skipping sell check")
                return
            
            if symbol in coins_data and coins_data[symbol].get('rsi') is not None:
                rsi = coins_data[symbol]['rsi']
                
                # Check if RSI meets sell condition (either drops to sell threshold or reaches take profit)
                should_sell, reason = self.check_sell_condition(rsi)
                if should_sell:
                    # Final check before selling - ensure we still have an active trade
                    if not self.active_trade or self.active_trade['symbol'] != symbol:
                        print(f"⚠️  Active trade cleared or changed before sell, skipping {symbol}")
                        return
                    
                    print(f"🔄 {reason} ({rsi:.2f}) for {symbol}, selling...")
                    
                    # Place sell order
                    order = self.sell_order(symbol, self.active_trade['quantity'])
                    
                    if order:
                        sell_price = float(order.get('fills', [{}])[0].get('price', coins_data[symbol]['price']))
                        sell_quantity = float(order.get('executedQty', self.active_trade['quantity']))
                        
                        trade_result = {
                            'symbol': symbol,
                            'buy_price': self.active_trade['buy_price'],
                            'sell_price': sell_price,
                            'quantity': sell_quantity,
                            'buy_rsi': self.active_trade['buy_rsi'],
                            'sell_rsi': rsi,
                            'buy_time': self.active_trade['buy_time'],
                            'sell_time': datetime.now().isoformat(),
                            'profit': (sell_price - self.active_trade['buy_price']) * sell_quantity,
                            'profit_pct': ((sell_price - self.active_trade['buy_price']) / self.active_trade['buy_price']) * 100
                        }
                        
                        trade_history.append(trade_result)
                        
                        # Clear active trade BEFORE emitting updates to prevent re-checking same coin
                        sold_symbol = symbol  # Store symbol before clearing trade
                        self.active_trade = None
                        active_trade = None
                        save_trade_data()
                        
                        # Emit trade update immediately (broadcast to all clients)
                        socketio.emit('trade_update', trade_result, namespace='/')
                        socketio.emit('active_trade_update', {'active_trade': None}, namespace='/')
                        print(f"✅ Trade closed: {sold_symbol} | Profit: {trade_result['profit']:.2f} USDT ({trade_result['profit_pct']:.2f}%)")
                        print(f"📊 Active trade cleared - Bot will now check for new buy signals")
                        
                        # Remove sold coin from coins_data to prevent immediate re-check
                        if sold_symbol in coins_data:
                            del coins_data[sold_symbol]
                            print(f"🔄 Removed {sold_symbol} from monitoring to prevent duplicate sell attempts")
                        
                        # Force immediate update after trade to ensure bot continues smoothly
                        time.sleep(0.5)  # Small delay to ensure trade is fully processed
                        return  # Exit to let main loop continue
                    else:
                        # Sell order failed - don't clear active trade, will retry on next check
                        print(f"❌ Sell order failed for {symbol} - will retry on next cycle")
                        # Don't return here - let the loop continue but keep the active trade
        else:
            # NO ACTIVE TRADE - Check for buy signals
            # Only when there's no active trade, look for coins crossing above buy threshold
            # If multiple coins cross simultaneously, buy the one with highest RSI
            buy_candidates = []
            
            for symbol, data in coins_data.items():
                rsi = data.get('rsi')
                if rsi is not None:
                    # Check if RSI crosses above buy threshold (from below)
                    if self.check_buy_condition(symbol, rsi):
                        buy_candidates.append({
                            'symbol': symbol,
                            'rsi': rsi,
                            'data': data
                        })
            
            # If we have candidates, buy the one with highest RSI
            if buy_candidates:
                # Sort by RSI descending (highest first)
                buy_candidates.sort(key=lambda x: x['rsi'], reverse=True)
                best_candidate = buy_candidates[0]
                
                symbol = best_candidate['symbol']
                rsi = best_candidate['rsi']
                data = best_candidate['data']
                
                # If multiple coins cross above threshold simultaneously, buy the one with highest RSI
                price = data.get('price', 0)
                change_24h = data.get('change_24h', 0)
                change_str = f"+{change_24h:.2f}%" if change_24h else "N/A"
                
                if len(buy_candidates) > 1:
                    other_symbols = [c['symbol'] for c in buy_candidates[1:]]
                    print(f"\n🟢 [BUY SIGNAL] Multiple coins crossed above RSI {self.rsi_buy}!")
                    print(f"   Selected: {symbol} (RSI: {rsi:.2f}) - Highest RSI")
                    print(f"   Price: ${price:.4f} | 24h Change: {change_str}")
                    print(f"   Other candidates: {', '.join(other_symbols)}")
                    print(f"   Executing buy order...")
                    print()
                else:
                    print(f"\n🟢 [BUY SIGNAL] {symbol} RSI crossed above {self.rsi_buy}!")
                    print(f"   Current RSI: {rsi:.2f} | Price: ${price:.4f} | 24h Change: {change_str}")
                    print(f"   Executing buy order...")
                    print()
                
                # Place buy order
                order = self.buy_order(symbol, 0)  # Quantity will be calculated in buy_order
                
                if order:
                    buy_price = float(order.get('fills', [{}])[0].get('price', data['price']))
                    buy_quantity = float(order.get('executedQty', 0))
                    
                    self.active_trade = {
                        'symbol': symbol,
                        'buy_price': buy_price,
                        'quantity': buy_quantity,
                        'buy_rsi': rsi,
                        'buy_time': datetime.now().isoformat()
                    }
                    active_trade = self.active_trade
                    save_trade_data()
                    
                    # Emit active trade update to web interface immediately (broadcast to all clients)
                    socketio.emit('active_trade_update', {'active_trade': self.active_trade}, namespace='/')
                    # Also force immediate coins update to refresh display
                    socketio.emit('coins_update', {'coins': list(coins_data.values())}, namespace='/')
                    print(f"✅ Trade started: {symbol} | Buy Price: {buy_price:.4f} | Quantity: {buy_quantity:.4f} | RSI: {rsi:.2f}")
                    print(f"📊 Active trade created - No new trades will start until this trade closes")
                    
                    # Force immediate update after trade to ensure bot continues smoothly
                    time.sleep(0.5)  # Small delay to ensure trade is fully processed
                    return  # Exit to let main loop continue
                else:
                    # Trade failed (e.g., insufficient balance)
                    error_msg = f"❌ Failed to buy {symbol}: Insufficient balance or order error"
                    print(error_msg)
                    socketio.emit('trade_error', {
                        'symbol': symbol,
                        'rsi': rsi,
                        'error': 'Insufficient balance or order error',
                        'timestamp': datetime.now().isoformat()
                    }, namespace='/')
    
    def run(self):
        """Main bot loop"""
        global bot_running, coins_data, active_trade
        
        print("🤖 Bot starting...")
        print(f"📊 Trading mode: {'ENABLED' if trading_enabled else 'DISABLED (Monitoring only)'}")
        
        # Check if client is available
        if not self.client:
            print("❌ Cannot start bot: Binance API client is not available")
            if self.geo_blocked:
                print("   Reason: Geographic restriction - Binance API is blocked from this location")
                print("   Please use a server in a location where Binance API is accessible")
            print("   Bot will not start. Please fix the API connection issue.")
            return
        
        bot_running = True
        
        # Fetch trade history from Binance
        global trade_history
        binance_trades = self.fetch_trade_history_from_binance(limit=500)
        combined_trades = merge_trade_histories(binance_trades, trade_history)
        if not trade_history and binance_trades:
            trade_history.extend([t for t in combined_trades if _trade_key(t) not in {_trade_key(ct) for ct in trade_history}])
            save_trade_data()
            print(f"📜 Loaded {len(combined_trades)} trades from Binance history")
        # Emit combined view
        total_trades = len(combined_trades)
        total_profit = sum(float(trade.get('profit', 0) or 0) for trade in combined_trades)
        trades_to_emit = combined_trades[:100]
        socketio.emit('trade_history_update', {
            'trades': trades_to_emit,
            'total_trades': total_trades,
            'total_profit': round(total_profit, 2),
            'source': 'binance_api'
        }, namespace='/')
        
        # Check for existing positions from Binance account only if none persisted locally
        detected_trade = self.active_trade
        if not self.active_trade:
            detected_trade = self.detect_existing_positions()
            if detected_trade:
                self.active_trade = detected_trade
                active_trade = detected_trade
                print(f"🔄 Restored active trade from account: {detected_trade['symbol']}")
                # Emit to web interface
                socketio.emit('active_trade_update', {'active_trade': detected_trade}, namespace='/')
        else:
            detected_trade = self.active_trade
            print(f"🔄 Restored active trade from persisted data: {detected_trade['symbol']}")
            socketio.emit('active_trade_update', {'active_trade': detected_trade}, namespace='/')
        
        # Get top coins initially (top 25 gainers by 24-hour price change)
        symbols = self.get_top_coins(25)
        
        # If we have an active trade, make sure its symbol is in the monitoring list
        if detected_trade and detected_trade['symbol'] not in symbols:
            symbols.append(detected_trade['symbol'])
        
        if not symbols:
            # Fallback: use top 25 USDT pairs by volume if no gainers found
            try:
                # Get exchange info with caching
                current_time = time.time()
                if (self.exchange_info_cache is None or 
                    current_time - self.exchange_info_cache_timestamp > self.exchange_info_cache_ttl):
                    exchange_info = self.client.get_exchange_info()
                    self.exchange_info_cache = exchange_info
                    self.exchange_info_cache_timestamp = current_time
                else:
                    exchange_info = self.exchange_info_cache
                active_usdt_symbols = set()
                for symbol_info in exchange_info['symbols']:
                    if symbol_info['status'] == 'TRADING' and symbol_info['quoteAsset'] == 'USDT':
                        active_usdt_symbols.add(symbol_info['symbol'])
                
                # Get ticker with caching
                if (self.ticker_cache is None or 
                    current_time - self.ticker_cache_timestamp > self.ticker_cache_ttl):
                    ticker = self.client.get_ticker()
                    self.ticker_cache = ticker
                    self.ticker_cache_timestamp = current_time
                else:
                    ticker = self.ticker_cache
                # Filter to only active USDT pairs and sort by volume
                active_usdt_pairs = [t for t in ticker if t['symbol'] in active_usdt_symbols]
                sorted_by_volume = sorted(active_usdt_pairs, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
                symbols = [t['symbol'] for t in sorted_by_volume[:25]]
            except Exception as e:
                print(f"❌ Error in fallback: {e}")
                symbols = []
        
        # Start monitoring immediately with initial coins
        self.update_coins_data(symbols)
        
        # Emit initial data to web interface
        if coins_data:
            socketio.emit('coins_update', {'coins': list(coins_data.values())}, namespace='/')
        
        update_count = 0
        top_coins_refresh_interval = 30  # Refresh top coins list every 30 updates (30 sec if update_interval is 1 sec) - faster refresh to catch new gainers
        
        while bot_running:
            try:
                # Periodically refresh the top coins list to get live top 25
                if update_count % top_coins_refresh_interval == 0:
                    new_symbols = self.get_top_coins(25)
                    if new_symbols:
                        symbols = new_symbols
                        # If we have an active trade, make sure its symbol stays in monitoring
                        if self.active_trade and self.active_trade['symbol'] not in symbols:
                            symbols.append(self.active_trade['symbol'])
                        # Clear old data for coins no longer in top 25 (but keep active trade symbol)
                        coins_data = {k: v for k, v in coins_data.items() if k in symbols}
                
                # Update coins data (emits updates incrementally for faster response)
                # Wrap in try-except to ensure bot continues even if update fails
                try:
                    self.update_coins_data(symbols, emit_updates=True)
                except Exception as e:
                    print(f"⚠️  Error updating coins data: {e}")
                    # Continue anyway - don't let update errors stop the bot
                
                # Track coins below buy threshold for logging
                coins_below_threshold = []  # Track coins below buy threshold for logging
                
                # IMPORTANT: Check buy signals on ALL coins (not filtered) to catch when they cross above threshold
                # We need to check all coins because a coin might cross above 70 and we need to detect it immediately
                for symbol, data in coins_data.items():
                    rsi = data.get('rsi')
                    if rsi is not None:
                        # Track coins below threshold for logging
                        if rsi < self.rsi_buy:
                            if not self.active_trade or symbol != self.active_trade['symbol']:
                                coins_below_threshold.append({
                                    'symbol': symbol,
                                    'rsi': rsi,
                                    'price': data.get('price', 0),
                                    'change_24h': data.get('change_24h', 0)
                                })
                
                # Coins below threshold are tracked but not printed to reduce terminal clutter
                
                # Check trading signals on ALL coins (not filtered) to catch buy signals when they cross above threshold
                # This ensures we detect when any coin crosses above 70, even if it's not in a filtered list
                # Wrap in try-except to ensure bot continues even if trading signal check fails
                try:
                    self.check_trading_signals()
                except Exception as e:
                    print(f"⚠️  Error checking trading signals: {e}")
                    import traceback
                    traceback.print_exc()
                    # Continue anyway - don't let signal check errors stop the bot
                
                update_count += 1
                
                # Wait before next update
                time.sleep(self.update_interval)
                
            except KeyboardInterrupt:
                print("\n🛑 Bot stopped by user")
                bot_running = False
                break
            except Exception as e:
                print(f"Error in bot loop: {e}")
                time.sleep(self.update_interval)
        
        # Cleanup when bot stops
        print("🛑 Bot stopping, cleaning up resources...")
        if hasattr(self, 'executor'):
            try:
                self.executor.shutdown(wait=False)  # Don't wait, just shutdown gracefully
                print("✅ Thread pool executor shut down")
            except Exception as e:
                print(f"⚠️  Error shutting down executor: {e}")

# Initialize bot
bot = None
bot_thread = None

def start_bot():
    """Start the bot in a separate thread"""
    global bot, bot_thread
    if API_KEY and API_SECRET:
        rsi_cfg = load_rsi_config()
        bot = BinanceRSIBot(API_KEY, API_SECRET, TESTNET, rsi_config=rsi_cfg)
        bot_thread = threading.Thread(target=bot.run, daemon=True)
        bot_thread.start()
    else:
        print("⚠️  API credentials not set. Please set BINANCE_API_KEY and BINANCE_API_SECRET environment variables.")

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    global API_KEY, API_SECRET, TESTNET
    return jsonify({
        'api_key': API_KEY[:10] + '...' if len(API_KEY) > 10 else API_KEY,
        'api_key_set': bool(API_KEY),
        'api_secret_set': bool(API_SECRET),
        'testnet': TESTNET,
        'trading_enabled': trading_enabled
    })

@app.route('/api/config', methods=['POST'])
def update_config():
    """Update configuration"""
    global API_KEY, API_SECRET, TESTNET, bot, bot_running, bot_thread
    
    data = request.json
    api_key = data.get('api_key', '').strip()
    api_secret = data.get('api_secret', '').strip()
    testnet = data.get('testnet', False)
    
    if not api_key or not api_secret:
        return jsonify({'success': False, 'message': 'API key and secret are required'}), 400
    
    try:
        # Save to config file
        save_config(api_key, api_secret, testnet)
        
        # Update global variables
        API_KEY = api_key
        API_SECRET = api_secret
        TESTNET = testnet
        
        # Stop existing bot if running
        if bot:
            print("🛑 Stopping existing bot to apply new API credentials...")
            bot_running = False
            if bot_thread and bot_thread.is_alive():
                bot_thread.join(timeout=5)
                if bot_thread.is_alive():
                    print("⚠️  Previous bot thread is still running; forcing restart anyway.")
            bot = None
            bot_thread = None
            bot_running = True  # Reset for new bot instance
        
        # Reinitialize bot with new credentials
        print("🔄 Restarting bot with new API credentials...")
        rsi_cfg = load_rsi_config()
        bot = BinanceRSIBot(API_KEY, API_SECRET, TESTNET, rsi_config=rsi_cfg)
        bot_thread = threading.Thread(target=bot.run, daemon=True)
        bot_thread.start()
        
        return jsonify({'success': True, 'message': 'Configuration updated successfully - Bot restarted with new credentials'})
    except Exception as e:
        print(f"❌ Error updating config: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/trading/status', methods=['GET'])
def get_trading_status():
    """Get trading status"""
    global trading_enabled
    return jsonify({'trading_enabled': trading_enabled})

@app.route('/api/trades/history', methods=['GET'])
def get_trade_history():
    """Get trade history via HTTP - calculated from Binance API data"""
    global trade_history, bot
    limit = request.args.get('limit', 100, type=int)
    
    # If bot is available, fetch fresh data from Binance
    remote_trades = []
    if bot:
        try:
            remote_trades = bot.fetch_trade_history_from_binance(limit=max(limit * 2, 500))
        except Exception as e:
            print(f"⚠️  Error fetching fresh trades from Binance: {e}")
    
    combined_trades = merge_trade_histories(remote_trades, trade_history)
    total_trades = len(combined_trades)
    total_profit = sum(float(trade.get('profit', 0) or 0) for trade in combined_trades)
    
    trades_to_return = combined_trades[:limit]
    
    return jsonify({
        'trades': trades_to_return,
        'total_trades': total_trades,
        'total_profit': round(total_profit, 2),
        'source': 'binance_api'  # Indicates data is from Binance API
    })

@app.route('/api/rsi/settings', methods=['GET'])
def get_rsi_settings():
    """Get current RSI buy/sell settings"""
    global bot
    if bot:
        return jsonify({
            'buy_rsi': bot.rsi_buy,
            'sell_rsi': bot.rsi_sell,
            'take_profit_rsi': getattr(bot, 'take_profit_rsi', 100.0)
        })
    else:
        rsi_cfg = load_rsi_config()
        return jsonify(rsi_cfg)

@app.route('/api/rsi/settings', methods=['POST'])
def set_rsi_settings():
    """Update RSI buy/sell settings"""
    global bot, API_KEY, API_SECRET, TESTNET
    data = request.json
    
    buy_rsi = float(data.get('buy_rsi', 70.0))
    sell_rsi = float(data.get('sell_rsi', 69.0))
    take_profit_rsi = float(data.get('take_profit_rsi', 100.0))  # Default 100 = disabled
    
    # Validate values
    if buy_rsi < 0 or buy_rsi > 100:
        return jsonify({'success': False, 'message': 'Buy RSI must be between 0 and 100'}), 400
    if sell_rsi < 0 or sell_rsi > 100:
        return jsonify({'success': False, 'message': 'Sell RSI must be between 0 and 100'}), 400
    if take_profit_rsi < 0 or take_profit_rsi > 100:
        return jsonify({'success': False, 'message': 'Take Profit RSI must be between 0 and 100'}), 400
    
    # Validate take profit is above buy threshold if enabled
    if take_profit_rsi < 100 and take_profit_rsi <= buy_rsi:
        return jsonify({'success': False, 'message': 'Take Profit RSI must be greater than Buy RSI threshold'}), 400
    
    try:
        # Save to config file
        save_config(API_KEY, API_SECRET, TESTNET, buy_rsi, sell_rsi, take_profit_rsi)
        # Persist to trade data file so UI refresh retains values
        save_trade_data()
        
        # Update bot if it exists
        if bot:
            bot.update_rsi_settings(buy_rsi, sell_rsi, take_profit_rsi)
        
        return jsonify({
            'success': True,
            'message': 'RSI settings updated successfully',
            'settings': {
                'buy_rsi': buy_rsi,
                'sell_rsi': sell_rsi,
                'take_profit_rsi': take_profit_rsi
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/trades/stats', methods=['GET'])
def get_trade_stats():
    """Get trade statistics calculated from Binance API data"""
    global trade_history, bot
    
    # Fetch fresh data from Binance if bot is available
    if bot:
        try:
            binance_trades = bot.fetch_trade_history_from_binance(limit=1000)
            if binance_trades:
                trade_history = binance_trades
        except Exception as e:
            print(f"⚠️  Error fetching fresh trades from Binance: {e}")
    
    # Calculate statistics from Binance trade data
    total_trades = len(trade_history)
    total_profit = sum(float(trade.get('profit', 0)) for trade in trade_history)
    
    # Calculate winning vs losing trades
    winning_trades = [t for t in trade_history if float(t.get('profit', 0)) > 0]
    losing_trades = [t for t in trade_history if float(t.get('profit', 0)) < 0]
    
    win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0
    
    avg_profit = total_profit / total_trades if total_trades > 0 else 0
    
    return jsonify({
        'total_trades': total_trades,
        'total_profit': round(total_profit, 2),
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
        'win_rate': round(win_rate, 2),
        'average_profit': round(avg_profit, 2),
        'source': 'binance_api'
    })

@app.route('/api/trading/start', methods=['POST'])
def start_trading():
    """Start trading"""
    global trading_enabled, API_KEY, API_SECRET
    
    if not API_KEY or not API_SECRET:
        return jsonify({'success': False, 'message': 'Please set API credentials first'}), 400
    
    trading_enabled = True
    socketio.emit('trading_status_update', {'trading_enabled': True}, namespace='/')
    print("✅ Trading enabled - Bot will now execute buy/sell orders")
    return jsonify({'success': True, 'message': 'Trading started'})

@app.route('/api/trades/active', methods=['GET'])
def get_active_trade():
    """Get active trade from Binance account"""
    global bot, active_trade
    
    # Check if bot has an active trade
    if bot and bot.active_trade:
        return jsonify({'active_trade': bot.active_trade})
    elif active_trade:
        return jsonify({'active_trade': active_trade})
    else:
        # Try to detect from Binance
        if bot:
            try:
                detected_trade = bot.detect_existing_positions()
                if detected_trade:
                    bot.active_trade = detected_trade
                    active_trade = detected_trade
                    # Emit to web interface
                    socketio.emit('active_trade_update', {'active_trade': detected_trade}, namespace='/')
                    return jsonify({'active_trade': detected_trade})
            except Exception as e:
                print(f"Error detecting active trade: {e}")
        
        return jsonify({'active_trade': None})

@app.route('/api/trading/stop', methods=['POST'])
def stop_trading():
    """Stop trading - close all active positions by selling them immediately (no RSI check)"""
    global trading_enabled, active_trade, trade_history, coins_data, bot
    
    print("🛑 Stop trading requested by admin")
    print("   Closing all active positions immediately (regardless of RSI)...")
    
    sold_symbols = []
    sold_positions = []
    error_occurred = False
    error_message = None
    
    # First, try to close all positions from Binance account directly
    if bot and bot.client:
        try:
            sold_positions = bot.close_all_positions()
            if sold_positions:
                for pos in sold_positions:
                    sold_symbols.append(pos['symbol'])
                    
                    # Add to trade history if we have complete trade data
                    if pos.get('buy_price') and pos.get('profit') is not None:
                        trade_result = {
                            'symbol': pos['symbol'],
                            'buy_price': pos['buy_price'],
                            'sell_price': pos['sell_price'],
                            'quantity': pos['quantity'],
                            'buy_rsi': pos.get('buy_rsi'),
                            'sell_rsi': None,  # RSI not checked on manual stop
                            'buy_time': pos.get('buy_time'),
                            'sell_time': pos['sell_time'],
                            'profit': pos['profit'],
                            'profit_pct': pos['profit_pct']
                        }
                        trade_history.append(trade_result)
                        save_trade_data()
                        
                        # Emit trade update
                        socketio.emit('trade_update', trade_result)
                        print(f"✅ Added {pos['symbol']} to trade history")
        except Exception as e:
            error_occurred = True
            error_message = str(e)
            print(f"❌ Error closing positions from Binance: {e}")
            import traceback
            traceback.print_exc()
    
    # After attempting to close positions, check if there are still active positions
    remaining_active_trade = None
    if bot and bot.client:
        try:
            # Check again for any remaining active positions (without selling)
            remaining_positions = bot.check_active_positions()
            if remaining_positions:
                # There are still positions that couldn't be closed
                remaining_symbols = [pos['symbol'] for pos in remaining_positions]
                print(f"⚠️  Warning: Still have active positions after close attempt: {remaining_symbols}")
                # Try to detect the active trade
                remaining_active_trade = bot.detect_existing_positions()
                if remaining_active_trade:
                    bot.active_trade = remaining_active_trade
                    active_trade = remaining_active_trade
                else:
                    # Create active trade from the first remaining position
                    if remaining_positions:
                        pos = remaining_positions[0]
                        # Try to get buy price from trade history
                        buy_price = pos['current_price']  # Fallback to current price
                        buy_time = datetime.now().isoformat()
                        try:
                            trades = bot.client.get_my_trades(symbol=pos['symbol'], limit=50)
                            if trades:
                                buy_trades = [t for t in trades if t['isBuyer']]
                                if buy_trades:
                                    latest_buy = max(buy_trades, key=lambda x: x['time'])
                                    buy_price = float(latest_buy['price'])
                                    buy_time = datetime.fromtimestamp(latest_buy['time'] / 1000).isoformat()
                        except Exception:
                            pass
                        
                        remaining_active_trade = {
                            'symbol': pos['symbol'],
                            'buy_price': buy_price,
                            'quantity': pos['quantity'],
                            'buy_rsi': None,
                            'buy_time': buy_time
                        }
                        bot.active_trade = remaining_active_trade
                        active_trade = remaining_active_trade
        except Exception as e:
            print(f"⚠️  Error checking for remaining positions: {e}")
            # If we can't check, try to detect from memory
            if bot.active_trade:
                remaining_active_trade = bot.active_trade
    
    # Only clear active_trade if we successfully sold all positions
    if sold_symbols and not remaining_active_trade:
        if bot and bot.active_trade:
            symbol = bot.active_trade['symbol']
            if symbol in sold_symbols:
                bot.active_trade = None
        if active_trade:
            active_trade = None
        # Emit active trade update to clear it in UI
        socketio.emit('active_trade_update', {'active_trade': None}, namespace='/')
        save_trade_data()
    elif remaining_active_trade:
        # Still have active trade, emit it to UI
        socketio.emit('active_trade_update', {'active_trade': remaining_active_trade}, namespace='/')
        print(f"⚠️  Active trade still exists: {remaining_active_trade.get('symbol')}")
        save_trade_data()
    
    # Update trade history totals and emit
    if sold_positions:
        total_trades = len(trade_history)
        total_profit = sum(float(t.get('profit', 0)) for t in trade_history)
        socketio.emit('trade_history_update', {
            'trades': trade_history[-100:] if len(trade_history) > 100 else trade_history,
            'total_trades': total_trades,
            'total_profit': round(total_profit, 2),
            'source': 'manual_close'
        }, namespace='/')
    
    # Disable trading
    trading_enabled = False
    socketio.emit('trading_status_update', {'trading_enabled': False}, namespace='/')
    
    # Prepare response
    if error_occurred and not sold_symbols:
        # Error occurred and no positions were sold
        print(f"❌ Trading stop failed - Could not close positions: {error_message}")
        return jsonify({
            'success': False, 
            'message': f'Failed to close positions: {error_message}',
            'trade_sold': False,
            'positions_still_active': remaining_active_trade is not None,
            'symbols': [],
            'error': error_message
        }), 500
    elif sold_symbols and not remaining_active_trade:
        # Successfully closed all positions
        symbols_str = ', '.join(sold_symbols)
        print(f"⛔ Trading disabled - Closed {len(sold_symbols)} position(s): {symbols_str}")
        return jsonify({
            'success': True, 
            'message': f'Trading stopped - Closed {len(sold_symbols)} position(s): {symbols_str}',
            'trade_sold': True,
            'positions_still_active': False,
            'symbols': sold_symbols,
            'positions_closed': len(sold_symbols)
        })
    elif remaining_active_trade:
        # Some positions were closed but others remain
        symbols_str = ', '.join(sold_symbols) if sold_symbols else 'None'
        remaining_symbol = remaining_active_trade.get('symbol', 'Unknown')
        print(f"⚠️  Trading disabled - Closed {len(sold_symbols)} position(s), but {remaining_symbol} still active")
        return jsonify({
            'success': False,
            'message': f'Closed {len(sold_symbols)} position(s), but {remaining_symbol} is still active. Please try again.',
            'trade_sold': len(sold_symbols) > 0,
            'positions_still_active': True,
            'remaining_symbol': remaining_symbol,
            'symbols': sold_symbols,
            'positions_closed': len(sold_symbols)
        }), 500
    else:
        # No positions to close
        print("⛔ Trading disabled - No active positions found to close")
        return jsonify({
            'success': True, 
            'message': 'Trading stopped - No active positions to close',
            'trade_sold': False,
            'positions_still_active': False,
            'symbols': []
        })

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    global trade_history, bot, coins_data, active_trade, trading_enabled
    try:
        print('Client connected')
        # Send initial data
        emit('coins_update', {'coins': list(coins_data.values())})
        emit('active_trade_update', {'active_trade': active_trade})
        # Calculate totals from Binance trade data
        total_trades = len(trade_history)
        total_profit = sum(float(trade.get('profit', 0)) for trade in trade_history)
        # Send last 100 trades (or all if less than 100) with totals from Binance
        trades_to_send = trade_history[-100:] if len(trade_history) > 100 else trade_history
        emit('trade_history_update', {
            'trades': trades_to_send,
            'total_trades': total_trades,
            'total_profit': round(total_profit, 2),
            'source': 'binance_api'
        })
        emit('trading_status_update', {'trading_enabled': trading_enabled})
    except Exception as e:
        print(f'Error in handle_connect: {e}')
        import traceback
        traceback.print_exc()

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print('Client disconnected')

@socketio.on_error_default
def default_error_handler(e):
    """Handle SocketIO errors"""
    print(f'SocketIO error: {e}')
    import traceback
    traceback.print_exc()

if __name__ == '__main__':
    print("🚀 Starting Binance RSI Bot...")
    print("📡 Web interface available at http://localhost:5000")
    start_bot()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)


