import logging

import requests

from models import db

logger = logging.getLogger(__name__)


SIP_ENTITLEMENT_HINTS = {
    'sip',
    'unlimited',
    'algo_trader_plus',
    'advanced',
    'pro',
    'real_time',
}


def _is_sip_entitled_from_account_payload(account_data):
    """Returns True if Alpaca account metadata indicates SIP entitlement."""
    # Alpaca payload shapes have changed over time; we inspect a few likely fields.
    for key in (
        'market_data_subscription',
        'data_subscription',
        'subscription_plan',
        'plan',
    ):
        value = account_data.get(key)
        if isinstance(value, str) and value.strip().lower() in SIP_ENTITLEMENT_HINTS:
            return True

    entitlements = account_data.get('entitlements')
    if isinstance(entitlements, dict):
        for key in ('market_data', 'stocks', 'stock_data'):
            value = entitlements.get(key)
            if isinstance(value, str) and value.strip().lower() in SIP_ENTITLEMENT_HINTS:
                return True

    return False


def verify_alpaca_data_feed(user):
    """Detect whether the user is entitled to SIP and persist the allowed feed."""
    if not user.alpaca_access_token:
        return

    # Default to IEX unless the *user's own* account proves SIP entitlement.
    resolved_feed = 'iex'

    headers = {
        'Authorization': f'Bearer {user.alpaca_access_token}',
        'accept': 'application/json',
    }
    url = 'https://paper-api.alpaca.markets/v2/account'

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            account_data = res.json()
            if _is_sip_entitled_from_account_payload(account_data):
                resolved_feed = 'sip'
        else:
            logger.error('Data feed entitlement check failed for %s: %s', user.id, res.text)
    except Exception as exc:
        logger.error('Error during data feed entitlement check for %s: %s', user.id, str(exc))

    user.alpaca_data_feed = resolved_feed
    db.session.commit()


def fetch_and_sync_bankroll(user):
    """Automatically pulls the selected Alpaca environment equity into the bankroll setting."""
    if not user.alpaca_access_token:
        return

    # CRITICAL: Use the user's OAuth bearer token for Alpaca account requests.
    headers = {
        "Authorization": f"Bearer {user.alpaca_access_token}",
        "accept": "application/json",
    }

    trading_mode = getattr(user, "trading_mode", "paper")
    subscription_status = getattr(user, "subscription_status", "free")

    if trading_mode == "live" and subscription_status == "pro":
        url = "https://api.alpaca.markets/v2/account"
    else:
        url = "https://paper-api.alpaca.markets/v2/account"

    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            user.bankroll = float(data.get('equity', 0.0))
            db.session.commit()
            logger.info(
                "Bankroll synced for user %s mode=%s equity=$%s",
                user.id,
                trading_mode,
                user.bankroll,
            )
        else:
            logger.error(
                "Bankroll sync failed for user %s mode=%s endpoint=%s response=%s",
                user.id,
                trading_mode,
                url,
                res.text,
            )
    except Exception as e:
        logger.error("Error during bankroll sync: %s", str(e))
