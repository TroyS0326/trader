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

