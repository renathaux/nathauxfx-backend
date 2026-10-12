#!/usr/bin/env python3
"""Verify stream generations against disposable PostgreSQL only.

Safety:
- Reads a Render staging URL from /tmp/flowsignal_staging_db_url by default.
- Refuses any source database name except flowsignal_phase12_staging.
- Creates a separate temporary database, runs verification there, then drops it.
- Never prints the credential or connection URL.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url


EXPECTED_SOURCE_DB = "flowsignal_phase12_staging"
EXPECTED_HEAD = "20260925_0026"
REQUIRED_TABLES = {
    "indicator_stream_generations",
    "indicator_stream_heads",
    "strategy_setup_generations",
}
REQUIRED_INDEX = "uq_generation_active"


def fail(message: str, *, cleanup=None) -> None:
    if cleanup:
        try:
            cleanup()
        except Exception:
            pass
    print(json.dumps({"verdict": "POSTGRES FAILURE — ENVIRONMENT/ACCESS", "reason": message}, indent=2))
    raise SystemExit(2)


def dsn_for_psycopg(url):
    # psycopg2 wants postgresql://, not SQLAlchemy's optional +psycopg2 suffix.
    clean = url.set(drivername="postgresql")
    return clean.render_as_string(hide_password=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--credential-file",
        default="/tmp/flowsignal_staging_db_url",
        help="File containing the Render staging External Database URL",
    )
    args = parser.parse_args()

    credential_path = Path(args.credential_file)
    if not credential_path.exists():
        fail(f"credential file missing: {credential_path}")
    raw = credential_path.read_text().strip()
    if not raw:
        fail(f"credential file is empty: {credential_path}")

    try:
        base_url = make_url(raw)
    except Exception as exc:
        fail(f"credential is not a valid SQLAlchemy/PostgreSQL URL: {exc}")

    if base_url.database != EXPECTED_SOURCE_DB:
        fail(
            f"refusing source database {base_url.database!r}; expected {EXPECTED_SOURCE_DB!r}"
        )
    if not base_url.drivername.startswith("postgresql"):
        fail(f"refusing non-PostgreSQL URL: {base_url.drivername}")

    temp_db = f"stream_generation_pg_verify_{os.getpid()}_{secrets.token_hex(3)}"
    temp_url = base_url.set(database=temp_db)
    base_dsn = dsn_for_psycopg(base_url)
    temp_dsn = dsn_for_psycopg(temp_url)

    repo_root = Path(__file__).resolve().parents[2]
    backend = repo_root / "Backend"
    env = dict(os.environ)
    env["DATABASE_URL"] = temp_url.render_as_string(hide_password=False)
    env["STREAM_GENERATION_POSTGRES_TEST_URL"] = env["DATABASE_URL"]
    env["PYTHONPATH"] = str(backend)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    created = False

    def drop_temp():
        nonlocal created
        if not created:
            return
        conn = psycopg2.connect(base_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (temp_db,),
                )
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(temp_db)))
        finally:
            conn.close()
        created = False

    report = {
        "source_instance": "flowsignal-phase12-staging",
        "source_database": EXPECTED_SOURCE_DB,
        "temporary_database": temp_db,
        "production_touched": False,
        "clean_migration": None,
        "postgres_runtime_tests": None,
        "cleanup": None,
    }

    try:
        conn = psycopg2.connect(base_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(temp_db)))
        finally:
            conn.close()
        created = True

        print(f"[1/4] Created isolated PostgreSQL database: {temp_db}")
        print("[2/4] Running clean Alembic migration to head...")
        migrate = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(backend / "alembic.ini"),
                "upgrade",
                "head",
            ],
            cwd=repo_root,
            env=env,
            text=True,
        )
        if migrate.returncode != 0:
            report["clean_migration"] = "FAIL"
            print(json.dumps(report, indent=2))
            return 3

        conn = psycopg2.connect(temp_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version")
                revision = cur.fetchone()[0]
                cur.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename = ANY(%s)",
                    (list(REQUIRED_TABLES),),
                )
                tables = {row[0] for row in cur.fetchall()}
                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname=%s",
                    (REQUIRED_INDEX,),
                )
                index_present = cur.fetchone() is not None
        finally:
            conn.close()

        if revision != EXPECTED_HEAD:
            raise RuntimeError(f"unexpected Alembic head {revision}; expected {EXPECTED_HEAD}")
        if tables != REQUIRED_TABLES:
            raise RuntimeError(f"missing generation tables: {sorted(REQUIRED_TABLES - tables)}")
        if not index_present:
            raise RuntimeError(f"missing PostgreSQL partial unique index {REQUIRED_INDEX}")

        report["clean_migration"] = {
            "result": "PASS",
            "revision": revision,
            "tables": sorted(tables),
            "active_unique_index": True,
        }

        print("[3/4] Running stream-generation suite on real PostgreSQL...")
        tests = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "Backend/tests/test_stream_generations.py",
                "-q",
            ],
            cwd=repo_root,
            env=env,
            text=True,
        )
        report["postgres_runtime_tests"] = "PASS" if tests.returncode == 0 else "FAIL"
        if tests.returncode != 0:
            print(json.dumps(report, indent=2))
            return 4

        print("[4/4] PostgreSQL verification passed.")
        report["verdict"] = "POSTGRES VERIFIED — READY FOR PRODUCTION SCHEMA DEPLOYMENT"
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        report["verdict"] = "POSTGRES FAILURE — CODE CHANGE REQUIRED"
        report["error_type"] = type(exc).__name__
        report["reason"] = str(exc)
        print(json.dumps(report, indent=2))
        return 5
    finally:
        try:
            drop_temp()
            report["cleanup"] = "TEMP DB DELETED"
            print(f"Cleanup: deleted temporary database {temp_db}")
        except Exception as exc:
            print(f"WARNING: temporary database cleanup failed: {type(exc).__name__}: {exc}")
        try:
            credential_path.unlink(missing_ok=True)
            print(f"Cleanup: deleted credential file {credential_path}")
        except Exception as exc:
            print(f"WARNING: credential-file cleanup failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
