@echo off
echo ========================================
echo Binance RSI Bot Setup
echo ========================================
echo.

echo Installing Python dependencies...
pip install -r requirements.txt

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo Next steps:
echo 1. Set your Binance API credentials:
echo    set BINANCE_API_KEY=your_api_key_here
echo    set BINANCE_API_SECRET=your_api_secret_here
echo.
echo 2. Optional: Enable testnet mode:
echo    set BINANCE_TESTNET=True
echo.
echo 3. Run the bot:
echo    python bot.py
echo.
echo 4. Open your browser to:
echo    http://localhost:5000
echo.
pause

