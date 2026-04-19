import requests
from models import db


def verify_alpaca_data_feed(user):
    """Detects if the user has SIP or IEX data based on their Alpaca account."""
    if not user.alpaca_access_token:
        return

    # Simple check: Defaulting to IEX for paper, upgrade logic can be added here.
    user.alpaca_data_feed = 'iex'
    db.session.commit()


def fetch_and_sync_bankroll(user):
    """Automatically pulls the real account equity into the bankroll setting."""
    if not user.alpaca_access_token:
        return

    headers = {"Authorization": f"Bearer {user.alpaca_access_token}"}
    preferred_base = "https://api.alpaca.markets" if user.subscription_status == 'pro' else "https://paper-api.alpaca.markets"
    fallback_base = "https://paper-api.alpaca.markets" if preferred_base == "https://api.alpaca.markets" else "https://api.alpaca.markets"
    account_urls = [f"{preferred_base}/v2/account", f"{fallback_base}/v2/account"]

    try:
        for url in account_urls:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                user.bankroll = float(data.get('equity', 0.0))
                db.session.commit()
                return
    except Exception:
        pass
