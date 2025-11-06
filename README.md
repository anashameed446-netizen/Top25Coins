# Binance RSI Trading Bot

An automated trading bot that monitors the top 25 cryptocurrencies on Binance and executes trades based on RSI (Relative Strength Index) indicators.

## Features

- **Top 25 Coins Monitoring**: Automatically tracks the top 25 coins by 24h volume
- **RSI-Based Trading**: 
  - Buys when RSI hits 70
  - Sells when RSI drops to 69
- **Single Trade Management**: Only one active trade at a time
- **Live Web Interface**: Real-time dashboard showing:
  - Live RSI values for all monitored coins
  - Current prices
  - Active trade status
  - Trade history with profit/loss

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API Credentials

**Recommended: Using .env file (Easiest Method)**

1. Copy the example environment file:
   ```bash
   # Windows
   copy .env.example .env
   
   # Linux/Mac
   cp .env.example .env
   ```

2. Edit the `.env` file and add your Binance API credentials:
   ```
   BINANCE_API_KEY=your_api_key_here
   BINANCE_API_SECRET=your_api_secret_here
   BINANCE_TESTNET=False
   ```

3. Get your API keys from: https://www.binance.com/en/my/settings/api-management

**Alternative: Environment Variables**

You can also set environment variables directly:

**Windows (PowerShell):**
```powershell
$env:BINANCE_API_KEY="your_api_key_here"
$env:BINANCE_API_SECRET="your_api_secret_here"
```

**Windows (Command Prompt):**
```cmd
set BINANCE_API_KEY=your_api_key_here
set BINANCE_API_SECRET=your_api_secret_here
```

**Linux/Mac:**
```bash
export BINANCE_API_KEY="your_api_key_here"
export BINANCE_API_SECRET="your_api_secret_here"
```

### 3. Testnet Mode (Optional)

To use Binance Testnet for testing without real money, set in `.env` file:
```
BINANCE_TESTNET=True
```

Or via environment variables:

**Windows (PowerShell):**
```powershell
$env:BINANCE_TESTNET="True"
```

**Windows (Command Prompt):**
```cmd
set BINANCE_TESTNET=True
```

**Linux/Mac:**
```bash
export BINANCE_TESTNET="True"
```

### 4. Run the Bot

**Windows:**
```bash
# Option 1: Use the batch file
run.bat

# Option 2: Run directly
python bot.py
```

**Linux/Mac:**
```bash
python bot.py
```

The bot will start and the web interface will be available at:
- **http://localhost:5000**

## How It Works

1. **Coin Selection**: The bot fetches the top 25 coins by 24h trading volume (USDT pairs only)

2. **RSI Calculation**: 
   - Calculates RSI using a 14-period indicator
   - Updates every 5 seconds
   - Uses 1-minute candlestick data

3. **Trading Logic**:
   - Monitors all 25 coins continuously
   - When RSI ≥ 70: Places a market buy order
   - When RSI ≤ 69 (during active trade): Places a market sell order
   - Only one trade at a time - if another coin hits 70 while a trade is active, it's ignored

4. **Web Interface**:
   - Real-time updates via WebSocket
   - Shows all monitored coins with their current RSI
   - Displays active trade details
   - Shows trade history with profit/loss calculations

## Important Notes

⚠️ **WARNING**: This bot uses real money when not in testnet mode. Use at your own risk!

- Always test with small amounts first
- Monitor the bot regularly
- RSI-based strategies can be risky in volatile markets
- Past performance doesn't guarantee future results
- Ensure you have sufficient USDT balance in your Binance account

## Trading Parameters

- **RSI Period**: 14 (default)
- **Buy Threshold**: RSI ≥ 70
- **Sell Threshold**: RSI ≤ 69
- **Update Interval**: 5 seconds
- **Candlestick Interval**: 1 minute

## Troubleshooting

### Bot not connecting to Binance
- Check your API credentials are correct
- Ensure your API key has trading permissions enabled
- Check if IP restrictions are set on your API key

### No trades executing
- Verify you have sufficient USDT balance
- Check if you're in testnet mode (testnet has different balances)
- Ensure the coins have sufficient liquidity

### Web interface not loading
- Make sure port 5000 is not in use
- Check firewall settings
- Try accessing http://127.0.0.1:5000 instead

## License

This project is for educational purposes. Use at your own risk.

