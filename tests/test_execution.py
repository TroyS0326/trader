import pytest
from app import app
import config


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_daily_loss_lock_prevents_execution(client, mocker):
    """
    Ensures that if the user hits their max daily failed trades,
    the system explicitly blocks new market orders.
    """
    # 1. Mock the database to pretend the user already failed 3 trades today
    config.MAX_FAILED_TRADES_PER_DAY = 3
    mocker.patch('app.get_failed_trades_today', return_value=3)

    # 2. Attempt to send a valid execution payload
    test_payload = {
        'symbol': 'AAPL',
        'entry_price': 150.00,
        'stop_price': 148.00,
        'target_1': 155.00,
        'target_2': 160.00,
        'qty': 100,
        'current_price': 149.50,
        'buy_upper': 151.00,
        'score_total': 95,
        'decision': 'BUY NOW'
    }

    # 3. Fire the request
    response = client.post('/api/execute', json=test_payload)
    data = response.get_json()

    # 4. Assert the system rejected the trade
    assert response.status_code == 403
    assert data['ok'] is False
    assert 'Daily loss lock is active' in data['error']
