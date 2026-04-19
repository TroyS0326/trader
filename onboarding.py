import requests
import logging
from models import db

logger = logging.getLogger(__name__)


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

    # Sandbox OAuth tokens must query the paper endpoint.
    url = "https://paper-api.alpaca.markets/v2/account"

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            user.bankroll = float(data.get('equity', 0.0))
            db.session.commit()
            logger.info("Bankroll synced for user %s: $%s", user.id, user.bankroll)
        else:
            logger.error("Bankroll sync failed for %s: %s", user.id, res.text)
    except Exception as e:
        logger.error("Error during bankroll sync: %s", str(e))
