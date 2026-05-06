import importlib
import sys
import pytest

REQUIRED_ENV = {
    'SECRET_KEY': 'test-secret',
    'TOKEN_ENCRYPTION_KEY': 'test-token',
    'ALPACA_CLIENT_ID': 'alpaca-client',
    'ALPACA_CLIENT_SECRET': 'alpaca-secret',
    'ALPACA_REDIRECT_URI': 'https://example.com/callback',
    'FINNHUB_API_KEY': 'finnhub-key',
    'GEMINI_API_KEY': 'gemini-key',
}


def _load_config(monkeypatch, **env):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    sys.modules.pop('config', None)
    import config
    return importlib.reload(config)


def test_normalize_postgres_url(monkeypatch):
    cfg = _load_config(monkeypatch, FLASK_ENV='testing', FLASK_DEBUG='1', DATABASE_URL='postgres://u:p@localhost/db')
    assert cfg.normalize_database_url('postgres://u:p@localhost/db').startswith('postgresql+psycopg://')


def test_normalize_postgresql_url(monkeypatch):
    cfg = _load_config(monkeypatch, FLASK_ENV='testing', FLASK_DEBUG='1', DATABASE_URL='postgresql://u:p@localhost/db')
    assert cfg.normalize_database_url('postgresql://u:p@localhost/db').startswith('postgresql+psycopg://')


def test_normalize_postgresql_psycopg_unchanged(monkeypatch):
    cfg = _load_config(monkeypatch, FLASK_ENV='testing', FLASK_DEBUG='1', DATABASE_URL='postgresql+psycopg://u:p@localhost/db')
    assert cfg.normalize_database_url('postgresql+psycopg://u:p@localhost/db') == 'postgresql+psycopg://u:p@localhost/db'


def test_production_missing_database_url_raises(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match='DATABASE_URL is required in production and must point to PostgreSQL.'):
        _load_config(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL=None, DB_PATH=str(tmp_path / 'prod.db'))


def test_production_sqlite_database_url_rejected(monkeypatch):
    with pytest.raises(ValueError, match='DATABASE_URL is required in production and must point to PostgreSQL.'):
        _load_config(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL='sqlite:////tmp/test.db')


def test_production_non_postgres_database_url_rejected(monkeypatch):
    with pytest.raises(ValueError, match='DATABASE_URL is required in production and must point to PostgreSQL.'):
        _load_config(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL='mysql://u:p@localhost/db')


def test_production_postgres_url_normalized_and_allowed(monkeypatch):
    cfg = _load_config(monkeypatch, FLASK_ENV='production', FLASK_DEBUG='0', DATABASE_URL='postgres://u:p@localhost/db')
    assert cfg.SQLALCHEMY_DATABASE_URI == 'postgresql+psycopg://u:p@localhost/db'


def test_non_production_empty_database_url_falls_back_to_sqlite(monkeypatch, tmp_path):
    cfg = _load_config(monkeypatch, FLASK_ENV='testing', FLASK_DEBUG='1', DATABASE_URL=None, DB_PATH=str(tmp_path / 't.db'))
    assert cfg.SQLALCHEMY_DATABASE_URI.startswith('sqlite:///')
