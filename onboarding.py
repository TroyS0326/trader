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


PAPER_ACCOUNT_URL = "https://paper-api.alpaca.markets/v2/account"
LIVE_ACCOUNT_URL = "https://api.alpaca.markets/v2/account"


def _is_sip_entitled_from_account_payload(account_data):
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


def _account_headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
    }


def _fetch_account_payload(token: str, url: str):
    try:
        res = requests.get(url, headers=_account_headers(token), timeout=10)
        if res.status_code == 200:
            return res.json()
        logger.warning("Alpaca account check failed endpoint=%s status=%s body=%s", url, res.status_code, res.text)
    except Exception as exc:
        logger.error("Alpaca account check error endpoint=%s error=%s", url, exc)
    return None


def detect_and_store_alpaca_connection(user, token: str) -> dict:
    """
    Called after OAuth callback.

    It tests the returned OAuth token against both Alpaca Paper and Alpaca Live.
    Whichever account endpoint accepts the token gets stored in the matching slot.
    """
    result = {
        "paper_connected": False,
        "live_connected": False,
        "paper_account_id": None,
        "live_account_id": None,
        "paper_equity": None,
        "live_equity": None,
    }

    paper_data = _fetch_account_payload(token, PAPER_ACCOUNT_URL)
    if paper_data:
        user.alpaca_paper_access_token = token
        user.alpaca_paper_account_id = paper_data.get("id") or paper_data.get("account_id")
        user.paper_bankroll = float(paper_data.get("equity") or 0.0)
        result["paper_connected"] = True
        result["paper_account_id"] = user.alpaca_paper_account_id
        result["paper_equity"] = user.paper_bankroll

    live_data = _fetch_account_payload(token, LIVE_ACCOUNT_URL)
    if live_data:
        user.alpaca_live_access_token = token
        user.alpaca_live_account_id = live_data.get("id") or live_data.get("account_id")
        user.live_bankroll = float(live_data.get("equity") or 0.0)
        result["live_connected"] = True
        result["live_account_id"] = user.alpaca_live_account_id
        result["live_equity"] = user.live_bankroll

    # Legacy compatibility: keep old fields populated with whichever account is active.
    if getattr(user, "trading_mode", "paper") == "live" and user.alpaca_live_access_token:
        user.alpaca_access_token = user.alpaca_live_access_token
        user.alpaca_account_id = user.alpaca_live_account_id
    elif user.alpaca_paper_access_token:
        user.alpaca_access_token = user.alpaca_paper_access_token
        user.alpaca_account_id = user.alpaca_paper_account_id
    elif user.alpaca_live_access_token:
        user.alpaca_access_token = user.alpaca_live_access_token
        user.alpaca_account_id = user.alpaca_live_account_id

    user.sync_legacy_bankroll_from_active_mode()

    # Data feed entitlement: default IEX. Upgrade if either connected account proves SIP.
    resolved_feed = "iex"
    if paper_data and _is_sip_entitled_from_account_payload(paper_data):
        resolved_feed = "sip"
    if live_data and _is_sip_entitled_from_account_payload(live_data):
        resolved_feed = "sip"

    user.alpaca_data_feed = resolved_feed

    db.session.commit()
    return result


def verify_alpaca_data_feed(user):
    """
    Detect whether user is entitled to SIP.
    Uses whichever token exists.
    """
    token = user.alpaca_live_access_token or user.alpaca_paper_access_token or user.alpaca_access_token
    if not token:
        return

    resolved_feed = 'iex'

    for url in (PAPER_ACCOUNT_URL, LIVE_ACCOUNT_URL):
        account_data = _fetch_account_payload(token, url)
        if account_data and _is_sip_entitled_from_account_payload(account_data):
            resolved_feed = 'sip'
            break

    user.alpaca_data_feed = resolved_feed
    db.session.commit()


def fetch_and_sync_bankroll(user):
    """
    Pulls equity from the selected environment only.

    Paper mode uses alpaca_paper_access_token.
    Live mode uses alpaca_live_access_token.
    """
    trading_mode = getattr(user, "trading_mode", "paper")
    subscription_status = getattr(user, "subscription_status", "free")

    if trading_mode == "live" and subscription_status == "pro":
        token = user.alpaca_live_access_token
        url = LIVE_ACCOUNT_URL
        mode = "live"
    else:
        token = user.alpaca_paper_access_token
        url = PAPER_ACCOUNT_URL
        mode = "paper"

    if not token:
        logger.warning("Bankroll sync skipped for user %s mode=%s: no token", user.id, mode)
        user.sync_legacy_bankroll_from_active_mode()
        db.session.commit()
        return

    data = _fetch_account_payload(token, url)
    if not data:
        logger.error("Bankroll sync failed for user %s mode=%s endpoint=%s", user.id, mode, url)
        user.sync_legacy_bankroll_from_active_mode()
        db.session.commit()
        return

    equity = float(data.get("equity") or 0.0)

    if mode == "live":
        user.live_bankroll = equity
        user.alpaca_live_account_id = data.get("id") or data.get("account_id") or user.alpaca_live_account_id
    else:
        user.paper_bankroll = equity
        user.alpaca_paper_account_id = data.get("id") or data.get("account_id") or user.alpaca_paper_account_id

    user.sync_legacy_bankroll_from_active_mode()
    db.session.commit()

    logger.info(
        "Bankroll synced user=%s mode=%s equity=$%s",
        user.id,
        mode,
        equity,
    )
