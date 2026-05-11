import os
for k in ["SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "ALPACA_CLIENT_ID", "ALPACA_CLIENT_SECRET", "ALPACA_REDIRECT_URI", "FINNHUB_API_KEY", "GEMINI_API_KEY"]:
    os.environ.setdefault(k, "test")

from datetime import datetime

import decision


def test_default_non_aggressive_regime_behavior_unchanged(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", False)
    out = decision.regime_trade_decision({"opportunity": 70, "tradability": 50, "entry_quality": 50}, datetime(2026, 1, 5, 10, 15), 0.5)
    assert out == "WATCH FOR BREAKOUT"


def test_aggressive_regime_morning_can_buy_now(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    monkeypatch.setattr(decision.config, "AGGRESSIVE_ALLOW_LUNCH_TRADING", False)
    out = decision.regime_trade_decision({"opportunity": 65, "tradability": 45, "entry_quality": 40}, datetime(2026, 1, 5, 10, 0), 0.5)
    assert out == "BUY NOW"


def test_aggressive_regime_lunch_blocked_unless_enabled(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    monkeypatch.setattr(decision.config, "AGGRESSIVE_ALLOW_LUNCH_TRADING", False)
    out = decision.regime_trade_decision({"opportunity": 99, "tradability": 99, "entry_quality": 99}, datetime(2026, 1, 5, 12, 0), 2.5)
    assert out == "WATCH"


def test_aggressive_regime_lunch_can_buy_when_explicitly_allowed(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    monkeypatch.setattr(decision.config, "AGGRESSIVE_ALLOW_LUNCH_TRADING", True)
    out = decision.regime_trade_decision({"opportunity": 75, "tradability": 45, "entry_quality": 55}, datetime(2026, 1, 5, 12, 15), 1.2)
    assert out == "BUY NOW"


def test_aggressive_momentum_buy_now(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    out = decision.momentum_trade_decision({}, datetime.now(), 0.0, {"day_change_pct": 18, "rvol": 2.0, "above_vwap": True, "spread_ok": True, "too_extended": False})
    assert out == "BUY NOW"


def test_aggressive_momentum_hard_safety_blocks(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    assert decision.momentum_trade_decision({}, datetime.now(), 0.0, {"data_stale": True}) == "DATA STALE"
    assert decision.momentum_trade_decision({}, datetime.now(), 0.0, {"below_stop": True}) == "SETUP BROKEN: BELOW STOP"
    assert decision.momentum_trade_decision({}, datetime.now(), 0.0, {"vwap_failure": True}) == "SETUP BROKEN: VWAP FAILURE"
    assert decision.momentum_trade_decision({}, datetime.now(), 0.0, {"buy_window_closed": True}) == "NO TRADE"
    assert decision.momentum_trade_decision({}, datetime.now(), 0.0, {"too_extended": True, "pullback_reclaim": False}) == "WATCH FOR PULLBACK"


def test_aggressive_pullback_reclaim_can_buy_now(monkeypatch):
    monkeypatch.setattr(decision.config, "AGGRESSIVE_INTRADAY_ENABLED", True)
    out = decision.momentum_trade_decision({}, datetime.now(), 0.0, {"pullback_reclaim": True, "above_vwap": True, "spread_ok": True, "rvol": 1.25})
    assert out == "BUY NOW"
