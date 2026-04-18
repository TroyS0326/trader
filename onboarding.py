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


def fetch_and_sync_bankroll(user):
    """
    Queries Alpaca for the current account equity and updates the
    user's bankroll in the local database.
    """
    if not user or not user.alpaca_access_token:
        return {"success": False, "message": "No broker connection found."}

    headers = {
        "Authorization": f"Bearer {user.alpaca_access_token}",
        "Content-Type": "application/json",
    }

    # Default to paper endpoint for safety.
    url = "https://paper-api.alpaca.markets/v2/account"

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            account_data = response.json()
            real_equity = float(account_data.get("equity", 0.0))

            user.bankroll = real_equity
            db.session.commit()

            return {
                "success": True,
                "message": f"Bankroll synced. Current Equity: ${real_equity:,.2f}",
            }

        return {"success": False, "message": "Failed to retrieve account data from Alpaca."}
    except Exception as exc:
        return {"success": False, "message": f"Connection error: {str(exc)}"}
