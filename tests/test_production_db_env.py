import importlib
import os
import sys
from pathlib import Path

import pytest

REQ = {
    'SECRET_KEY': 'x',
    'TOKEN_ENCRYPTION_KEY': 'x',
    'ALPACA_CLIENT_ID': 'x',
    'ALPACA_CLIENT_SECRET': 'x',
    'ALPACA_REDIRECT_URI': 'https://x/cb',
    'FINNHUB_API_KEY': 'x',
    'GEMINI_API_KEY': 'x',
}


def _load(monkeypatch, **env):
    for k, v in REQ.items():
        monkeypatch.setenv(k, v)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    sys.modules.pop('config', None)
    import config
    return importlib.reload(config)


def test_production_missing_database_url_fails(monkeypatch):
    with pytest.raises(ValueError, match='Production must use Postgres; veteran_trades.db is decommissioned.'):
        _load(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL=None)


def test_production_sqlite_fails(monkeypatch):
    with pytest.raises(ValueError, match='Production must use Postgres; veteran_trades.db is decommissioned.'):
        _load(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL='sqlite:////tmp/prod.db')


def test_production_veteran_trades_fails(monkeypatch):
    with pytest.raises(ValueError, match='Production must use Postgres; veteran_trades.db is decommissioned.'):
        _load(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL='postgresql+psycopg://u:p@h/veteran_trades.db')


def test_non_production_sqlite_works(monkeypatch, tmp_path):
    cfg = _load(monkeypatch, FLASK_ENV='testing', FLASK_DEBUG='1', DATABASE_URL=None, DB_PATH=str(tmp_path / 't.db'))
    assert cfg.SQLALCHEMY_DATABASE_URI.startswith('sqlite:///')


def test_validate_script_detects_legacy_refs():
    src = Path('scripts/validate_production_db_env.py').read_text()
    assert 'veteran_trades.db' in src
    assert 'backup-xeanvi-db.sh' in src
