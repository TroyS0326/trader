import pytest
from unittest.mock import MagicMock
import broker


def test_place_bracket_order_formatting(mocker):
    """
    Verifies that the broker correctly packages Buy, Stop, and Target orders
    before sending them to Alpaca.
    """
    # 1. Mock the actual network request to Alpaca
    mock_post = mocker.patch('requests.post')

    # 2. Simulate a successful response from Alpaca
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        'id': 'test_order_123',
        'status': 'accepted',
        'symbol': 'AAPL'
    }
    mock_post.return_value = mock_response

    # 3. Trigger the function with test data
    result = broker.place_managed_entry_order(
        symbol="AAPL",
        qty=10,
        entry_price=150.00,
        stop_price=145.00,
        target_1_price=160.00,
        target_2_price=170.00
    )

    # 4. ASSERTIONS: Check if the math sent to Alpaca was correct
    assert result['id'] == 'test_order_123'

    # Check that it attempted to send a 'bracket' order type
    args, kwargs = mock_post.call_args
    sent_data = kwargs['json']

    assert sent_data['symbol'] == 'AAPL'
    assert sent_data['order_class'] == 'bracket'
    assert sent_data['stop_loss']['stop_price'] == 145.00
    assert sent_data['take_profit']['limit_price'] == 160.00
