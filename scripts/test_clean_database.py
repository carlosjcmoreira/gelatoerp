#!/usr/bin/env python3
"""Run the complete unittest suite against a disposable PostgreSQL database.

The regular ``test-suite`` workflow is intentionally unchanged.  This runner
is an additional regression check for tests that accidentally depend on data
created by an earlier test module or on the development database's state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql


ROOT = Path(__file__).resolve().parent.parent
TEST_COMMAND = [
    sys.executable,
    "-m",
    "unittest",
    "discover",
    "-s",
    "tests",
    "-p",
    "test*.py",
]


def _admin_url(database_url: str) -> str:
    """Return the same PostgreSQL URL, connected to the maintenance database."""

    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must be a PostgreSQL connection URL")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/postgres",
            parsed.query,
            parsed.fragment,
        )
    )


def _database_url(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database_name}",
            parsed.query,
            parsed.fragment,
        )
    )


def _create_database(admin_url: str, database_name: str) -> None:
    connection = psycopg2.connect(admin_url)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(database_name)
                )
            )
    finally:
        connection.close()


def _drop_database(admin_url: str, database_name: str) -> None:
    connection = psycopg2.connect(admin_url)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            # The test subprocess should normally have released every
            # connection, but FORCE also makes Ctrl-C/failing setup cleanup
            # deterministic on PostgreSQL 13+.
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
    finally:
        connection.close()


def _run(command: list[str], environment: dict[str, str], label: str) -> int:
    print(f"[clean-test-db] {label}", flush=True)
    completed = subprocess.run(command, cwd=ROOT, env=environment)
    return completed.returncode


def main() -> int:
    source_url = os.environ.get("DATABASE_URL")
    if not source_url:
        print(
            "[clean-test-db] DATABASE_URL is required to create the disposable "
            "PostgreSQL database.",
            file=sys.stderr,
        )
        return 2

    database_name = f"niva_clean_tests_{uuid.uuid4().hex[:20]}"
    admin_url = _admin_url(source_url)
    isolated_url = _database_url(source_url, database_name)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = isolated_url
    # The full application factory runs migrations and seeds the disposable
    # database.  Background integrations are not part of this test check.
    environment["EVENTOS_SYNC_ENABLED"] = "0"
    # This class verifies historical production data that is intentionally not
    # copied into a new empty database. Its schema-independent checks still run
    # in the standard suite against the configured working database.
    environment["SKIP_LIVE_DB_CHECKS"] = "1"

    created = False
    try:
        print(f"[clean-test-db] creating {database_name}", flush=True)
        _create_database(admin_url, database_name)
        created = True

        setup_status = _run(
            [
                sys.executable,
                "-c",
                "from flask_app.app import create_app; create_app()",
            ],
            environment,
            "initializing schema and migrations",
        )
        if setup_status:
            return setup_status

        return _run(TEST_COMMAND, environment, "running unittest discovery")
    except KeyboardInterrupt:
        print("[clean-test-db] interrupted", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"[clean-test-db] failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if created:
            try:
                print(f"[clean-test-db] dropping {database_name}", flush=True)
                _drop_database(admin_url, database_name)
            except Exception as exc:
                print(
                    f"[clean-test-db] cleanup failed for {database_name}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    raise SystemExit(main())