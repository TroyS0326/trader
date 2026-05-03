import os
from pathlib import Path

from flask import Flask

import config
from models import db, Trade
from db import maybe_store_realized_pnl


BASE_DIR = Path(__file__).resolve().parent


def create_backfill_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.abspath(config.DB_PATH)}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def main() -> None:
    app = create_backfill_app()

    with app.app_context():
        trades = Trade.query.filter(Trade.pnl.is_(None)).order_by(Trade.id.asc()).all()

        updated = 0
        skipped = 0

        for trade in trades:
            before = trade.pnl
            maybe_store_realized_pnl(trade)

            if before is None and trade.pnl is not None:
                updated += 1
            else:
                skipped += 1

        db.session.commit()

        print(f"Backfill complete.")
        print(f"Updated trades: {updated}")
        print(f"Skipped trades: {skipped}")


if __name__ == "__main__":
    main()
