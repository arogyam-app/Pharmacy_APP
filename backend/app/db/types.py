"""Portable exact-decimal column types.

SQLite has no DECIMAL: SQLAlchemy's plain ``Numeric`` round-trips through
float there, which silently loses paise. We store scaled integers instead
(``12.34`` -> ``1234``), so ordering, comparison and ``SUM()`` stay correct in
SQL, and Python always gets a ``Decimal`` back. On PostgreSQL the column is a
real ``NUMERIC`` and none of this applies.

Never use raw floats for money or quantity — see CLAUDE.md rule 1.

CAUTION — CHECK constraints: because SQLite stores these scaled, a constraint
comparing a PreciseDecimal column to a *literal* means different things on the
two dialects (``discount_pct <= 100`` reads as 1.00 on SQLite). Compare two
same-scale columns instead, and put value ranges in the Pydantic schema.
"""

import os
import threading
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import BigInteger, Numeric, Uuid
from sqlalchemy.types import TypeDecorator


#: Guards the monotonic counter below. Ids are minted from request threads.
_uuid7_lock = threading.Lock()
_uuid7_last_ms = 0
_uuid7_seq = 0

#: 12 bits of counter (RFC 9562 "monotonic random", method 2) — 4096 ids per
#: millisecond per process, which is far more than a counter can issue.
_UUID7_SEQ_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """A time-ordered UUID (RFC 9562 version 7), monotonic within a process.

    Version 4 would do the job of never colliding, and would quietly undo the
    indexing work: a random key lands in a random leaf of the btree on every
    insert, so the index stops fitting in cache and writes start touching the
    whole tree. Version 7 puts a millisecond timestamp in the high bits, so
    consecutive inserts land next to each other exactly as an autoincrementing
    integer did — the property that made integer keys fast, kept.

    **The counter is not decoration.** A bare v7 only orders to the
    millisecond, and rows written inside one millisecond then sort by their
    random tail — which is to say, arbitrarily. Plenty of code here reads
    `ORDER BY id` as insertion order: the lines of a purchase invoice, the
    payments on a bill, the rows of an audit trail. Under a bare v7 those
    shuffle occasionally and only under load, which is the worst way to find
    out. The 12-bit counter makes ids issued in the same millisecond strictly
    increasing, so insertion order survives.

    That guarantee is per process. Two uvicorn workers can interleave within a
    millisecond — but every ordering that matters here is over rows written by
    one request, in one transaction, on one worker.

    Generated here rather than by the database: PostgreSQL grew a native
    `uuidv7()` only in v18, and SQLite has none. One implementation in Python
    keeps dev and production issuing the same shape of key.
    """
    global _uuid7_last_ms, _uuid7_seq

    with _uuid7_lock:
        ms = int(time.time() * 1000)
        if ms > _uuid7_last_ms:
            _uuid7_last_ms, _uuid7_seq = ms, 0
        elif _uuid7_seq < _UUID7_SEQ_MAX:
            _uuid7_seq += 1
        else:
            # 4096 in one millisecond. Step into the next one rather than
            # wrap the counter and hand back a key that sorts backwards.
            _uuid7_last_ms += 1
            _uuid7_seq = 0
        ms, seq = _uuid7_last_ms, _uuid7_seq

    raw = bytearray(16)
    raw[0:6] = ms.to_bytes(6, "big")
    raw[6] = 0x70 | ((seq >> 8) & 0x0F)   # version 7 + counter high nibble
    raw[7] = seq & 0xFF                   # counter low byte
    raw[8:16] = os.urandom(8)
    raw[8] = (raw[8] & 0x3F) | 0x80       # RFC 9562 variant
    return uuid.UUID(bytes=bytes(raw))


class _UUIDColumn(TypeDecorator):
    """``Uuid``, but it also accepts the string form of one.

    An id makes a round trip through JSON on the way to the browser and back,
    and comes home as ``"019fd852-963d-7315-af7e-28d7dd8a007e"``. Pydantic
    parses it at the API boundary, but plenty of code — reports, the seed, a
    filter built straight from a response body — hands the string to a query
    instead, and the raw type fails that with ``'str' object has no attribute
    'hex'``, three frames inside SQLAlchemy and nowhere near the cause.

    Coercing here costs one isinstance check per bind and removes the whole
    class of failure. A string that is not a UUID still raises, which is
    right: that is a bug, not a format.
    """

    impl = Uuid
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(as_uuid=True)

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


def UUIDPK():
    """Primary-key column type: native ``uuid`` on PostgreSQL, ``CHAR(32)`` on SQLite.

    Keys are UUIDs rather than sequence numbers because the www app will write
    to Supabase while the counter keeps writing here. Two databases handing out
    `id = 4831` for different rows is not a conflict either of them can detect
    — it is silent corruption at the next sync. A UUID needs no coordination.

    This is *not* how invoice numbers work, and must not become how they work:
    `invoice_no` stays a consecutive series from `services.sequences`, because
    CGST Rule 46(b) requires exactly that and a customer needs to be able to
    read it out over the phone.
    """
    return _UUIDColumn()


class PreciseDecimal(TypeDecorator):
    """``NUMERIC(precision, scale)`` everywhere, scaled ``BIGINT`` on SQLite."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 12, scale: int = 2, **kwargs) -> None:
        self.dec_precision = precision
        self.dec_scale = scale
        self.factor = 10**scale
        self.quantum = Decimal(1).scaleb(-scale)
        super().__init__(precision=precision, scale=scale, asdecimal=True, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(
            Numeric(precision=self.dec_precision, scale=self.dec_scale, asdecimal=True)
        )

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        dec = Decimal(str(value)).quantize(self.quantum, rounding=ROUND_HALF_UP)
        if dialect.name == "sqlite":
            return int(dec * self.factor)
        return dec

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "sqlite":
            return (Decimal(int(value)) / self.factor).quantize(self.quantum)
        return Decimal(value).quantize(self.quantum)


# Canonical column flavours — use these, not PreciseDecimal directly.
def Money() -> PreciseDecimal:
    """Amounts: totals, line values, tax. NUMERIC(12,2)."""
    return PreciseDecimal(12, 2)


def UnitPrice() -> PreciseDecimal:
    """Per-unit prices (MRP/cost of one tablet). NUMERIC(12,4)."""
    return PreciseDecimal(12, 4)


def Quantity() -> PreciseDecimal:
    """Stock quantities in smallest sellable units. NUMERIC(12,3)."""
    return PreciseDecimal(12, 3)


def Rate() -> PreciseDecimal:
    """Percentages: GST rate, discount %. NUMERIC(6,2)."""
    return PreciseDecimal(6, 2)


ZERO_MONEY = Decimal("0.00")
ZERO_QTY = Decimal("0.000")


def quantize_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_qty(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_unit_price(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
