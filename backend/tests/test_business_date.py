"""What "today" means to the shop.

These tests pin the behaviour that was wrong: business dates were taken from
UTC, so every night between 00:00 and 05:30 IST the app dated an invoice to
the previous day, treated a batch expiring today as still sellable, and
measured return windows a day early.

The bug was invisible outside that window, which is why it survived — the
suite was green every morning. So none of these tests may depend on the hour
they are run at: each one *puts* the clock in the window rather than waiting
for it.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.utils import clock
from app.utils.clock import IST, business_date, store_now, to_business_date


class FrozenDatetime(datetime):
    """A `datetime` whose `now()` is wherever a test puts it."""

    frozen = datetime(2026, 8, 1, 19, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen.replace(tzinfo=None)


@pytest.fixture
def at_half_past_midnight(monkeypatch):
    """19:30 UTC on 1 August is 01:00 IST on 2 August — inside the window."""
    monkeypatch.setattr(clock, "datetime", FrozenDatetime)
    yield FrozenDatetime.frozen


# --- the helper -----------------------------------------------------------


def test_the_shop_date_is_the_one_at_the_counter(at_half_past_midnight):
    assert at_half_past_midnight.date() == date(2026, 8, 1)   # UTC still says the 1st
    assert business_date() == date(2026, 8, 2)                # the shop says the 2nd


def test_the_clock_reads_ist(at_half_past_midnight):
    now = store_now()
    assert (now.hour, now.minute) == (1, 0)
    assert now.utcoffset() == timedelta(hours=5, minutes=30)


def test_a_stored_instant_maps_to_the_day_it_was_rung_up():
    """A sale timestamped 19:30 UTC happened at 01:00 the next day."""
    assert to_business_date(datetime(2026, 8, 1, 19, 30, tzinfo=timezone.utc)) == date(2026, 8, 2)
    # And one from the middle of the working day stays put.
    assert to_business_date(datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)) == date(2026, 8, 1)


def test_a_naive_instant_is_read_as_utc():
    assert to_business_date(datetime(2026, 8, 1, 19, 30)) == date(2026, 8, 2)


def test_the_two_agree_all_day():
    """Whatever the hour, `business_date()` is the date `store_now()` shows."""
    assert business_date() == store_now().date()


def test_the_offset_is_fixed_so_there_is_no_daylight_saving_to_get_wrong():
    """India has never observed DST, so +05:30 holds for every date."""
    winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).astimezone(IST)
    summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).astimezone(IST)
    assert winter.utcoffset() == summer.utcoffset() == timedelta(hours=5, minutes=30)


def test_the_clock_needs_no_timezone_database():
    """A missing `tzdata` package must not be able to mis-date a bill."""
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


# --- what it protects -----------------------------------------------------


def test_an_invoice_carries_the_date_the_shop_would_write(client, admin_headers, monkeypatch):
    """The defect itself: a sale just after midnight was dated to yesterday."""
    from app.services import sales as sales_service

    monkeypatch.setattr(clock, "datetime", FrozenDatetime)
    monkeypatch.setattr(sales_service, "datetime", FrozenDatetime)

    from tests.conftest import sell, stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="After Midnight")
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "1", "discount_pct": "0"}],
    )

    assert sale["invoice_date"] == "2026-08-02"          # the shop's day
    assert sale["invoice_datetime"].startswith("2026-08-01T19:30")   # the UTC instant


def test_the_invoice_date_and_its_timestamp_never_disagree(client, admin_headers):
    """Both come from one instant, so they cannot straddle midnight."""
    from tests.conftest import sell, stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="One Instant")
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "1", "discount_pct": "0"}],
    )
    stamped = datetime.fromisoformat(sale["invoice_datetime"])
    assert sale["invoice_date"] == to_business_date(stamped).isoformat()


def test_yesterdays_stock_stops_selling_at_midnight_not_at_half_past_five(
    client, admin_headers, monkeypatch
):
    """Stock is sellable *through* its expiry date, and not a minute past.

    A batch dated 1 August is finished when the shop opens its books on the
    2nd. Dating from UTC kept it sellable until 05:30 IST — five and a half
    hours of selling out-of-date medicine.
    """
    from tests.conftest import API, make_batch, make_medicine

    medicine = make_medicine(client, admin_headers, brand_name="Expired Overnight")
    make_batch(
        client, admin_headers, medicine["id"], batch_no="YESTERDAY",
        expiry_date="2026-08-01",
    )

    # 19:30 UTC on the 1st — the shop is already into the 2nd.
    monkeypatch.setattr(clock, "datetime", FrozenDatetime)
    assert business_date() == date(2026, 8, 2)

    quote = client.post(
        f"{API}/pos/quote",
        json={"items": [{"medicine_id": medicine["id"], "qty_units": "1"}]},
        headers=admin_headers,
    )
    assert quote.status_code == 400, quote.text
    assert quote.json()["detail"]["code"] in ("OUT_OF_STOCK", "INSUFFICIENT_STOCK")


def test_stock_expiring_today_is_still_sellable_today(client, admin_headers, monkeypatch):
    """The other half of the same rule — this is not a fix for that."""
    from tests.conftest import API, make_batch, make_medicine

    medicine = make_medicine(client, admin_headers, brand_name="Expires Today")
    make_batch(
        client, admin_headers, medicine["id"], batch_no="TODAY",
        expiry_date="2026-08-02",
    )
    monkeypatch.setattr(clock, "datetime", FrozenDatetime)

    quote = client.post(
        f"{API}/pos/quote",
        json={"items": [{"medicine_id": medicine["id"], "qty_units": "1"}]},
        headers=admin_headers,
    )
    assert quote.status_code == 200, quote.text


# --- timestamps on the wire ----------------------------------------------


def test_a_timestamp_says_which_offset_it_is_in(client, admin_headers):
    """SQLite stores no offset, so the API has to add it back.

    A naive ISO string is read by a browser as *local* time, which put every
    time on screen 5½ hours out — and, just after midnight, on the wrong day.
    """
    from tests.conftest import sell, stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Timestamped")
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "1", "discount_pct": "0"}],
    )

    stamped = datetime.fromisoformat(sale["invoice_datetime"])
    assert stamped.tzinfo is not None, sale["invoice_datetime"]
    assert stamped.utcoffset() == timedelta(0)


def test_the_date_on_the_bill_is_that_timestamps_own_shop_day(client, admin_headers):
    from tests.conftest import sell, stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Same Instant")
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "1", "discount_pct": "0"}],
    )
    stamped = datetime.fromisoformat(sale["invoice_datetime"])
    assert sale["invoice_date"] == stamped.astimezone(IST).date().isoformat()


def test_every_timestamp_the_register_returns_is_offset_aware(client, admin_headers):
    from tests.conftest import API, sell, stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Register Stamp")
    sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "1", "discount_pct": "0"}],
    )
    rows = client.get(f"{API}/sales", headers=admin_headers).json()["items"]
    for row in rows:
        assert datetime.fromisoformat(row["invoice_datetime"]).tzinfo is not None
