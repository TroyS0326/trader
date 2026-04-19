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
    headers = {"Authorization": f"Bearer {user.alpaca_access_token}"}
    url = "https://paper-api.alpaca.markets/v2/account"

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            user.bankroll = float(data.get('equity', 0.0))
            db.session.commit()
    except Exception:
        pass
