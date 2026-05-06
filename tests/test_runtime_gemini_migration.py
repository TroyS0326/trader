import importlib
import json
import sys
import types
from pathlib import Path


def test_no_runtime_google_generativeai_imports_in_python_sources():
    excluded = {".git", "venv", ".pytest_cache", "__pycache__"}
    for path in Path('.').rglob('*.py'):
        if any(part in excluded for part in path.parts):
            continue
        text = path.read_text(encoding='utf-8')
        if path.as_posix().startswith('tests/'):
            continue
        assert 'google.generativeai' not in text, f"Found in {path}"


def test_explainability_success_and_fallback(monkeypatch):
    import explainability

    setup = {"symbol": "AAPL", "setup_grade": "A", "score_total": 91}
    payload = {
        "thesis": "Solid setup.",
        "key_reasons": ["Reason 1", "Reason 2", "Reason 3"],
        "risk_note": "Manage risk.",
    }

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(explainability.gemini_client, "generate_text", lambda *a, **k: json.dumps(payload))

    out = explainability.generate_trade_thesis(setup)
    assert out == payload

    monkeypatch.setattr(explainability.gemini_client, "generate_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out_fallback = explainability.generate_trade_thesis(setup)
    assert out_fallback == explainability.generate_fallback_thesis(setup)


def _install_ai_catalyst_stubs(monkeypatch):
    fake_numpy = types.SimpleNamespace(mean=lambda x: sum(x) / len(x) if x else 0)
    monkeypatch.setitem(sys.modules, 'numpy', fake_numpy)
    monkeypatch.setitem(sys.modules, 'xgboost', types.SimpleNamespace())
    class FakeDataFrame:
        empty = True

    monkeypatch.setitem(sys.modules, 'pandas', types.SimpleNamespace(DataFrame=FakeDataFrame))
    monkeypatch.setitem(sys.modules, 'yfinance', types.SimpleNamespace(Ticker=lambda s: types.SimpleNamespace(info={}, financials=FakeDataFrame())))
    monkeypatch.setitem(sys.modules, 'feature_store', types.SimpleNamespace(store=types.SimpleNamespace(update_symbol_features=lambda *a, **k: None)))
    monkeypatch.setitem(sys.modules, 'scanner', types.SimpleNamespace(get_company_news=lambda *a, **k: [{"headline": "news"}]))
    monkeypatch.setitem(sys.modules, 'transformers', types.SimpleNamespace(pipeline=lambda *a, **k: (lambda texts: [])))


def test_ai_catalyst_success_fallback_and_risk_cap(monkeypatch):
    _install_ai_catalyst_stubs(monkeypatch)
    import ai_catalyst
    ai_catalyst = importlib.reload(ai_catalyst)

    monkeypatch.setenv("GEMINI_API_KEY", "k")

    mocked = {
        "symbol": "ZZZZ",
        "catalyst_score": 95,
        "risk_flag": "PUMP_AND_DUMP_RISK",
        "forensic_note": "risk detected",
    }
    monkeypatch.setattr(ai_catalyst.gemini_client, 'generate_text', lambda *a, **k: json.dumps(mocked))
    out = ai_catalyst.generate_catalyst_score('abC')
    assert out["symbol"] == "ABC"
    assert out["risk_flag"] == "PUMP_AND_DUMP_RISK"
    assert out["catalyst_score"] == 40

    monkeypatch.setattr(ai_catalyst.gemini_client, 'generate_text', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    fb = ai_catalyst.generate_catalyst_score('abC')
    assert fb["risk_flag"] == "PUMP_AND_DUMP_RISK"
    assert fb["catalyst_score"] == 35
