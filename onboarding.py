import requests

from models import User, db  # Importing the database we built in Step 1


def verify_alpaca_data_feed(user_id, api_key, api_secret):
    """
    Silently tests the user's keys to see if they have the Pro (SIP) data feed
    or the Free (IEX) feed, and updates their profile.
    """
    # 1. Setup the headers using this specific user's keys, NOT your .env keys
    headers = {
        'accept': 'application/json',
        'APCA-API-KEY-ID': api_key,
        'APCA-API-SECRET-KEY': api_secret,
    }

    # 2. The Silent Test: Try to pull 1 bar of AAPL data specifically requesting the 'sip' feed
    url = 'https://data.alpaca.markets/v2/stocks/bars'
    params = {
        'symbols': 'AAPL',
        'timeframe': '1Min',
        'limit': 1,
        'feed': 'sip',  # <--- We intentionally ask for the Pro feed
    }

    # 3. Find the user in our database
    user = User.query.get(user_id)
    if not user:
        return {'success': False, 'message': 'User not found.'}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)

        if response.status_code == 200:
            # SUCCESS! Alpaca accepted the request. They have the Pro plan.
            user.data_feed = 'sip'
            message = 'Connection successful. Market Data+ (SIP) feed activated.'
        else:
            # FORBIDDEN (403). Alpaca rejected the SIP request. They are on the Free plan.
            user.data_feed = 'iex'
            message = (
                'Connection successful. Defaulting to Free (IEX) data feed. '
                'Upgrade your Alpaca account for faster execution.'
            )

        # 4. Save the results to the database
        db.session.commit()
        return {'success': True, 'feed_assigned': user.data_feed, 'message': message}

    except Exception as e:
        return {'success': False, 'message': f'Could not connect to Alpaca: {str(e)}'}
