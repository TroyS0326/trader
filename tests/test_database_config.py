import importlib
import os


def _reload_config():
    import config
    return importlib.reload(config)


def test_build_database_uri_converts_postgres(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgres://u:p@localhost:5432/db')
    monkeypatch.setenv('FLASK_ENV', 'testing')
    monkeypatch.setenv('FLASK_DEBUG', '1')
    cfg = _reload_config()
    assert cfg.build_database_uri().startswith('postgresql+psycopg://')


def test_build_database_uri_local_sqlite_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'local.db'))
    monkeypatch.setenv('FLASK_ENV', 'testing')
    monkeypatch.setenv('FLASK_DEBUG', '1')
    cfg = _reload_config()
    assert cfg.SQLALCHEMY_DATABASE_URI.startswith('sqlite:///')
