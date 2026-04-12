import pytest
from unittest.mock import MagicMock
import broker

def test_place_bracket_order_formatting(mocker):
    # 1. Mock the internal helper that sends the order
    # We target 'broker._post_json' so we don't have to worry about URL formatting in the test
    mock_post = mocker.patch('broker._post_json')
    mock_post.return_value = {'id': 'test_order_123', 'status': 'accepted'}

    # 2. THE KEY FIX: Mock 'get_order' directly inside the broker module
    # This ensures that when _poll_for_fill calls get_order, it gets our fake 'filled' status immediately
    mock_get_order = mocker.patch('broker.get_order')
    mock_get_order.return_value = {
        'id': 'test_order_123',
        'status': 'filled', # <--- This prevents the 15-second timeout
        'symbol': 'AAPL',
        'filled_avg_price': 150.00
    }

    # 3. Trigger the function
    result = broker.place_managed_entry_order(
        symbol="AAPL",
        qty=10,
        entry_price=150.00,
        stop_price=145.00,
        target_1_price=160.00,
        target_2_price=170.00
    )

    # 4. Assertions
    assert result['status'] == 'filled'
    assert result['id'] == 'test_order_123'
    assert mock_get_order.called
