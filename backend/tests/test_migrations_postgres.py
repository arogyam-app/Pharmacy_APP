"""The migration chain, run against a real PostgreSQL server.

`test_migrations.py` proves the chain works on SQLite. That is not the same
claim. Alembic rewrites whole tables on SQLite (`render_as_batch`) and issues
real `ALTER TABLE` on PostgreSQL, so a migration can be correct on one and
impossible on the other — and every existing test builds its schema from model
metadata, which is right by construction and therefore proves nothing about a
database that got where it is by migrating.

The specific thing this catches: revisions 0010, 0017 and 0018 drop CHECK
constraints by the bare name `print_format_valid`, while 0001 created them
under the naming convention as `ck_store_settings_print_format_valid`. Whether
that works depends on Alembic applying `NAMING_CONVENTION` to the bare name at
DROP time. On SQLite the table is rebuilt and the question never comes up. On
PostgreSQL, if the convention is not applied, `alembic upgrade head` fails on
a fresh database with "constraint does not exist" — which would mean the app
cannot be deployed at all, while the whole suite stays green.

Skipped unless a PostgreSQL server is reachable, so it costs nothing on a dev
machine. Inside the deployment stack it runs against the compose database:

    docker compose exec api pytest tests/test_migrations_postgres.py -v

It creates a scratch database, migrates it, inspects it, and drops it. The
live data is never touched.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

from app.models.enums import (
    DosageForm,
    ExpiryBasis,
    GstRegistrationType,
    ItemType,
    PackUnit,
    PrintFormat,
    ScheduleType,
    values,
)

BACKEND = Path(__file__).resolve().parent.parent
SCRATCH_DB = "pharmacy_migration_check"

LIVE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not LIVE_URL.startswith("postgresql"),
    reason="No PostgreSQL DATABASE_URL — this suite only means anything on Postgres.",
)


def _swap_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@pytest.fixture(scope="module")
def migrated_pg():
    """A scratch PostgreSQL database with every migration applied to it."""
    admin_url = _swap_database(LIVE_URL, "postgres")
    scratch_url = _swap_database(LIVE_URL, SCRATCH_DB)

    # CREATE/DROP DATABASE cannot run inside a transaction.
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": scratch_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "alembic upgrade head FAILED on PostgreSQL. The app cannot be deployed "
        f"until this passes.\n\nSTDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
    )

    engine = create_engine(scratch_url)
    yield engine
    engine.dispose()

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
    admin.dispose()


def check_clause(engine, table: str, column: str) -> str:
    """The text of the CHECK constraint guarding one column's values."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = :table AND c.contype = 'c'
                """
            ),
            {"table": table},
        ).scalars().all()

    for clause in rows:
        if column in clause and " IN " in clause.upper():
            return clause
    raise AssertionError(f"no value CHECK on {table}.{column} in the migrated schema")


@pytest.mark.parametrize(
    "table,column,enum",
    [
        ("store_settings", "print_format", PrintFormat),
        ("store_settings", "gst_registration_type", GstRegistrationType),
        ("medicines", "schedule_type", ScheduleType),
        ("medicines", "dosage_form", DosageForm),
        ("medicines", "pack_unit", PackUnit),
        ("medicines", "item_type", ItemType),
        ("batches", "pack_unit", PackUnit),
        ("batches", "expiry_basis", ExpiryBasis),
    ],
)
def test_every_enum_value_is_allowed_by_the_migrated_constraint(
    migrated_pg, table, column, enum
):
    clause = check_clause(migrated_pg, table, column)
    missing = [v for v in values(enum) if f"'{v}'" not in clause]
    assert not missing, (
        f"{table}.{column} rejects {missing} on PostgreSQL — the CHECK was not "
        f"rebuilt when the enum grew. Add a migration.\nConstraint: {clause}"
    )


def test_every_not_null_timestamp_has_a_default(migrated_pg):
    """A NOT NULL column with no default makes every insert fail with a 500.

    The models set `created_at`/`updated_at` via `server_default`, so the
    INSERT omits them. A migration that creates them NOT NULL without that
    default is invisible to `create_all`-based tests and fatal in production.
    """
    with migrated_pg.connect() as conn:
        offenders = conn.execute(
            text(
                """
                SELECT table_name || '.' || column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND column_name IN ('created_at', 'updated_at')
                  AND is_nullable = 'NO'
                  AND column_default IS NULL
                ORDER BY 1
                """
            )
        ).scalars().all()

    assert not offenders, (
        "NOT NULL with no default, so an insert that omits them fails: "
        f"{', '.join(offenders)}. Add server_default=sa.func.now() in the migration."
    )


def test_the_seed_row_can_actually_be_inserted(migrated_pg):
    """The failure this file exists for, in its original form: seeding.

    A stale CHECK refuses the settings row and the app is unusable from the
    first minute, having passed every test on the way in.
    """
    with migrated_pg.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stores (id, name, created_at, updated_at, is_active) "
                "VALUES (1, 'Check Pharmacy', now(), now(), true)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO store_settings "
                "(store_id, default_discount_pct, max_discount_pct, gst_enabled, "
                " gst_registration_type, price_includes_gst, near_expiry_days, "
                " low_stock_units, order_book_decline_pct, return_window_days, "
                " credit_note_validity_days, print_format, round_off_enabled, "
                " allow_negative_stock, created_at, updated_at) "
                "VALUES (1, 10, 20, true, 'REGULAR', true, 90, 100, 60, 7, 180, "
                " 'A5_NEW', true, false, now(), now())"
            )
        )
        stored = conn.execute(text("SELECT print_format FROM store_settings")).scalar()
        assert stored == "A5_NEW"
        conn.rollback()


def test_the_chain_ends_where_the_files_say_it_does(migrated_pg):
    """Guards against a migration that exists but never ran."""
    with migrated_pg.connect() as conn:
        applied = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()

    revisions = sorted(
        p.name.split("_")[0]
        for p in (BACKEND / "alembic" / "versions").glob("[0-9]*.py")
    )
    assert applied == revisions[-1], (
        f"the database is at {applied}, but the newest revision on disk is {revisions[-1]}"
    )
