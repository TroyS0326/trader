from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan_contract import validate_scan_payload_contract, decision_allowlist_from_env


def test_valid_best_pick_contract_passes():
    result = validate_scan_payload_contract({"best_pick": {"symbol": "AAPL", "decision": "BUY NOW", "qty": 2, "entry_price": 10, "stop_price": 9, "target_1": 11, "target_2": 12}})
    assert result["has_best_pick"] is True
    assert result["best_pick_key_used"] == "best_pick"
    assert result["executable_payload_ready"] is True


def test_watchlist_first_item_detected_when_best_pick_missing():
    result = validate_scan_payload_contract({"watchlist": [{"symbol": "MSFT", "decision": "WATCH", "qty": 0}]})
    assert result["best_pick_key_used"] == "watchlist[0]"
    assert "No best_pick/best/top_pick key; watchlist[0] present." in result["payload_shape_notes"]


def test_missing_qty_reported():
    result = validate_scan_payload_contract({"best_pick": {"symbol": "AAPL", "decision": "BUY NOW", "entry_price": 10, "stop_price": 9, "target_1": 11, "target_2": 12}})
    assert "qty" in result["missing_order_fields"]
    assert result["qty_valid"] is False


def test_alias_fields_normalized():
    result = validate_scan_payload_contract({"best": {"ticker": "tsla", "action": "BUY", "shares": "3", "entry": "100", "stop": "95", "target_1_price": "110", "target_2_price": "120"}})
    fields = result["normalized_order_fields"]
    assert result["best_pick_key_used"] == "best"
    assert fields == {"symbol": "TSLA", "qty": 3, "entry_price": 100.0, "stop_price": 95.0, "target_1": 110.0, "target_2": 120.0}


def test_non_executable_decision_reported():
    result = validate_scan_payload_contract({"best_pick": {"symbol": "AAPL", "decision": "WATCH", "qty": 2, "entry_price": 10, "stop_price": 9, "target_1": 11, "target_2": 12}})
    assert result["decision_is_executable"] is False
    assert result["executable_payload_ready"] is False


def test_decision_allowlist_default_executable_set(monkeypatch):
    monkeypatch.delenv('CENTRAL_SCANNER_EXECUTE_DECISIONS', raising=False)
    allow = decision_allowlist_from_env()
    assert {'BUY NOW', 'A+', 'A'} <= allow
    assert validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'BUY NOW', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})['decision_is_executable'] is True
    assert validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'A+', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})['decision_is_executable'] is True
    assert validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'A', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})['decision_is_executable'] is True


def test_buy_not_executable_by_default_but_allowed_via_env(monkeypatch):
    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTE_DECISIONS', 'BUY NOW,A+,A')
    r = validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'BUY', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})
    assert r['decision_is_executable'] is False

    monkeypatch.setenv('CENTRAL_SCANNER_EXECUTE_DECISIONS', 'BUY NOW,A+,A,BUY')
    r2 = validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'BUY', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})
    assert r2['decision_is_executable'] is True


def test_watch_remains_non_executable(monkeypatch):
    monkeypatch.delenv('CENTRAL_SCANNER_EXECUTE_DECISIONS', raising=False)
    r = validate_scan_payload_contract({'best_pick': {'symbol': 'AAPL', 'decision': 'WATCH', 'qty': 2, 'entry_price': 10, 'stop_price': 9, 'target_1': 11, 'target_2': 12}})
    assert r['decision_is_executable'] is False


def test_top_level_payload_shape_notes_are_preserved():
    result = validate_scan_payload_contract({"payload_shape_notes": ["PAYLOAD_JSON_MISSING_OR_INVALID"]})
    assert "PAYLOAD_JSON_MISSING_OR_INVALID" in result["payload_shape_notes"]


def test_internal_payload_shape_notes_are_preserved_and_deduped():
    result = validate_scan_payload_contract({"payload_shape_notes": ["PAYLOAD_JSON_MISSING_OR_INVALID"], "_payload_shape_notes": ["PAYLOAD_JSON_MISSING_OR_INVALID", 123]})
    assert result["payload_shape_notes"].count("PAYLOAD_JSON_MISSING_OR_INVALID") == 1
