import requests
from models import db, User 

def verify_alpaca_data_feed(user_id, api_key, api_secret, claimed_feed):
    """
    Trust, but verify. Checks the user's claimed data feed against Alpaca's actual servers.
    """
    user = User.query.get(user_id)
    if not user:
        return {"success": False, "message": "User not found."}

    # Save their API keys (in Phase 2, this is replaced by OAuth tokens)
    user.alpaca_access_token = f"{api_key}:{api_secret}" # Temporary storage for testing Phase 1

    # If they honestly selected the Free feed, just assign it and skip the test to save API calls
    if claimed_feed == 'iex':
        user.data_feed = 'iex'
        db.session.commit()
        return {
            "success": True, 
            "message": "Broker connected. Free (IEX) data feed activated. Consider upgrading for better performance."
        }

    # If they claimed SIP (Pro), we must run the Silent Test
    headers = {
        'accept': 'application/json',
        'APCA-API-KEY-ID': api_key,
        'APCA-API-SECRET-KEY': api_secret,
    }

    # Silently ask for 1 minute of Apple stock using the 'sip' feed parameter
    url = "https://data.alpaca.markets/v2/stocks/bars"
    params = {
        'symbols': 'AAPL',
        'timeframe': '1Min',
        'limit': 1,
        'feed': 'sip'
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            # They told the truth and have the Pro plan!
            user.data_feed = 'sip'
            message = "Broker connected successfully. Market Data+ (SIP) feed verified and activated!"
        else:
            # They claimed SIP, but Alpaca rejected it (Likely a 403 Forbidden)
            user.data_feed = 'iex'
            message = "Broker connected, but Alpaca rejected SIP access. We have safely downgraded you to the Free (IEX) feed to prevent crashes."

        db.session.commit()
        return {"success": True, "message": message}

    except Exception as e:
        return {"success": False, "message": f"Could not connect to Alpaca: {str(e)}"}
