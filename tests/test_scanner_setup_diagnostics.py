from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('requests', SimpleNamespace())
sys.modules.setdefault('dotenv', SimpleNamespace(load_dotenv=lambda *a, **k: None))

import scanner


def test_build_setup_grade_diagnostics_reports_no_trade_failures():
    diagnostics = scanner.build_setup_grade_diagnostics(
        total=10,
        catalyst_score=1,
        liquidity_score=1,
        sector_score=0,
        confirm_score=1,
        vwap_score=1,
        pullback_score=1,
        premarket_gap_pct=0.5,
        premarket_notional=100_000,
    )
    assert diagnostics["setup_grade"] == "NO TRADE"
    assert "TOTAL_SCORE_BELOW_WATCH_THRESHOLD" in diagnostics["failed_watch_requirements"]
    assert "CATALYST_SCORE_BELOW_WATCH_THRESHOLD" in diagnostics["failed_watch_requirements"]
    assert "LIQUIDITY_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "CONFIRM_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "VWAP_SCORE_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "PREMARKET_GAP_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]
    assert "PREMARKET_DOLLAR_VOLUME_BELOW_A_THRESHOLD" in diagnostics["failed_a_requirements"]


def test_execution_eligibility_reason_uses_allowlist_semantics():
    executable = scanner.build_setup_grade_diagnostics(
        total=scanner.A_SCORE,
        catalyst_score=4,
        liquidity_score=3,
        sector_score=scanner.MIN_SECTOR_SYMPATHY_SCORE,
        confirm_score=3,
        vwap_score=3,
        pullback_score=2,
        premarket_gap_pct=scanner.MIN_PREMARKET_GAP_PCT,
        premarket_notional=scanner.MIN_PREMARKET_DOLLAR_VOL,
    )
    assert executable["setup_grade"] in {"A", "A+"}


def _mk_bar(ts, o=10,h=11,l=9,c=10,v=1000):
    return {'t': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def test_opening_range_reason_no_opening_range_bars(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar('2026-05-08T14:00:00+00:00'), _mk_bar('2026-05-08T14:50:00+00:00')]
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete_reason']=='NO_OPENING_RANGE_BARS'


def test_opening_range_reason_latest_before_end(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[_mk_bar('2026-05-08T13:30:00+00:00'), _mk_bar('2026-05-08T13:35:00+00:00')]
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete_reason']=='LATEST_BAR_BEFORE_OR_END'


def test_opening_range_complete(monkeypatch):
    monkeypatch.setattr(scanner, 'buy_window_open', lambda: True)
    bars=[]
    for i in range(20):
        bars.append(_mk_bar(f'2026-05-08T13:{30+i:02d}:00+00:00'))
    stats=scanner.get_opening_range_stats(bars)
    assert stats['opening_range_complete'] is True
