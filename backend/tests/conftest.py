"""Shared pytest fixtures.

Tests run against a throwaway SQLite file so the dev database is never
touched. DATABASE_URL must be set before any app module is imported.
"""

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("ENVIRONMENT", "test")
_TEST_DB = Path(tempfile.gettempdir()) / "pharmacy_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.rate_limit import login_limiter  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit import DocumentSequence  # noqa: E402
from app.models.enums import DocumentType, UserRole  # noqa: E402
from app.models.store import Store, StoreSettings  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.sequences import starting_number  # noqa: E402
from app.utils.clock import business_date

API = settings.api_v1_prefix

#: A well-formed id that was never issued, for "not found" tests.
#:
#: Keys are UUIDs now, so the old `999999` no longer means "a row that is not
#: there" — it means "not an id at all", and the API rejects it at validation
#: with a 422 before any lookup happens. That tests the parser, not the
#: handler. This is a real v7 UUID with a zero timestamp, so it is accepted,
#: looked up, and genuinely absent.
MISSING_ID = "00000000-0000-7000-8000-000000000000"

ADMIN_EMAIL = "admin@testpharmacy.in"
ADMIN_PASSWORD = "Admin@1234"
SALES_EMAIL = "sales@testpharmacy.in"
SALES_PASSWORD = "Sales@1234"


@pytest.fixture(autouse=True)
def fresh_database():
    """Every test starts from an empty schema — no cross-test bleed."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    login_limiter._hits.clear()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def store(db) -> Store:
    row = Store(name="Test Pharmacy", state_code="18")
    db.add(row)
    db.flush()
    db.add(StoreSettings(store_id=row.id))
    for doc_type in DocumentType:
        # Through the same helper the seeder uses, so a test store's
        # numbering matches a real one — the sale series opens at
        # settings.invoice_number_start, the rest at 1.
        db.add(
            DocumentSequence(
                store_id=row.id,
                doc_type=doc_type.value,
                next_number=starting_number(doc_type),
            )
        )
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def admin(db, store) -> User:
    return _make_user(db, store, "Test Admin", ADMIN_EMAIL, ADMIN_PASSWORD, UserRole.ADMIN)


@pytest.fixture
def sales(db, store) -> User:
    return _make_user(db, store, "Test Sales", SALES_EMAIL, SALES_PASSWORD, UserRole.SALES_EXEC)


def _make_user(db, store, name, email, password, role) -> User:
    user = User(
        store_id=store.id,
        full_name=name,
        email=email,
        password_hash=hash_password(password),
        role=role.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_medicine(client, headers, **overrides):
    """Catalogue identity only — money lives on the batch."""
    payload = {
        "brand_name": "Testolol 50",
        "manufacturer_name": "Test Labs",
        "composition_text": "Atenolol 50mg",
        "dosage_form": "TABLET",
        "pack_size": "10",
    }
    payload.update(overrides)
    response = client.post(f"{API}/medicines", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def make_batch(client, headers, medicine_id, **overrides):
    """Give a medicine stock, and therefore a price."""
    payload = {
        "medicine_id": medicine_id,
        "batch_no": f"B{medicine_id}-1",
        "expiry_date": (business_date() + timedelta(days=365)).isoformat(),
        "qty_packs": "10",
        "mrp_per_pack": "100.00",
        "gst_rate": "12",
    }
    payload.update(overrides)
    response = client.post(f"{API}/inventory/batches", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def stocked_medicine(client, headers, *, mrp="100.00", qty_packs="10", **overrides):
    """Medicine + opening batch in one step, the common test setup."""
    medicine = make_medicine(client, headers, **overrides)
    batch = make_batch(
        client, headers, medicine["id"], mrp_per_pack=mrp, qty_packs=qty_packs
    )
    return medicine, batch


def make_supplier(client, headers, **overrides):
    """A distributor in the store's own state (18) — so CGST + SGST."""
    payload = {"name": "Test Distributors", "state_code": "18"}
    payload.update(overrides)
    response = client.post(f"{API}/suppliers", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def make_purchase(client, headers, supplier_id, medicine_id, **overrides):
    """One goods-inward invoice with a single line."""
    line = {
        "medicine_id": medicine_id,
        "batch_no": "PB-1",
        "expiry_date": (business_date() + timedelta(days=400)).isoformat(),
        "qty_packs": "10",
        "cost_per_pack": "100.00",
        "mrp_per_pack": "150.00",
        "gst_rate": "12",
    }
    line.update(overrides.pop("line", {}))
    payload = {
        "supplier_id": supplier_id,
        "supplier_invoice_no": "SUP-001",
        "supplier_invoice_date": business_date().isoformat(),
        "items": [line],
    }
    payload.update(overrides)
    response = client.post(f"{API}/purchases", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def sell(client, headers, items, **overrides):
    """Confirm a sale, paying the exact amount the quote came back with."""
    quote = client.post(
        f"{API}/pos/quote",
        json={"items": items, **{k: v for k, v in overrides.items() if k in ("customer", "doctor_id", "discount_override_reason")}},
        headers=headers,
    )
    assert quote.status_code == 200, quote.text
    payable = quote.json()["totals"]["net_amount"]

    payload = {
        "items": items,
        "payments": [{"mode": "CASH", "amount": payable}],
    }
    payload.update(overrides)
    response = client.post(f"{API}/sales", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, email: str, password: str) -> dict:
    """Returns the full token payload; raises loudly if the login failed."""
    response = client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(client: TestClient, email: str, password: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, email, password)['access_token']}"}


@pytest.fixture
def admin_headers(client, admin) -> dict[str, str]:
    return auth_headers(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
def sales_headers(client, sales) -> dict[str, str]:
    return auth_headers(client, SALES_EMAIL, SALES_PASSWORD)
