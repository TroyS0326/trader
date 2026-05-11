from types import SimpleNamespace

from filters import passes_hard_gatekeeper


def _stats(**overrides):
    base = {
        "symbol": "AAPL",
        "price": 150.0,
        "daily_dollar_volume": 10_000_000.0,
        "spread_pct": 0.005,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_hard_gatekeeper_accepts_liquid_stock_above_5():
    keep, reason = passes_hard_gatekeeper(_stats(price=150.0))
    assert keep is True
    assert reason == ""


def test_hard_gatekeeper_rejects_sub_dollar_stock():
    keep, reason = passes_hard_gatekeeper(_stats(price=0.99))
    assert keep is False
    assert reason == "price_out_of_range"


def test_hard_gatekeeper_rejects_stock_above_500():
    keep, reason = passes_hard_gatekeeper(_stats(price=500.01))
    assert keep is False
    assert reason == "price_out_of_range"


def test_hard_gatekeeper_rejects_wide_spread_stock():
    keep, reason = passes_hard_gatekeeper(_stats(spread_pct=0.03))
    assert keep is False
    assert reason == "spread_too_wide"


def test_hard_gatekeeper_rejects_low_liquidity_stock():
    keep, reason = passes_hard_gatekeeper(_stats(daily_dollar_volume=1_500_000.0))
    assert keep is False
    assert reason == "insufficient_liquidity"
