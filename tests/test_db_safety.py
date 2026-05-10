import importlib
import os
import sys

import pytest
from flask import Flask

from models import db


@pytest.fixture
def db_safety_module(monkeypatch):
    required = {
        'SECRET_KEY': 's',
        'TOKEN_ENCRYPTION_KEY': 't',
        'ALPACA_CLIENT_ID': 'id',
        'ALPACA_CLIENT_SECRET': 'sec',
        'ALPACA_REDIRECT_URI': 'https://example.com/cb',
        'FINNHUB_API_KEY': 'f',
        'GEMINI_API_KEY': 'g',
        'FLASK_ENV': 'testing',
        'FLASK_DEBUG': '1',
        'DATABASE_URL': '',
    }
    for k, v in required.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop('config', None)
    sys.modules.pop('db_safety', None)
    import config  # noqa: F401
    import db_safety
    return importlib.reload(db_safety)


def _patch_runtime(monkeypatch, db_safety, is_prod, is_testing, env, uri):
    monkeypatch.setattr(db_safety.config, 'IS_PRODUCTION', is_prod)
    monkeypatch.setattr(db_safety.config, 'IS_TESTING', is_testing)
    monkeypatch.setattr(db_safety.config, 'FLASK_ENV', env)
    monkeypatch.setattr(db_safety.config, 'SQLALCHEMY_DATABASE_URI', uri)


def test_production_missing_database_url_fails(monkeypatch, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', 'postgresql+psycopg://u:p@localhost/db')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(RuntimeError, match='DATABASE_URL'):
        db_safety_module.validate_runtime_database_safety()


def test_production_sqlite_uri_fails(monkeypatch, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', 'sqlite:////tmp/prod.db')
    monkeypatch.setenv('DATABASE_URL', 'sqlite:////tmp/prod.db')
    with pytest.raises(RuntimeError, match=r'postgresql\+psycopg'):
        db_safety_module.validate_runtime_database_safety()


def test_production_postgres_uri_passes(monkeypatch, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', 'postgresql+psycopg://u:p@db.example.com:5432/app')
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://u:p@db.example.com:5432/app')
    db_safety_module.validate_runtime_database_safety()


def test_redaction_hides_password(db_safety_module):
    out = db_safety_module.redact_database_uri('postgresql+psycopg://user:supersecret@host:5432/name?token=abc')
    assert 'supersecret' not in out
    assert 'token=***' in out


def test_non_production_sqlite_allowed(monkeypatch, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, False, True, 'testing', 'sqlite:////tmp/test.db')
    db_safety_module.validate_runtime_database_safety()


def test_empty_production_user_table_fails_unless_override(monkeypatch, tmp_path, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', f"sqlite:///{tmp_path/'safe.db'}")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'safe.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        db.session.execute(db.text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT)'))
        db.session.commit()
        monkeypatch.delenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', raising=False)
        with pytest.raises(RuntimeError, match='empty'):
            db_safety_module.assert_not_empty_production_database(db)
        monkeypatch.setenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', '1')
        db_safety_module.assert_not_empty_production_database(db)
