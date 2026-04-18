import requests
from models import db


def verify_alpaca_data_feed(user):
    """
    Checks a connected Alpaca OAuth account and assigns the best supported market-data feed.
    """
    if not user or not user.alpaca_access_token:
        return False

    headers = {
        "Authorization": f"Bearer {user.alpaca_access_token}",
        "accept": "application/json",
    }
    url = "https://paper-api.alpaca.markets/v2/account"

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        if data.get('account_number'):
            user.alpaca_data_feed = 'sip' if user.subscription_status == 'pro' else 'iex'
        else:
            user.alpaca_data_feed = 'iex'
        db.session.commit()
        return True
    except Exception as e:
        print(f"Error verifying feed: {e}")
        user.alpaca_data_feed = 'iex'
        db.session.commit()
        return False
