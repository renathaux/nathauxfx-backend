#!/usr/bin/env python3
"""One-time, idempotent migration of FlowSignal's SQLite rows to Neon.

The script never prints connection strings, passwords, password hashes, or
setting values. Run it from Backend after `alembic upgrade head` support is
installed through requirements.txt.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db import normalize_database_url, redact_database_url
from models import Base


BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_URL = f"sqlite:///{BACKEND_DIR / 'database' / 'flowsignal.db'}"
TABLE_ORDER = (
    "users",
    "runtime_settings",
    "news_trading_mode_audit",
    "auto_trade_state_audit",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy FlowSignal SQLite data into a migrated Neon database."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("SOURCE_SQLITE_URL", DEFAULT_SQLITE_URL),
        help="Source SQLite SQLAlchemy URL (defaults to Backend/database/flowsignal.db).",
    )
    parser.add_argument(
        "--target",
        default=os.getenv("MIGRATION_DATABASE_URL") or os.getenv("DATABASE_URL"),
        help="Target Neon URL (defaults to MIGRATION_DATABASE_URL, then DATABASE_URL).",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Keep existing target rows and insert only non-conflicting source rows.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation for the write operation.",
    )
    return parser.parse_args()


def validate_urls(source_url, target_url):
    source = normalize_database_url(source_url)
    target = normalize_database_url(target_url) if target_url else ""
    if not source.startswith("sqlite"):
        raise SystemExit("Source must be a SQLite SQLAlchemy URL.")
    if not target.startswith("postgresql"):
        raise SystemExit(
            "Target must be a Neon PostgreSQL URL supplied through --target or DATABASE_URL."
        )
    return source, target


def run_schema_migrations(target_url):
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = target_url
    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


def destination_counts(connection):
    return {
        name: connection.execute(
            select(func.count()).select_from(Base.metadata.tables[name])
        ).scalar_one()
        for name in TABLE_ORDER
    }


def reset_postgres_sequences(connection):
    for table_name in (
        "users",
        "news_trading_mode_audit",
        "auto_trade_state_audit",
    ):
        connection.execute(text("""
            SELECT setval(
                pg_get_serial_sequence(:table_name, 'id'),
                COALESCE((SELECT MAX(id) FROM %s), 1),
                EXISTS(SELECT 1 FROM %s)
            )
        """ % (table_name, table_name)), {"table_name": table_name})


def migrate(source_url, target_url, merge=False):
    source_engine = create_engine(source_url)
    target_engine = create_engine(target_url, pool_pre_ping=True)
    source_metadata = MetaData()
    source_metadata.reflect(bind=source_engine, only=list(TABLE_ORDER))

    missing = [name for name in TABLE_ORDER if name not in source_metadata.tables]
    if missing:
        raise SystemExit(f"Source SQLite database is missing tables: {', '.join(missing)}")

    with target_engine.begin() as target_connection:
        counts_before = destination_counts(target_connection)
        if not merge and any(counts_before.values()):
            details = ", ".join(
                f"{name}={count}" for name, count in counts_before.items() if count
            )
            raise SystemExit(
                "Target is not empty. Re-run with --merge only after reviewing it. "
                f"Existing row counts: {details}"
            )

        copied = {}
        with source_engine.connect() as source_connection:
            for table_name in TABLE_ORDER:
                rows = [dict(row._mapping) for row in source_connection.execute(
                    select(source_metadata.tables[table_name])
                )]
                copied[table_name] = len(rows)
                if not rows:
                    continue
                target_table = Base.metadata.tables[table_name]
                statement = pg_insert(target_table).values(rows)
                if merge:
                    statement = statement.on_conflict_do_nothing()
                target_connection.execute(statement)

        reset_postgres_sequences(target_connection)
        counts_after = destination_counts(target_connection)

    source_engine.dispose()
    target_engine.dispose()
    return copied, counts_after


def main():
    args = parse_args()
    source_url, target_url = validate_urls(args.source, args.target)
    print("Source:", redact_database_url(source_url))
    print("Target:", redact_database_url(target_url))
    if not args.yes:
        raise SystemExit("No data written. Re-run with --yes after verifying source and target.")

    run_schema_migrations(target_url)
    copied, counts = migrate(source_url, target_url, merge=args.merge)
    print("Migration complete. Copied row counts:", copied)
    print("Neon row counts:", counts)


if __name__ == "__main__":
    main()
