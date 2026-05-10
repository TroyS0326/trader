#!/usr/bin/env python3
import os
from pathlib import Path

import config
from app import app
from db_safety import database_identity, redact_database_uri
from models import db

KEY_TABLES = ('user', 'trades', 'scans', 'user_events', 'stripe_events', 'blog_posts', 'watch_candidates')


def main() -> int:
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', config.SQLALCHEMY_DATABASE_URI)
    ident = database_identity(uri)

    print(f"FLASK_ENV={config.FLASK_ENV}")
    print(f"FLASK_DEBUG={'1' if config.DEBUG else '0'}")
    print(f"TESTING={'1' if config.IS_TESTING else '0'}")
    print(f"/etc/xeanvi/xeanvi.env exists={Path('/etc/xeanvi/xeanvi.env').exists()}")
    print(f"SQLALCHEMY_DATABASE_URI={redact_database_uri(uri)}")
    print(f"dialect={ident.get('dialect')} driver={ident.get('driver')}")

    with app.app_context():
        inspector = db.inspect(db.engine)
        tables = sorted(inspector.get_table_names())
        print(f"tables={tables}")

        for table in KEY_TABLES:
            if table not in tables:
                continue
            count = db.session.execute(db.text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
            print(f"{table}.count={count}")
            cols = {c['name'] for c in inspector.get_columns(table)}
            for col in ('created_at', 'updated_at'):
                if col in cols:
                    latest = db.session.execute(db.text(f'SELECT MAX("{col}") FROM "{table}"')).scalar_one()
                    print(f"{table}.latest_{col}={latest}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
