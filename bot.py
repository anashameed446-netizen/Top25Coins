import os
import time
import threading
from datetime import datetime
from typing import Dict, Optional, List
from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
import json
from dotenv import load_dotenv
import configparser

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

def save_config(api_key, api_secret, testnet=False, rsi_buy=None, rsi_sell=None):
    """Save configuration to config.ini"""
    try:
        config = configparser.ConfigParser()
        config['BINANCE'] = {
            'api_key': api_key,
            'api_secret': api_secret,
            'testnet': str(testnet)
        }
        # Add RSI settings if provided
        if rsi_buy is not None:
            config['RSI'] = {
                'buy_rsi': str(rsi_buy),
                'sell_rsi': str(rsi_sell)
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
                'sell_rsi': float(config['RSI'].get('sell_rsi', 69.0))
            }
    return {
        'buy_rsi': 70.0,
        'sell_rsi': 69.0
    }

API_KEY, API_SECRET, TESTNET = load_config()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*")

# Global state
coins_data = {}
active_trade = None
trade_history = []
bot_running = False
trading_enabled = False  # Trading control flag

class BinanceRSIBot:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, rsi_config: dict = None):
        """Initialize Binance client"""
        if testnet:
            self.client = Client(api_key, api_secret, testnet=True)
        else:
            self.client = Client(api_key, api_secret)
        
        self.rsi_period = 14
        # RSI thresholds - single values only
        if rsi_config:
            self.rsi_buy = float(rsi_config.get('buy_rsi', 70.0))
            self.rsi_sell = float(rsi_config.get('sell_rsi', 69.0))
        else:
            rsi_cfg = load_rsi_config()
            self.rsi_buy = rsi_cfg['buy_rsi']
            self.rsi_sell = rsi_cfg['sell_rsi']
        self.active_trade = None
        self.update_interval = 1  # seconds - near real-time updates (1 second refresh)
        # Track previous RSI values to detect crossing above buy threshold
        self.previous_rsi = {}  # {symbol: previous_rsi_value}
        
        # Test API permissions
        self.check_api_permissions()
    
    def update_rsi_settings(self, buy_rsi, sell_rsi):
        """Update RSI buy/sell thresholds"""
        self.rsi_buy = float(buy_rsi)
        self.rsi_sell = float(sell_rsi)
        print(f"📊 RSI settings updated: Buy when RSI crosses above {self.rsi_buy}, Sell when RSI drops to {self.rsi_sell}")
    
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
    
    def check_sell_condition(self, rsi: float) -> bool:
        """Check if RSI drops to sell threshold"""
        return rsi <= self.rsi_sell
    
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
        try:
            print("📡 Fetching active USDT trading pairs from Binance...")
            
            # First, get exchange info to filter only active (TRADING) USDT pairs
            exchange_info = self.client.get_exchange_info()
            active_usdt_symbols = set()
            for symbol_info in exchange_info['symbols']:
                # Only include pairs with TRADING status and USDT as quote currency
                if symbol_info['status'] == 'TRADING' and symbol_info['quoteAsset'] == 'USDT':
                    active_usdt_symbols.add(symbol_info['symbol'])
            
            print(f"📊 Found {len(active_usdt_symbols)} active USDT trading pairs")
            
            # Get ticker data for all pairs
            ticker = self.client.get_ticker()
            
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
            
            # Return top coins with detailed logging
            result = [coin['symbol'] for coin in sorted_coins[:limit]]
            if result:
                print(f"✅ Top {len(result)} USDT-paired gainers by 24h change (Binance market):")
                for idx, coin_data in enumerate(sorted_coins[:limit], 1):
                    print(f"   {idx}. {coin_data['symbol']}: +{coin_data['change_percent']:.2f}%")
            else:
                print("⚠️  No USDT gainers found among active trading pairs")
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
            
            # Step 3: Calculate average gain and average loss over the period
            avg_gain = gain.rolling(window=period).mean()
            avg_loss = loss.rolling(window=period).mean()
            
            # Step 4: Calculate RS (Relative Strength)
            # Avoid division by zero - if avg_loss is 0, all prices went up, RSI = 100
            avg_loss_safe = avg_loss.replace(0, 0.0001)  # Replace 0 with small value
            rs = avg_gain / avg_loss_safe
            
            # Step 5: Calculate RSI
            rsi = 100 - (100 / (1 + rs))
            
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
    
    def get_klines(self, symbol: str, interval: str = '1h', limit: int = 100) -> List[float]:
        """Get klines (candlestick data) for RSI calculation"""
        try:
            klines = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            prices = [float(k[4]) for k in klines]  # Close prices
            return prices
        except Exception as e:
            print(f"Error getting klines for {symbol}: {e}")
            return []
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price of a symbol"""
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker['price'])
        except Exception as e:
            print(f"Error getting price for {symbol}: {e}")
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
                    
                symbol = f"{asset}USDT"
                # Check if this is a valid trading pair
                try:
                    # Try to get price to verify it's a valid pair
                    price = self.get_current_price(symbol)
                    if price:
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
                if asset != 'USDT' and balance > 0:
                    symbol = f"{asset}USDT"
                    if symbol in usdt_symbols:
                        symbols_to_check.add(symbol)
            
            # Limit symbols_to_check to avoid too many API calls (max 30 symbols)
            if len(symbols_to_check) < 30:
                # Also check a few common trading pairs in case we traded them
                common_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'SOLUSDT', 
                                'XRPUSDT', 'DOGEUSDT', 'DOTUSDT', 'MATICUSDT', 'LINKUSDT']
                for symbol in common_symbols:
                    if symbol in usdt_symbols and symbol not in symbols_to_check:
                        symbols_to_check.add(symbol)
                        if len(symbols_to_check) >= 30:
                            break
            
            # If no symbols found, check a few common ones
            if not symbols_to_check:
                common_symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'ADAUSDT', 'SOLUSDT']
                symbols_to_check = {s for s in common_symbols if s in usdt_symbols}
            
            print(f"   Checking {len(symbols_to_check)} symbols for trade history...")
            
            # Fetch trades for each symbol
            for symbol in symbols_to_check:
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
            
            # Calculate quantity based on available USDT (use 99.5% to account for trading fees ~0.1% and price fluctuations)
            available_usdt = usdt_balance * 0.995
            current_price = self.get_current_price(symbol)
            
            if current_price is None:
                print(f"❌ Could not get current price for {symbol}")
                return None
            
            # Calculate quantity (with precision)
            quantity = available_usdt / current_price
            
            print(f"💵 Using {available_usdt:.2f} USDT (99.5% of balance) to buy {symbol}")
            
            # Get symbol info for precision
            exchange_info = self.client.get_symbol_info(symbol)
            if not exchange_info:
                return None
            
            # Get quantity precision
            quantity_precision = None
            for filter_item in exchange_info['filters']:
                if filter_item['filterType'] == 'LOT_SIZE':
                    step_size = float(filter_item['stepSize'])
                    quantity_precision = len(str(step_size).split('.')[-1].rstrip('0'))
                    break
            
            if quantity_precision:
                quantity = round(quantity, quantity_precision)
            
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
                print(f"   💡 The bot uses 99.5% of your balance to account for fees.")
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
    
    def sell_order(self, symbol: str, quantity: float) -> Optional[Dict]:
        """Place a sell order"""
        try:
            # Get account balance for the coin
            account = self.client.get_account()
            balances = {b['asset']: float(b['free']) for b in account['balances']}
            
            coin_asset = symbol.replace('USDT', '')
            if coin_asset not in balances or balances[coin_asset] < 0.0001:
                print(f"Insufficient {coin_asset} balance")
                return None
            
            # Use available balance
            quantity = balances[coin_asset]
            
            # Get symbol info for precision
            exchange_info = self.client.get_symbol_info(symbol)
            if not exchange_info:
                return None
            
            # Get quantity precision
            quantity_precision = None
            for filter_item in exchange_info['filters']:
                if filter_item['filterType'] == 'LOT_SIZE':
                    step_size = float(filter_item['stepSize'])
                    quantity_precision = len(str(step_size).split('.')[-1].rstrip('0'))
                    break
            
            if quantity_precision:
                quantity = round(quantity, quantity_precision)
            
            if quantity <= 0:
                return None
            
            # Place market sell order
            order = self.client.create_order(
                symbol=symbol,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_MARKET,
                quantity=quantity
            )
            
            current_price = self.get_current_price(symbol)
            print(f"✅ SELL ORDER EXECUTED: {symbol} | Quantity: {quantity} | Price: {current_price}")
            return order
            
        except BinanceAPIException as e:
            print(f"Binance API Error during sell: {e}")
            return None
        except Exception as e:
            print(f"Error placing sell order: {e}")
            return None
    
    def update_coins_data(self, symbols: List[str], emit_updates: bool = True):
        """Update RSI data for all coins, including 24h change percentage"""
        global coins_data
        
        # Get ticker data once for all symbols to get 24h change
        try:
            ticker_data_map = {}
            ticker = self.client.get_ticker()
            for ticker_info in ticker:
                ticker_data_map[ticker_info['symbol']] = ticker_info
        except Exception as e:
            ticker_data_map = {}
        
        updated_count = 0
        for symbol in symbols:
            try:
                prices = self.get_klines(symbol, interval='1h', limit=100)
                if len(prices) >= self.rsi_period + 1:
                    rsi = self.calculate_rsi(prices, self.rsi_period)
                    current_price = self.get_current_price(symbol)
                    
                    # Get 24h change from ticker data
                    change_24h = None
                    if symbol in ticker_data_map:
                        change_24h = float(ticker_data_map[symbol].get('priceChangePercent', 0))
                    
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
                        
                        # Send incremental updates to web interface for faster response
                        if emit_updates and updated_count % 5 == 0:
                            socketio.emit('coins_update', {'coins': list(coins_data.values())})
            except Exception as e:
                # Silently skip errors to avoid spam, but log important ones
                if 'rate limit' in str(e).lower():
                    print(f"⚠️ Rate limit hit, slowing down...")
                pass
        
        # Send final update with all coins
        if emit_updates and updated_count > 0:
            socketio.emit('coins_update', {'coins': list(coins_data.values())})
            print(f"📊 Updated {updated_count}/{len(symbols)} coins data")
    
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
                    if self.check_sell_condition(rsi):
                        print(f"🔍 [MONITORING] SELL SIGNAL: {symbol} RSI {rsi:.2f} (dropped to {self.rsi_sell}) - Trading disabled")
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
            
            if symbol in coins_data and coins_data[symbol].get('rsi') is not None:
                rsi = coins_data[symbol]['rsi']
                
                # Check if RSI meets sell condition
                if self.check_sell_condition(rsi):
                    print(f"🔄 RSI dropped to {self.rsi_sell} ({rsi:.2f}) for {symbol}, selling...")
                    
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
                        self.active_trade = None
                        active_trade = None
                        
                        # Emit trade update
                        socketio.emit('trade_update', trade_result)
                        socketio.emit('active_trade_update', {'active_trade': None})
                        print(f"✅ Trade closed: {symbol} | Profit: {trade_result['profit']:.2f} USDT ({trade_result['profit_pct']:.2f}%)")
                        print(f"📊 Active trade cleared - Bot will now check for new buy signals")
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
                    
                    # Emit active trade update to web interface
                    socketio.emit('active_trade_update', {'active_trade': self.active_trade})
                    print(f"✅ Trade started: {symbol} | Buy Price: {buy_price:.4f} | Quantity: {buy_quantity:.4f} | RSI: {rsi:.2f}")
                    print(f"📊 Active trade created - No new trades will start until this trade closes")
                else:
                    # Trade failed (e.g., insufficient balance)
                    error_msg = f"❌ Failed to buy {symbol}: Insufficient balance or order error"
                    print(error_msg)
                    socketio.emit('trade_error', {
                        'symbol': symbol,
                        'rsi': rsi,
                        'error': 'Insufficient balance or order error',
                        'timestamp': datetime.now().isoformat()
                    })
    
    def run(self):
        """Main bot loop"""
        global bot_running, coins_data, active_trade
        
        print("🤖 Bot starting...")
        print(f"📊 Trading mode: {'ENABLED' if trading_enabled else 'DISABLED (Monitoring only)'}")
        bot_running = True
        
        # Fetch trade history from Binance
        global trade_history
        binance_trades = self.fetch_trade_history_from_binance(limit=500)
        if binance_trades:
            trade_history = binance_trades
            print(f"📜 Loaded {len(trade_history)} trades from Binance history")
            # Calculate totals from Binance data
            total_trades = len(trade_history)
            total_profit = sum(float(trade.get('profit', 0)) for trade in trade_history)
            # Emit to web interface (send last 100 trades with totals from Binance)
            trades_to_emit = trade_history[-100:] if len(trade_history) > 100 else trade_history
            socketio.emit('trade_history_update', {
                'trades': trades_to_emit,
                'total_trades': total_trades,
                'total_profit': round(total_profit, 2),
                'source': 'binance_api'
            })
        
        # Check for existing positions from Binance account
        detected_trade = self.detect_existing_positions()
        if detected_trade:
            self.active_trade = detected_trade
            active_trade = detected_trade
            print(f"🔄 Restored active trade from account: {detected_trade['symbol']}")
            # Emit to web interface
            socketio.emit('active_trade_update', {'active_trade': detected_trade})
        
        # Get top coins initially (top 25 gainers by 24-hour price change)
        print("🔄 Finding top 25 coins with positive 24h price change (Binance market)...")
        symbols = self.get_top_coins(25)
        
        # If we have an active trade, make sure its symbol is in the monitoring list
        if detected_trade and detected_trade['symbol'] not in symbols:
            symbols.append(detected_trade['symbol'])
            print(f"➕ Added active trade symbol {detected_trade['symbol']} to monitoring list")
        
        if not symbols:
            print("⚠️  No USDT gainers found, using top 25 USDT pairs by volume as fallback...")
            # Fallback: use top 25 USDT pairs by volume if no gainers found
            try:
                exchange_info = self.client.get_exchange_info()
                active_usdt_symbols = set()
                for symbol_info in exchange_info['symbols']:
                    if symbol_info['status'] == 'TRADING' and symbol_info['quoteAsset'] == 'USDT':
                        active_usdt_symbols.add(symbol_info['symbol'])
                
                ticker = self.client.get_ticker()
                # Filter to only active USDT pairs and sort by volume
                active_usdt_pairs = [t for t in ticker if t['symbol'] in active_usdt_symbols]
                sorted_by_volume = sorted(active_usdt_pairs, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
                symbols = [t['symbol'] for t in sorted_by_volume[:25]]
                print(f"✅ Using top {len(symbols)} USDT pairs by volume as fallback")
            except Exception as e:
                print(f"❌ Error in fallback: {e}")
                symbols = []
        
        print(f"📊 Monitoring {len(symbols)} coins: {', '.join(symbols[:5])}...")
        
        # Start monitoring immediately with initial coins
        print("🔄 Calculating RSI for selected coins...")
        self.update_coins_data(symbols)
        
        # Emit initial data to web interface
        if coins_data:
            socketio.emit('coins_update', {'coins': list(coins_data.values())})
            print(f"✅ Sending {len(coins_data)} coins data to web interface")
        else:
            print("⚠️  No coin data available yet")
        
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
                        print(f"🔄 Refreshed top 25 USDT gainers (24h change): {', '.join(symbols[:5])}...")
                        # Clear old data for coins no longer in top 25 (but keep active trade symbol)
                        coins_data = {k: v for k, v in coins_data.items() if k in symbols}
                
                # Update coins data (emits updates incrementally for faster response)
                self.update_coins_data(symbols, emit_updates=True)
                
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
                self.check_trading_signals()
                
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

# Initialize bot
bot = None

def start_bot():
    """Start the bot in a separate thread"""
    global bot
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
    global API_KEY, API_SECRET, TESTNET, bot
    
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
        
        # Reinitialize bot with new credentials
        if bot:
            bot.client = Client(API_KEY, API_SECRET, testnet=TESTNET)
        
        return jsonify({'success': True, 'message': 'Configuration updated successfully'})
    except Exception as e:
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
    if bot:
        try:
            # Fetch fresh trade history from Binance
            binance_trades = bot.fetch_trade_history_from_binance(limit=1000)
            if binance_trades:
                trade_history = binance_trades
        except Exception as e:
            print(f"⚠️  Error fetching fresh trades from Binance: {e}")
            # Fall back to cached trade_history
    
    # Calculate totals from trade_history (which comes from Binance)
    total_trades = len(trade_history)
    total_profit = sum(float(trade.get('profit', 0)) for trade in trade_history)
    
    trades_to_return = trade_history[-limit:] if len(trade_history) > limit else trade_history
    
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
            'sell_rsi': bot.rsi_sell
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
    
    # Validate values
    if buy_rsi < 0 or buy_rsi > 100:
        return jsonify({'success': False, 'message': 'Buy RSI must be between 0 and 100'}), 400
    if sell_rsi < 0 or sell_rsi > 100:
        return jsonify({'success': False, 'message': 'Sell RSI must be between 0 and 100'}), 400
    
    try:
        # Save to config file
        save_config(API_KEY, API_SECRET, TESTNET, buy_rsi, sell_rsi)
        
        # Update bot if it exists
        if bot:
            bot.update_rsi_settings(buy_rsi, sell_rsi)
        
        return jsonify({
            'success': True,
            'message': 'RSI settings updated successfully',
            'settings': {
                'buy_rsi': buy_rsi,
                'sell_rsi': sell_rsi
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
    socketio.emit('trading_status_update', {'trading_enabled': True})
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
                    socketio.emit('active_trade_update', {'active_trade': detected_trade})
                    return jsonify({'active_trade': detected_trade})
            except Exception as e:
                print(f"Error detecting active trade: {e}")
        
        return jsonify({'active_trade': None})

@app.route('/api/trading/stop', methods=['POST'])
def stop_trading():
    """Stop trading - sell any active trade first"""
    global trading_enabled, active_trade, trade_history, coins_data, bot
    
    # Check if there's an active trade
    current_active_trade = None
    if bot and bot.active_trade:
        current_active_trade = bot.active_trade
    elif active_trade:
        current_active_trade = active_trade
    
    # Store symbol before selling (for response message)
    sold_symbol = None
    
    # If there's an active trade, sell it first
    if current_active_trade:
        sold_symbol = current_active_trade['symbol']
        symbol = current_active_trade['symbol']
        quantity = current_active_trade['quantity']
        
        print(f"🛑 Stop trading requested - Active trade found: {symbol}")
        print(f"   Selling {symbol} before disabling trading...")
        
        try:
            # Place sell order
            order = bot.sell_order(symbol, quantity) if bot else None
            
            if order:
                # Get sell price from order or current price
                sell_price = None
                if order.get('fills'):
                    sell_price = float(order['fills'][0].get('price', 0))
                else:
                    # Get current price from coins_data or API
                    if symbol in coins_data:
                        sell_price = coins_data[symbol]['price']
                    else:
                        if bot:
                            sell_price = bot.get_current_price(symbol)
                
                if sell_price:
                    sell_quantity = float(order.get('executedQty', quantity))
                    
                    trade_result = {
                        'symbol': symbol,
                        'buy_price': current_active_trade['buy_price'],
                        'sell_price': sell_price,
                        'quantity': sell_quantity,
                        'buy_rsi': current_active_trade.get('buy_rsi', None),
                        'sell_rsi': None,  # RSI not checked on manual stop
                        'buy_time': current_active_trade['buy_time'],
                        'sell_time': datetime.now().isoformat(),
                        'profit': (sell_price - current_active_trade['buy_price']) * sell_quantity,
                        'profit_pct': ((sell_price - current_active_trade['buy_price']) / current_active_trade['buy_price']) * 100
                    }
                    
                    trade_history.append(trade_result)
                    
                    # Clear active trade
                    if bot:
                        bot.active_trade = None
                    active_trade = None
                    
                    # Emit trade update
                    socketio.emit('trade_update', trade_result)
                    socketio.emit('active_trade_update', {'active_trade': None})
                    # Update totals from Binance data and emit
                    total_trades = len(trade_history)
                    total_profit = sum(float(t.get('profit', 0)) for t in trade_history)
                    socketio.emit('trade_history_update', {
                        'trades': trade_history[-100:] if len(trade_history) > 100 else trade_history,
                        'total_trades': total_trades,
                        'total_profit': round(total_profit, 2),
                        'source': 'binance_api'
                    })
                    
                    print(f"✅ Sold {symbol} at {sell_price:.4f} | Profit: {trade_result['profit']:.2f} USDT ({trade_result['profit_pct']:.2f}%)")
                else:
                    print(f"⚠️  Sold {symbol} but couldn't determine sell price")
            else:
                print(f"❌ Failed to sell {symbol} - Order may have failed")
                return jsonify({'success': False, 'message': f'Failed to sell active trade: {symbol}'}), 500
                
        except Exception as e:
            print(f"❌ Error selling active trade: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'success': False, 'message': f'Error selling active trade: {str(e)}'}), 500
    
    # Disable trading
    trading_enabled = False
    socketio.emit('trading_status_update', {'trading_enabled': False})
    
    if sold_symbol:
        print("⛔ Trading disabled - Active trade was sold")
        return jsonify({
            'success': True, 
            'message': f'Trading stopped - Sold {sold_symbol}',
            'trade_sold': True,
            'symbol': sold_symbol
        })
    else:
        print("⛔ Trading disabled - Bot will only monitor signals")
        return jsonify({
            'success': True, 
            'message': 'Trading stopped',
            'trade_sold': False
        })

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    global trade_history, bot
    print('Client connected')
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

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print('Client disconnected')

if __name__ == '__main__':
    print("🚀 Starting Binance RSI Bot...")
    print("📡 Web interface available at http://localhost:5000")
    start_bot()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

