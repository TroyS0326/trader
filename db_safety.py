import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


def redact_database_uri(uri: str) -> str:
    raw = (uri or '').strip()
    if not raw:
        return ''
    try:
        parsed = urlsplit(raw)
    except Exception:
        return '<invalid-uri>'

    username = parsed.username or ''
    hostname = parsed.hostname or ''
    port = f":{parsed.port}" if parsed.port else ''
    userinfo = ''
    if username:
        userinfo = username
        if parsed.password is not None:
            userinfo += ':***'
        userinfo += '@'
    netloc = f"{userinfo}{hostname}{port}"

    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        masked = '***' if any(tok in key.lower() for tok in ('password', 'pass', 'token', 'secret', 'key')) else value
        query_pairs.append((key, masked))
    query = urlencode(query_pairs)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def database_identity(uri: str) -> dict:
    raw = (uri or '').strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme or 'unknown'
    dialect, _, driver = scheme.partition('+')
    ident = {
        'dialect': dialect or 'unknown',
        'driver': driver or None,
        'host': parsed.hostname,
        'port': parsed.port,
        'database': None,
        'sqlite_path': None,
        'redacted_uri': redact_database_uri(raw),
    }

    if dialect == 'sqlite':
        db_path = (parsed.path or '').strip()
        if db_path == ':memory:' or raw.endswith(':memory:'):
            ident['database'] = ':memory:'
            ident['sqlite_path'] = ':memory:'
        else:
            clean_path = db_path.lstrip('/')
            ident['database'] = clean_path or db_path
            ident['sqlite_path'] = db_path
    else:
        ident['database'] = (parsed.path or '').lstrip('/') or None
    return ident


def _runtime_values(app=None):
    if app is not None:
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', config.SQLALCHEMY_DATABASE_URI)
    else:
        uri = config.SQLALCHEMY_DATABASE_URI
    return config.IS_PRODUCTION, config.IS_TESTING, (uri or '').strip()


def validate_runtime_database_safety(app=None) -> None:
    is_production, is_testing, uri = _runtime_values(app)
    identity = database_identity(uri)
    logger.info('Database runtime identity: %s', identity)

    if not is_production:
        return

    if config.FLASK_ENV != 'production':
        raise RuntimeError('Unsafe runtime: FLASK_ENV must be exactly "production" in production mode.')

    raw_database_url = os.getenv('DATABASE_URL', '').strip()
    if not raw_database_url:
        raise RuntimeError('Unsafe runtime: DATABASE_URL must be set and non-empty in production.')

    if not uri.startswith('postgresql+psycopg://'):
        raise RuntimeError('Unsafe runtime: production database must use postgresql+psycopg:// URI.')

    if identity['dialect'] == 'sqlite':
        raise RuntimeError('Unsafe runtime: sqlite is not allowed in production.')

    sqlite_path = identity.get('sqlite_path') or ''
    repo_root = Path(__file__).resolve().parent
    if sqlite_path:
        abs_path = Path(sqlite_path).expanduser().resolve()
        if sqlite_path == ':memory:' or '/tmp/' in sqlite_path or not Path(sqlite_path).is_absolute() or repo_root in abs_path.parents:
            raise RuntimeError('Unsafe runtime: sqlite path is not allowed in production.')

    if is_testing:
        raise RuntimeError('Unsafe runtime: testing mode must not be active in production.')


def assert_not_empty_production_database(db, required_tables=('user',)) -> None:
    if not config.IS_PRODUCTION:
        return

    inspector = inspect(db.engine)
    table_names = sorted(inspector.get_table_names())
    logger.info('Production database table count=%d tables=%s', len(table_names), table_names)

    for table_name in required_tables:
        if table_name not in table_names:
            raise RuntimeError(f'Production safety check failed: required table "{table_name}" is missing.')

    user_count = db.session.execute(text('SELECT COUNT(*) FROM "user"')).scalar_one()
    logger.info('Production database user table row_count=%s', user_count)

    if user_count == 0 and os.getenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', '0') != '1':
        raise RuntimeError('Production safety check failed: user table is empty. Set ALLOW_EMPTY_PRODUCTION_DB_STARTUP=1 only for controlled emergency startup.')


def assert_existing_production_database_has_users(db) -> None:
    if not config.IS_PRODUCTION:
        return

    inspector = inspect(db.engine)
    table_names = sorted(inspector.get_table_names())
    logger.info('Production pre-create database table count=%d tables=%s', len(table_names), table_names)

    if 'user' not in table_names:
        raise RuntimeError('Production safety check failed before schema creation: production DB appears fresh or wrong because required user table is missing.')

    user_count = db.session.execute(text('SELECT COUNT(*) FROM "user"')).scalar_one()
    logger.info('Production pre-create user table row_count=%s', user_count)

    if user_count == 0 and os.getenv('ALLOW_EMPTY_PRODUCTION_DB_STARTUP', '0') != '1':
        raise RuntimeError('Production safety check failed before schema creation: user table is empty. Set ALLOW_EMPTY_PRODUCTION_DB_STARTUP=1 only for controlled emergency startup.')
