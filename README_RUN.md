# How to Run the Bot

## ✅ Use `bot.py` (NOT bot3.py)

**Main file:** `bot.py` - This is the correct file to run.

## 🚀 Running the Bot

```bash
python bot.py
```

**NOT:**

```bash
python bot3.py  # ❌ Don't use this
```

## 📋 What's Fixed in bot.py:

1. ✅ Namespace fix for socket connections
2. ✅ Trade data loading from trade_data.json
3. ✅ Better logging for debugging
4. ✅ Active trade detection and restoration
5. ✅ Coins data emission on startup

## 🔍 If Data Not Showing:

1. **Check terminal output:**

   - Should see: "🤖 Bot starting..."
   - Should see: "📊 Starting to monitor X coins..."
   - Should see: "✅ Emitting initial coins data: X coins"

2. **Check browser console (F12):**

   - Should see: "✅ Connected to server"
   - Should see: "📊 Coins update received: X coins"

3. **Verify API keys:**

   - Check `config.ini` has valid API keys
   - Bot should print: "API Key: [first 10 chars]..."

4. **If still not working:**
   - Stop bot (Ctrl+C)
   - Restart: `python bot.py`
   - Check terminal for errors

## 📝 Files:

- **bot.py** - ✅ Main file (USE THIS)
- **bot2.py** - Old version
- **bot3.py** - Old version (has issues)

<!-- .\venv\Scripts\Activate -->
<!-- Set-ExecutionPolicy RemoteSigned -->
<!-- pip install -r requirements.txt -->
<!-- python bot.py -->
