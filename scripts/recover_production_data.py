#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

DEFAULT_TABLES = ["users", "scans", "user_events", "stripe_events", "blog_posts", "market_regimes"]
OPTIONAL_TABLES = ["trade_audit_logs", "waitlist"]


@dataclass
class TableReport:
    source_name: str
    table: str
    source_rows: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    skip_reasons: list[str] = field(default_factory=list)


class SourceAdapter:
    def __init__(self, name: str, engine):
        self.name = name
        self.engine = engine
        self.inspector = inspect(engine)

    def has_table(self, table: str) -> bool:
        return self.inspector.has_table(table)

    def columns(self, table: str) -> list[str]:
        return [c["name"] for c in self.inspector.get_columns(table)]

    def rows(self, table: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rs = conn.execute(text(f'SELECT * FROM "{table}"'))
            return [dict(r._mapping) for r in rs]



def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Manual production data recovery tool")
    p.add_argument("--mode", choices=["dry-run", "apply"], default="dry-run")
    p.add_argument("--sqlite-path")
    p.add_argument("--source-postgres-db")
    p.add_argument("--only")
    p.add_argument("--overwrite-existing", action="store_true")
    p.add_argument("--skip-backup", action="store_true")
    return p.parse_args()


def require_apply_safety() -> str:
    if os.getenv("FLASK_ENV") != "production":
        raise RuntimeError("Refusing apply: FLASK_ENV must be production")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("Refusing apply: DATABASE_URL missing")
    if not db_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Refusing apply: DATABASE_URL must use postgresql+psycopg://")
    return db_url


def backup_database(db_url: str) -> Path:
    out_dir = Path("/var/backups/xeanvi-db")
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"recovery-before-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", str(output), db_url], check=True)
    return output


def payload_hash(row: dict[str, Any], field: str) -> str:
    v = row.get(field)
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        s = json.dumps(v, sort_keys=True)
    else:
        s = str(v)
    return hashlib.sha256(s.encode()).hexdigest()


def main() -> int:
    args = parse_args()
    selected = DEFAULT_TABLES if not args.only else [s.strip() for s in args.only.split(",") if s.strip()]

    target_url = os.getenv("DATABASE_URL")
    if not target_url:
        raise RuntimeError("DATABASE_URL is required")
    target_engine = create_engine(target_url)
    target_inspector = inspect(target_engine)

    if args.mode == "apply":
        safe_url = require_apply_safety()
        parsed = urlparse(safe_url)
        if parsed.path.lstrip("/") != "xeanvi":
            raise RuntimeError("Refusing apply: target DB in DATABASE_URL must be xeanvi")
        if not args.skip_backup:
            backup_path = backup_database(safe_url)
            print(f"Backup created at {backup_path}")

    sources: list[SourceAdapter] = []
    if args.sqlite_path:
        sources.append(SourceAdapter(f"sqlite:{args.sqlite_path}", create_engine(f"sqlite:///{args.sqlite_path}")))
    if args.source_postgres_db:
        parsed = urlparse(target_url)
        source_url = safe_source = f"{parsed.scheme}://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}/{args.source_postgres_db}"
        sources.append(SourceAdapter(f"postgres:{args.source_postgres_db}", create_engine(source_url)))
    if not sources:
        raise RuntimeError("At least one source must be provided")

    metadata = MetaData()
    reports: list[TableReport] = []

    with target_engine.begin() as conn:
        for source in sources:
            for table_name in selected + OPTIONAL_TABLES:
                if table_name in OPTIONAL_TABLES and table_name not in selected:
                    continue
                rep = TableReport(source.name, table_name)
                reports.append(rep)
                src_table = "user" if table_name == "users" and source.has_table("user") else table_name
                tgt_table = "user" if table_name == "users" and target_inspector.has_table("user") else table_name
                if not source.has_table(src_table) or not target_inspector.has_table(tgt_table):
                    rep.skip_reasons.append("missing source or target table")
                    print(f"WARN: skipping {table_name} from {source.name}: missing source or target table")
                    continue
                src_cols = set(source.columns(src_table))
                tgt_cols = [c["name"] for c in target_inspector.get_columns(tgt_table)]
                tgt_colset = set(tgt_cols)
                table = Table(tgt_table, metadata, autoload_with=target_engine)
                rows = source.rows(src_table)
                rep.source_rows = len(rows)

                if table_name == "users":
                    for row in rows:
                        email = row.get("email")
                        if blank(email):
                            rep.skipped += 1
                            rep.skip_reasons.append("user missing email")
                            continue
                        existing = conn.execute(select(table).where(table.c.email == email)).mappings().first()
                        common = {k: row.get(k) for k in tgt_cols if k in src_cols and k != "id"}
                        for ts in ("created_at", "updated_at"):
                            if ts in tgt_colset and blank(common.get(ts)):
                                common[ts] = now_utc()
                        if not existing:
                            conn.execute(table.insert().values(**common))
                            rep.inserted += 1
                        else:
                            updates = {}
                            for k, v in common.items():
                                if k == "email":
                                    continue
                                cur = existing.get(k)
                                if args.overwrite_existing:
                                    if v is not None:
                                        updates[k] = v
                                elif blank(cur) and not blank(v):
                                    updates[k] = v
                            if updates:
                                conn.execute(table.update().where(table.c.email == email).values(**updates))
                                rep.updated += 1
                            else:
                                rep.skipped += 1
                    continue

                if table_name == "scans":
                    existing_rows = conn.execute(select(table)).mappings().all()
                    existing_sigs = {
                        (r.get("created_at"), r.get("market_day"), r.get("best_symbol"), r.get("best_decision"), r.get("best_score"), payload_hash(r, "payload_json"))
                        for r in existing_rows
                    }
                    for row in rows:
                        out = {k: row.get(k) for k in tgt_cols if k in src_cols and k != "id"}
                        if "created_at" in tgt_colset and blank(out.get("created_at")):
                            out["created_at"] = now_utc()
                        sig = (out.get("created_at"), out.get("market_day"), out.get("best_symbol"), out.get("best_decision"), out.get("best_score"), payload_hash(out, "payload_json"))
                        if sig in existing_sigs:
                            rep.skipped += 1
                            continue
                        conn.execute(table.insert().values(**out))
                        existing_sigs.add(sig)
                        rep.inserted += 1
                    continue

                # generic tables
                dedupe = {}
                if table_name == "stripe_events" and "event_id" in tgt_colset:
                    dedupe = {r[0] for r in conn.execute(select(table.c.event_id)).all() if r[0]}
                if table_name == "blog_posts" and "slug" in tgt_colset:
                    dedupe = {r[0] for r in conn.execute(select(table.c.slug)).all() if r[0]}

                for row in rows:
                    out = {k: row.get(k) for k in tgt_cols if k in src_cols and k != "id"}
                    for ts in ("created_at", "updated_at"):
                        if ts in tgt_colset and blank(out.get(ts)):
                            out[ts] = now_utc()
                    if table_name == "stripe_events" and out.get("event_id") in dedupe:
                        rep.skipped += 1
                        continue
                    if table_name == "blog_posts" and out.get("slug") in dedupe:
                        rep.skipped += 1
                        continue
                    if table_name == "user_events":
                        sig = (out.get("event_name"), out.get("created_at"), out.get("user_id"), payload_hash(out, "context"))
                        if "_user_event_sigs" not in dedupe:
                            existing = conn.execute(select(table)).mappings().all()
                            dedupe["_user_event_sigs"] = {
                                (e.get("event_name"), e.get("created_at"), e.get("user_id"), payload_hash(e, "context")) for e in existing
                            }
                        if sig in dedupe["_user_event_sigs"]:
                            rep.skipped += 1
                            continue
                        dedupe["_user_event_sigs"].add(sig)
                    try:
                        conn.execute(table.insert().values(**out))
                        rep.inserted += 1
                    except Exception as exc:
                        rep.skipped += 1
                        rep.skip_reasons.append(f"incompatible row: {exc.__class__.__name__}")

        if args.mode == "dry-run":
            conn.rollback()
            print("Dry-run complete: rolled back all planned changes")

    if args.mode == "apply":
        print("Apply complete: transaction committed")

    for r in reports:
        print(f"source={r.source_name} table={r.table} source_rows={r.source_rows} inserted={r.inserted} updated={r.updated} skipped={r.skipped}")
        if r.skip_reasons:
            print(f"  skip_reasons={r.skip_reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
