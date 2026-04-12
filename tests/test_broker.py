import pytest
from unittest.mock import MagicMock
import broker


def test_place_bracket_order_formatting(mocker):
    # 1. Mock the POST request (placing the order)
    mock_post = mocker.patch('requests.post')
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200
    mock_post_resp.json.return_value = {'id': 'test_order_123', 'status': 'accepted'}
    mock_post.return_value = mock_post_resp

    # 2. THE FIX: Mock the GET request (checking if it is filled)
    # This tells the bot the order was filled instantly so it doesn't time out
    mock_get = mocker.patch('requests.get')
    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = {
        'id': 'test_order_123',
        'status': 'filled',  # <--- This stops the 15-second timer
        'symbol': 'AAPL',
        'filled_avg_price': 150.00,
    }
    mock_get.return_value = mock_get_resp

    # 3. Trigger the function
    result = broker.place_managed_entry_order(
        symbol="AAPL",
        qty=10,
        entry_price=150.00,
        stop_price=145.00,
        target_1_price=160.00,
        target_2_price=170.00,
    )

    # 4. Assertions
    assert result['status'] == 'filled'
    assert mock_post.called
    assert mock_get.called
