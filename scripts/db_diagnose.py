#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask
from sqlalchemy import inspect, text

import config
from db_safety import database_identity, redact_database_uri
from models import db

KEY_TABLES = ('user', 'trades', 'scans', 'user_events', 'stripe_events', 'blog_posts', 'watch_candidates')


def main() -> int:
    diagnostic_app = Flask("db_diagnose")
    diagnostic_app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
    diagnostic_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    engine_options = getattr(config, 'SQLALCHEMY_ENGINE_OPTIONS', None)
    if isinstance(engine_options, dict):
        diagnostic_app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options

    db.init_app(diagnostic_app)

    uri = diagnostic_app.config.get('SQLALCHEMY_DATABASE_URI', config.SQLALCHEMY_DATABASE_URI)
    ident = database_identity(uri)

    print(f"FLASK_ENV={config.FLASK_ENV}")
    print(f"FLASK_DEBUG={'1' if config.DEBUG else '0'}")
    print(f"TESTING={'1' if config.IS_TESTING else '0'}")
    print(f"/etc/xeanvi/xeanvi.env exists={Path('/etc/xeanvi/xeanvi.env').exists()}")
    print(f"SQLALCHEMY_DATABASE_URI={redact_database_uri(uri)}")
    print(f"dialect={ident.get('dialect')} driver={ident.get('driver')}")

    with diagnostic_app.app_context():
        inspector = inspect(db.engine)
        tables = sorted(inspector.get_table_names())
        print(f"tables={tables}")

        for table in KEY_TABLES:
            if table not in tables:
                continue
            count = db.session.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
            print(f"{table}.count={count}")
            cols = {c['name'] for c in inspector.get_columns(table)}
            for col in ('created_at', 'updated_at'):
                if col in cols:
                    latest = db.session.execute(text(f'SELECT MAX("{col}") FROM "{table}"')).scalar_one()
                    print(f"{table}.latest_{col}={latest}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
