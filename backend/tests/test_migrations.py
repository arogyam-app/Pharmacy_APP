"""The migrated schema, checked against the models.

Every other test builds its tables from the model metadata, so a CHECK
constraint that lists enum values by name can fall out of step with the enum
without a single test noticing — the fresh-from-metadata table is always
right, and only a database that has actually been migrated is wrong. That is
how `print_format IN ('THERMAL_80', 'A5')` survived the addition of `A4`:
the suite passed and the seed failed.

`alembic check` does not cover this: autogenerate does not compare CHECK
constraints.
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.db.base import Base
from app.db.types import uuid7

BACKEND = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory) -> sqlite3.Connection:
    """A database built the way a real one is: by running the migrations."""
    path = tmp_path_factory.mktemp("migrated") / "migrated.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{path.as_posix()}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr

    connection = sqlite3.connect(path)
    yield connection
    connection.close()


def table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    assert row, f"{table} is missing from the migrated schema"
    return row[0]


def constraint_sql(connection: sqlite3.Connection, table: str, column: str) -> str:
    """The CHECK clause guarding one column's allowed values."""
    match = re.search(
        rf"CHECK\s*\(\s*{column}\s+IN\s*\(([^)]*)\)",
        table_sql(connection, table),
        re.IGNORECASE,
    )
    assert match, f"no CHECK on {table}.{column} in the migrated schema"
    return match.group(1)


def enum_checks() -> list[tuple[str, str, tuple[str, ...]]]:
    """Every `column IN (...)` constraint the models declare.

    Read off the metadata rather than listed by hand. The hand-kept list was
    the actual bug twice over: the guard existed, the new enum was simply
    never added to it, so the very constraint that had gone stale was the one
    nothing checked. A list derived from the models cannot fall behind them.
    """
    found = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for constraint in table.constraints:
            text = str(getattr(constraint, "sqltext", "")).strip()
            match = re.match(r"(\w+) IN \((.*)\)$", text)
            if match:
                values = tuple(sorted(re.findall(r"'([^']+)'", match.group(2))))
                found.append((table_name, match.group(1), values))
    assert found, "no enum CHECK constraints found — has the metadata moved?"
    return found


@pytest.mark.parametrize(
    "table,column,expected",
    enum_checks(),
    ids=[f"{t}.{c}" for t, c, _ in enum_checks()],
)
def test_every_enum_value_is_allowed_by_the_migrated_constraint(
    migrated_db, table, column, expected
):
    allowed = constraint_sql(migrated_db, table, column)
    missing = [v for v in expected if f"'{v}'" not in allowed]
    assert not missing, (
        f"{table}.{column} rejects {missing} — the CHECK constraint was not "
        f"updated when the enum grew. Add a migration that rebuilds it. "
        f"(Alembic autogenerate does not compare CHECK constraints, so this "
        f"never appears in a generated revision.)"
    )


def test_every_not_null_column_can_actually_be_inserted(migrated_db):
    """A NOT NULL column needs a default *somewhere*, or nothing can be saved.

    The models set `created_at` and `updated_at` with `server_default`, so the
    INSERT omits them and the database is expected to fill them in. A
    migration that creates the columns NOT NULL *without* that default makes
    every insert fail with a 500 — while the suite stays green, because
    `create_all` builds from the model metadata, which carries the default.
    Only a database that got there by migration is wrong.

    Written after exactly that shipped once. It walks the whole migrated
    schema rather than any one table, so the next table added this way fails
    here instead of at the counter.
    """
    tables = [
        row[0]
        for row in migrated_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
        )
    ]
    assert tables, "no tables in the migrated schema"

    offenders = []
    for table in tables:
        for _, name, _, not_null, default, primary_key in migrated_db.execute(
            f"PRAGMA table_info({table})"
        ):
            # A primary key fills itself in; everything else that cannot be
            # NULL has to say what it should be when the INSERT leaves it out.
            if name in ("created_at", "updated_at") and not_null and default is None:
                offenders.append(f"{table}.{name}")

    assert not offenders, (
        f"NOT NULL with no default, so an insert that omits them fails: "
        f"{', '.join(offenders)}. Add server_default=CURRENT_TIMESTAMP in the migration."
    )


def test_the_letterhead_columns_exist_after_migrating(migrated_db):
    sql = table_sql(migrated_db, "store_settings")
    for column in ("logo_image", "partner_logo_image", "order_qr_image", "order_qr_caption"):
        assert column in sql, column


def test_the_catalogue_list_price_exists_after_migrating(migrated_db):
    assert "list_mrp_per_pack" in table_sql(migrated_db, "medicines")


def test_a_migrated_store_can_actually_be_billed_on_the_letterhead(migrated_db):
    """The failure this file exists for: seeding a migrated database.

    Inserting the row the seed inserts is what the stale CHECK refused.
    """
    # Keys are UUIDs and are generated in Python, so raw SQL has to supply
    # them — there is no sequence to fall back on. SQLite stores them as the
    # 32-character hex form.
    store_id = uuid7().hex
    settings_id = uuid7().hex

    migrated_db.execute(
        "INSERT INTO stores (id, name, created_at, updated_at, is_active) "
        "VALUES (?, 'Check Pharmacy', datetime('now'), datetime('now'), 1)",
        (store_id,),
    )
    migrated_db.execute(
        "INSERT INTO store_settings "
        "(id, store_id, default_discount_pct, max_discount_pct, gst_enabled, "
        " gst_registration_type, price_includes_gst, near_expiry_days, low_stock_units, "
        " order_book_decline_pct, return_window_days, credit_note_validity_days, "
        " print_format, round_off_enabled, allow_negative_stock, created_at, updated_at) "
        "VALUES (?, ?, 1000, 2000, 1, 'REGULAR', 1, 90, 10000, 6000, 7, 180, "
        " 'A4', 1, 0, datetime('now'), datetime('now'))",
        (settings_id, store_id),
    )
    stored = migrated_db.execute("SELECT print_format FROM store_settings").fetchone()
    assert stored[0] == "A4"
    migrated_db.rollback()
