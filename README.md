# Veteran Day Trading Playbook Pro

This local app is built to follow your playbook in order:
1. Screen for catalyst, RVOL/liquidity, spread, float proxy, and ATR
2. Validate with daily alignment, relative strength, and intraday tape proxies
3. Execute with a defined entry, stop, and 2 profit targets

## What it does
- Morning scan for top candidates
- Gemini-based catalyst scoring when a Gemini key is present
- Lightweight Charts for 1-minute and daily charts
- Live watchlist updates through a browser websocket
- Alpaca paper-trade bracket order button
- SQLite scan history and trade journal
- Exact plain-English panel for:
  - Day of the Week: What Stock to Watch
  - Buy only after 10:00 AM ET if it is between $X and $Y
  - Buy 5 shares max
  - Stop
  - Take profit range

## Important truth
This app is a disciplined ranking and execution assistant. It does not guarantee profit.

## Windows setup
1. Install Python 3.11
2. Unzip this folder somewhere simple, such as `C:\veteran-best-app`
3. Open PowerShell in the folder
4. Run:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python app.py
```

Then open `http://127.0.0.1:5000`

## Keys
Required:
- Alpaca paper/data key and secret

Recommended:
- Finnhub key for company news and profile data
- Gemini key for true catalyst scoring

## Notes
- The float rule uses Finnhub shares outstanding as a proxy when a true public float source is unavailable.
- The live watchlist uses your local app websocket and periodic quote refreshes for stability.
- Bracket orders are sent only to Alpaca paper trading.


## Upgrade notes
- The scanner now classifies each day as A+, A, WATCH, or NO TRADE.
- Paper execution is blocked unless the best setup is graded A or A+.
- Premarket gap, premarket dollar volume, and sector sympathy now materially affect ranking.
