"""What "today" means to the shop.

Instants are stored in UTC — that part was always right. A **business date**
is a different thing: the date on an invoice, the day a batch goes out of
date, the window a return falls inside. That is the date at the counter, and
the counter is in India, 5½ hours ahead of UTC.

Dating those from UTC was wrong every night between 00:00 and 05:30 IST. A
sale rung up at half past midnight was invoiced to the previous day, so it
fell into the wrong day-book and, on the first of a month, the wrong GST
period; a batch that expired yesterday stayed sellable for another five and
a half hours; return windows and credit-note expiry were measured a day
early.

Two rules follow, and they are the whole module:

* a **timestamp** (`created_at`, `used_at`, `printed_at`) stays UTC — use
  ``datetime.now(timezone.utc)`` as before;
* a **date** that a person would recognise as "today" comes from here.

The offset is fixed rather than looked up in a timezone database. India has
never observed daylight saving, so +05:30 is exact for every date past and
future — which means no `tzdata` package to install, and nothing that can
fail to load and quietly leave a bill dated wrong.
"""

from datetime import date, datetime, timedelta, timezone

#: The counter's clock. One shop, one country, no DST.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def store_now() -> datetime:
    """Now, as the clock on the shop wall reads it."""
    return datetime.now(IST)


def business_date() -> date:
    """Today, as the shop means it.

    This is the only thing that should date an invoice, decide whether a
    batch has expired, or measure a return window.
    """
    return store_now().date()


def as_utc(moment: datetime) -> datetime:
    """A stored instant, made explicit about being UTC.

    SQLite has no offset in its timestamps, so SQLAlchemy hands back naive
    datetimes even from a ``DateTime(timezone=True)`` column. They *are*
    UTC — everything is written that way — but nothing downstream can tell.
    """
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def utc_iso(moment: datetime | None) -> str | None:
    """An ISO-8601 string that says which offset it is in.

    Without this the API emits `2026-08-02T19:26:00`, and a browser reads a
    naive string as *local* time — putting every timestamp on screen 5½
    hours out and, just after midnight, on the wrong day.
    """
    return None if moment is None else as_utc(moment).isoformat()


def business_day_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    """The UTC instants that bracket a run of business days, end exclusive.

    Filtering a UTC `created_at` column by a business date needs this: the
    day that starts at midnight *at the counter* began at 18:30 UTC the
    evening before. Comparing the column against the bare dates would drop
    everything rung up before 05:30 on the last day and pull in the previous
    evening's work on the first.
    """
    start = datetime.combine(first, datetime.min.time(), tzinfo=IST)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=IST)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def to_business_date(moment: datetime) -> date:
    """The business date a stored UTC instant belongs to.

    A sale timestamped 19:30 UTC happened at 01:00 the next day at the
    counter, and belongs on that day's takings.
    """
    return as_utc(moment).astimezone(IST).date()
