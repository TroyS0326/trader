import importlib
import os
import sys
from pathlib import Path

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



def test_app_imports_db_safety_functions():
    src = Path('app.py').read_text()
    assert 'assert_existing_production_database_has_users' in src
    assert 'validate_runtime_database_safety' in src
    assert 'assert_not_empty_production_database' in src


def test_diagnostic_script_does_not_import_real_app():
    src = Path('scripts/db_diagnose.py').read_text()
    assert 'from app import app' not in src


def _setup_user_table(db, with_row=False):
    db.session.execute(db_safety_module_text('CREATE TABLE "user" (id INTEGER PRIMARY KEY, email TEXT)'))
    if with_row:
        db.session.execute(db_safety_module_text("INSERT INTO \"user\" (email) VALUES ('u@example.com')"))
    db.session.commit()


def db_safety_module_text(sql):
    from sqlalchemy import text
    return text(sql)


def test_existing_production_db_fails_when_user_missing(monkeypatch, tmp_path, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', f"sqlite:///{tmp_path/'safe.db'}")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'safe.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        with pytest.raises(RuntimeError, match='user table is missing|user table'):
            db_safety_module.assert_existing_production_database_has_users(db)


def test_existing_production_db_fails_when_user_empty(monkeypatch, tmp_path, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', f"sqlite:///{tmp_path/'safe2.db'}")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'safe2.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        _setup_user_table(db, with_row=False)
        monkeypatch.delenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', raising=False)
        with pytest.raises(RuntimeError, match='empty'):
            db_safety_module.assert_existing_production_database_has_users(db)


def test_existing_production_db_allows_empty_with_override(monkeypatch, tmp_path, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', f"sqlite:///{tmp_path/'safe3.db'}")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'safe3.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    with app.app_context():
        _setup_user_table(db, with_row=False)
        monkeypatch.setenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', '1')
        db_safety_module.assert_existing_production_database_has_users(db)


def test_assert_not_empty_production_database_uses_sqlalchemy_inspect(monkeypatch, tmp_path, db_safety_module):
    _patch_runtime(monkeypatch, db_safety_module, True, False, 'production', f"sqlite:///{tmp_path/'safe4.db'}")
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{tmp_path/'safe4.db'}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    called = {'ok': False}

    real_inspect = db_safety_module.inspect
    def _wrapped(engine):
        called['ok'] = True
        return real_inspect(engine)

    monkeypatch.setattr(db_safety_module, 'inspect', _wrapped)
    with app.app_context():
        _setup_user_table(db, with_row=True)
        db_safety_module.assert_not_empty_production_database(db)
    assert called['ok'] is True
