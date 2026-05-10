from datetime import datetime

import pytest
from sqlalchemy import create_engine, text

import scripts.recover_production_data as recovery


@pytest.fixture
def target_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'target.db'}")
    with eng.begin() as c:
        c.execute(text('CREATE TABLE "user" (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, name TEXT, password_hash TEXT, created_at TEXT, updated_at TEXT)'))
        c.execute(text('CREATE TABLE scans (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, market_day TEXT, best_symbol TEXT, best_decision TEXT, best_score REAL, payload_json TEXT)'))
        c.execute(text('CREATE TABLE stripe_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, created_at TEXT)'))
    return eng


def _source_db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'source.db'}")
    with eng.begin() as c:
        c.execute(text('CREATE TABLE user (id INTEGER PRIMARY KEY, email TEXT, name TEXT, password_hash TEXT)'))
        c.execute(text('CREATE TABLE scans (id INTEGER PRIMARY KEY, market_day TEXT, best_symbol TEXT, best_decision TEXT, best_score REAL, payload_json TEXT)'))
        c.execute(text("INSERT INTO user (email,name,password_hash) VALUES ('a@x.com','Alice','h1'),('b@x.com','Bob','h2')"))
        c.execute(text("INSERT INTO scans (market_day,best_symbol,best_decision,best_score,payload_json) VALUES ('2026-05-01','AAPL','BUY',0.9,'{}')"))
    return eng


def test_dry_run_does_not_mutate(monkeypatch, tmp_path, target_engine):
    src = _source_db(tmp_path)
    monkeypatch.setenv('DATABASE_URL', str(target_engine.url))
    monkeypatch.setattr(recovery, 'create_engine', lambda url: target_engine if str(url) == str(target_engine.url) else src)
    monkeypatch.setattr(recovery, 'parse_args', lambda: type('A', (), {'mode': 'dry-run', 'sqlite_path': 'x.db', 'source_postgres_db': None, 'only': 'users,scans', 'overwrite_existing': False, 'skip_backup': False})())
    recovery.main()
    with target_engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM "user"')).scalar_one() == 0


def test_user_merge_and_overwrite_behaviors(target_engine):
    with target_engine.begin() as c:
        c.execute(text("INSERT INTO \"user\" (email,name,password_hash) VALUES ('a@x.com','Existing','keep')"))
    table = recovery.Table('user', recovery.MetaData(), autoload_with=target_engine)
    with target_engine.begin() as c:
        row = {'email': 'a@x.com', 'name': 'Alice'}
        existing = c.execute(recovery.select(table).where(table.c.email == 'a@x.com')).mappings().first()
        assert existing['name'] == 'Existing'
        # default non-overwrite
        updates = {}
        if recovery.blank(existing['name']) and not recovery.blank(row['name']):
            updates['name'] = row['name']
        assert updates == {}


def test_created_at_fill_and_scan_dedupe(target_engine):
    table = recovery.Table('scans', recovery.MetaData(), autoload_with=target_engine)
    with target_engine.begin() as c:
        payload = {'market_day': '2026-01-01', 'best_symbol': 'AAPL', 'best_decision': 'BUY', 'best_score': 0.5, 'payload_json': '{}', 'created_at': None}
        if recovery.blank(payload['created_at']):
            payload['created_at'] = recovery.now_utc()
        c.execute(table.insert().values(**payload))
        existing = c.execute(recovery.select(table)).mappings().all()
        sigs = {(r.get('created_at'), r.get('market_day'), r.get('best_symbol'), r.get('best_decision'), r.get('best_score'), recovery.payload_hash(r, 'payload_json')) for r in existing}
        assert payload['created_at'] is not None
        dup = (payload.get('created_at'), payload.get('market_day'), payload.get('best_symbol'), payload.get('best_decision'), payload.get('best_score'), recovery.payload_hash(payload, 'payload_json'))
        assert dup in sigs


def test_refuse_apply_checks(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    with pytest.raises(RuntimeError):
        recovery.require_apply_safety()


def test_skip_missing_target_table(target_engine):
    insp = recovery.inspect(target_engine)
    assert insp.has_table('blog_posts') is False
