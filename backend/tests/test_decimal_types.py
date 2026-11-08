"""Guards CLAUDE.md rule 1: money never round-trips through float."""

from decimal import Decimal

from sqlalchemy import Column, Integer, MetaData, Table, func, select

from app.db.session import engine
from app.db.types import UUIDPK, Money, Quantity, Rate, uuid7

_metadata = MetaData()
_probe = Table(
    "decimal_probe",
    _metadata,
    Column("id", UUIDPK(), primary_key=True, default=uuid7),
    # Explicit, because a uuid7 key orders by *time* and these four rows
    # are written inside the same millisecond.
    Column("seq", Integer, nullable=False),
    Column("amount", Money()),
    Column("qty", Quantity()),
    Column("rate", Rate()),
)


def _rows():
    # 0.1 + 0.2 and 8.61 are the classic float-drift cases.
    return [
        {"seq": 1, "amount": Decimal("0.10"), "qty": Decimal("0.500"), "rate": Decimal("10.00")},
        {"seq": 2, "amount": Decimal("0.20"), "qty": Decimal("1.250"), "rate": Decimal("12.50")},
        {"seq": 3, "amount": Decimal("8.61"), "qty": Decimal("10.000"), "rate": Decimal("18.00")},
        {"seq": 4, "amount": Decimal("-3.33"), "qty": Decimal("-2.750"), "rate": Decimal("0.00")},
    ]


def test_decimal_round_trip_and_sql_sum_are_exact():
    _metadata.create_all(bind=engine)
    try:
        with engine.begin() as conn:
            conn.execute(_probe.delete())
            conn.execute(_probe.insert(), _rows())

            stored = conn.execute(select(_probe.c.amount).order_by(_probe.c.seq)).scalars().all()
            assert stored == [Decimal("0.10"), Decimal("0.20"), Decimal("8.61"), Decimal("-3.33")]
            assert all(isinstance(v, Decimal) for v in stored)

            # SUM() must stay exact in SQL, including the negative row.
            total = conn.execute(select(func.sum(_probe.c.amount))).scalar_one()
            assert total == Decimal("5.58")

            # Ordering must survive the scaled-integer storage, negatives first.
            ordered = conn.execute(
                select(_probe.c.amount).order_by(_probe.c.amount)
            ).scalars().all()
            assert ordered == sorted(ordered)
            assert ordered[0] == Decimal("-3.33")

            # Range comparison in SQL, not in Python.
            above = conn.execute(
                select(func.count()).select_from(_probe).where(_probe.c.amount > Decimal("0.15"))
            ).scalar_one()
            assert above == 2
    finally:
        _metadata.drop_all(bind=engine)


def test_scales_are_preserved():
    _metadata.create_all(bind=engine)
    try:
        with engine.begin() as conn:
            conn.execute(_probe.delete())
            conn.execute(
                _probe.insert(),
                [
                    {
                        "seq": 1,
                        "amount": Decimal("1.005"),
                        "qty": Decimal("0.3335"),
                        "rate": Decimal("9.999"),
                    }
                ],
            )
            row = conn.execute(select(_probe)).one()
            # Half-up rounding at each column's declared scale.
            assert row.amount == Decimal("1.01")
            assert row.qty == Decimal("0.334")
            assert row.rate == Decimal("10.00")
    finally:
        _metadata.drop_all(bind=engine)


# --- keys -----------------------------------------------------------------


def test_uuid7_keys_are_strictly_increasing():
    """`ORDER BY id` has to keep meaning insertion order.

    Autoincrement gave that for free. A bare UUIDv7 only orders to the
    millisecond, so rows written inside one millisecond would sort by their
    random tail — and the lines of an invoice, the payments on a bill and the
    rows of an audit trail all read `ORDER BY id` as the order they were
    written in. Those would shuffle occasionally, under load, in production.

    The counter in `uuid7` is what prevents it. This test is the guard.
    """
    from itertools import pairwise

    from app.db.types import uuid7

    # Far more than one millisecond's worth, so the counter path is exercised
    # rather than just the clock advancing between calls.
    keys = [uuid7() for _ in range(20_000)]

    assert len(set(keys)) == len(keys), "duplicate key"
    assert all(a < b for a, b in pairwise(keys)), "keys are not strictly increasing"
    assert all(k.version == 7 for k in keys)


def test_uuid7_keys_are_unique_across_threads():
    """Two workers minting ids at the same moment must not collide."""
    from concurrent.futures import ThreadPoolExecutor

    from app.db.types import uuid7

    with ThreadPoolExecutor(16) as pool:
        keys = list(pool.map(lambda _: uuid7(), range(20_000)))

    assert len(set(keys)) == len(keys)
