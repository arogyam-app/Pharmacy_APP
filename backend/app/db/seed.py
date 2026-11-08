"""Idempotent seed: store, settings, sequences, admin user, demo catalogue.

    python -m app.db.seed              # everything
    python -m app.db.seed --no-demo    # core rows only

Safe to re-run — existing rows are left alone. Later phases extend this with
opening stock (docs/DATA_MODEL.md §9).
"""

from uuid import UUID

import sys
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.security import hash_password
from app.db.demo_catalog import DEMO_MEDICINES
from app.db.session import SessionLocal
from app.models.audit import DocumentSequence
from app.models.catalog import Manufacturer, Medicine
from app.models.enums import DocumentType, ScheduleType, UserRole
from app.models.inventory import Batch
from app.models.party import Doctor, Supplier
from app.models.store import Store, StoreSettings
from app.models.user import User
from app.services import catalog as catalog_service
from app.services import inventory as inventory_service
from app.services import sequences


def seed_store(db: Session) -> Store:
    store = db.query(Store).order_by(Store.id).first()
    if store:
        return store
    store = Store(
        name=app_settings.seed_store_name,
        legal_name=app_settings.seed_store_name,
        state_code=app_settings.seed_store_state_code,
    )
    db.add(store)
    db.flush()
    print(f"  + store #{store.id} {store.name!r}")
    return store


def seed_settings(db: Session, store: Store) -> StoreSettings:
    row = db.query(StoreSettings).filter(StoreSettings.store_id == store.id).one_or_none()
    if row:
        return row
    # Defaults come from the model (10% default / 20% max discount, etc.).
    row = StoreSettings(store_id=store.id)
    db.add(row)
    db.flush()
    print(f"  + settings for store #{store.id} "
          f"(discount {row.default_discount_pct}%–{row.max_discount_pct}%)")
    return row


def seed_sequences(db: Session, store: Store) -> None:
    existing = {
        doc_type
        for (doc_type,) in db.query(DocumentSequence.doc_type).filter(
            DocumentSequence.store_id == store.id
        )
    }
    for doc_type in DocumentType:
        if doc_type.value in existing:
            continue
        start = sequences.starting_number(doc_type)
        db.add(
            DocumentSequence(store_id=store.id, doc_type=doc_type.value, next_number=start)
        )
        print(f"  + sequence {doc_type.value} -> {str(start).zfill(sequences.NUMBER_WIDTH)}")


def seed_admin(db: Session, store: Store) -> User | None:
    email = app_settings.seed_admin_email.strip().lower()
    if db.query(User).filter(User.email == email).count():
        return None
    user = User(
        store_id=store.id,
        full_name="Store Administrator",
        email=email,
        role=UserRole.ADMIN.value,
        password_hash=hash_password(app_settings.seed_admin_password),
        must_change_password=True,
    )
    db.add(user)
    db.flush()
    print(f"  + admin {email} (password from SEED_ADMIN_PASSWORD; change on first sign-in)")
    return user


"""Two distributors: one in the store's own state, one outside it — so the
intra-state (CGST + SGST) and inter-state (IGST) paths both have a supplier
to demonstrate them."""
DEMO_SUPPLIERS = [
    # name, state_code offset, gstin prefix, contact, terms
    ("Meditrust Distributors", None, "Ramesh Nair", 30),
    ("Northline Pharma Agencies", "07", "Sunita Rao", 15),
]


def seed_suppliers(db: Session, store: Store, actor: User | None) -> None:
    actor_id = actor.id if actor else None
    created = 0
    for name, state_code, contact, terms in DEMO_SUPPLIERS:
        exists = (
            db.query(Supplier)
            .filter(Supplier.store_id == store.id, func.lower(Supplier.name) == name.lower())
            .count()
        )
        if exists:
            continue
        # A NULL state code on the second supplier would read as intra-state;
        # the explicit "07" is what makes it an IGST supplier.
        code = state_code or store.state_code
        db.add(
            Supplier(
                store_id=store.id,
                name=name,
                state_code=code,
                gstin=f"{code or '00'}ABCDE1234F1Z{len(name) % 10}" if code else None,
                contact_person=contact,
                payment_terms_days=terms,
                created_by_id=actor_id,
                updated_by_id=actor_id,
            )
        )
        created += 1
    if created:
        print(f"  + {created} demo suppliers")


"""A few prescribers, so a Schedule H sale can be demonstrated without
first having to create one."""
DEMO_DOCTORS = [
    ("Dr Anjali Sen", "WBMC-44821", "General Medicine", "Sen Clinic"),
    ("Dr Rakesh Iyer", "WBMC-51033", "Paediatrics", "City Hospital"),
    ("Dr Meera Ghosh", "WBMC-38290", "Dermatology", "Skin & Care Centre"),
]


def seed_doctors(db: Session, store: Store, actor: User | None) -> None:
    actor_id = actor.id if actor else None
    created = 0
    for name, registration, specialization, clinic in DEMO_DOCTORS:
        exists = (
            db.query(Doctor)
            .filter(Doctor.store_id == store.id, func.lower(Doctor.name) == name.lower())
            .count()
        )
        if exists:
            continue
        db.add(
            Doctor(
                store_id=store.id,
                name=name,
                registration_no=registration,
                specialization=specialization,
                hospital_clinic=clinic,
                created_by_id=actor_id,
                updated_by_id=actor_id,
            )
        )
        created += 1
    if created:
        print(f"  + {created} demo doctors")


def _seed_opening_batch(
    db: Session, store: Store, medicine: Medicine, mrp: str, gst_rate: str, actor_id: UUID | None
) -> bool:
    """Give a demo medicine one opening batch, so it has a price and stock.

    Expiries are spread deliberately — most far out, a couple inside the
    near-expiry window and one already lapsed — so the Phase 6 alerts have
    something real to fire on.
    """
    if db.query(Batch).filter(Batch.medicine_id == medicine.id).count():
        return False

    today = date.today()
    # Deterministic spread keyed off the medicine id. `.int` is the UUID's
    # 128 bits as a number — the key is no longer a small integer, so it
    # cannot be used as one directly.
    bucket = medicine.id.int % 12
    if bucket == 0:
        expiry = today - timedelta(days=20)      # already expired
    elif bucket in (1, 2):
        expiry = today + timedelta(days=45)      # inside the 90-day warning
    else:
        expiry = today + timedelta(days=300 + bucket * 15)

    # Retail margin runs roughly 15-25 % on MRP.
    mrp_decimal = Decimal(mrp)
    cost = (mrp_decimal * Decimal("0.78")).quantize(Decimal("0.01"))

    inventory_service.create_batch(
        db,
        store_id=store.id,
        medicine=medicine,
        # A batch number goes on a shelf label and gets read out loud, so it
        # stays short: the last four hex digits of the key, not the key.
        batch_no=f"B{medicine.id.hex[-4:].upper()}A",
        expiry_date=expiry,
        qty_units=Decimal(medicine.units_per_pack * 10),  # ten packs
        mrp_per_pack=mrp_decimal,
        cost_per_pack=cost,
        gst_rate=Decimal(gst_rate),
        received_date=today - timedelta(days=15),
        actor_id=actor_id,
    )
    return True


def seed_demo_catalogue(db: Session, store: Store, actor: User | None) -> None:
    """Demo medicines, with several brands per composition so the
    same-salt alternatives panel has something to show."""
    actor_id = actor.id if actor else None
    created = filled = 0
    prescription_schedules = {
        ScheduleType.SCHEDULE_H.value,
        ScheduleType.SCHEDULE_H1.value,
        ScheduleType.SCHEDULE_X.value,
    }

    for (
        brand, manufacturer_name, composition_text, therapeutic_class, dosage_form,
        pack_size, pack_unit, schedule_type, hsn_code, gst_rate, mrp_per_pack,
    ) in DEMO_MEDICINES:
        manufacturer = (
            db.query(Manufacturer)
            .filter(func.lower(Manufacturer.name) == manufacturer_name.lower())
            .one_or_none()
        )
        if manufacturer is None:
            manufacturer = Manufacturer(
                name=manufacturer_name, created_by_id=actor_id, updated_by_id=actor_id
            )
            db.add(manufacturer)
            db.flush()

        composition = catalog_service.resolve_composition(
            db,
            composition_text=composition_text,
            therapeutic_class=therapeutic_class,
            actor_id=actor_id,
        )

        resolved_size, resolved_unit, units_per_pack, pack_label = (
            catalog_service.resolve_pack(dosage_form, Decimal(pack_size), pack_unit)
        )

        existing = (
            db.query(Medicine)
            .filter(
                Medicine.store_id == store.id,
                func.lower(Medicine.brand_name) == brand.lower(),
                Medicine.manufacturer_id == manufacturer.id,
            )
            .first()
        )
        if existing is not None:
            # Re-runnable: give an already-catalogued medicine its opening
            # stock if it has none, so prices appear without duplicating rows.
            if _seed_opening_batch(db, store, existing, mrp_per_pack, gst_rate, actor_id):
                filled += 1
            continue

        medicine = Medicine(
            store_id=store.id,
            brand_name=brand,
            manufacturer_id=manufacturer.id,
            composition_id=composition.id,
            dosage_form=dosage_form,
            pack_size=resolved_size,
            pack_unit=resolved_unit,
            pack_label=pack_label,
            units_per_pack=units_per_pack,
            schedule_type=schedule_type,
            is_prescription_required=schedule_type in prescription_schedules,
            hsn_code=hsn_code,
            created_by_id=actor_id,
            updated_by_id=actor_id,
        )
        db.add(medicine)
        db.flush()
        catalog_service.refresh_search_text(db, medicine)
        _seed_opening_batch(db, store, medicine, mrp_per_pack, gst_rate, actor_id)
        created += 1

    compositions = (
        db.query(func.count(func.distinct(Medicine.composition_id)))
        .filter(Medicine.store_id == store.id)
        .scalar()
    )
    batches = db.query(func.count(Batch.id)).filter(Batch.store_id == store.id).scalar()
    if created:
        print(f"  + {created} demo medicines across {compositions} compositions")
    if filled:
        print(f"  ~ {filled} existing medicines given opening stock")
    if not created and not filled:
        print("  = demo catalogue already present and stocked")
    print(f"  = {batches} batches carry the MRP, GST and cost")


def run(with_demo: bool = True) -> None:
    db = SessionLocal()
    try:
        print(f"Seeding {app_settings.database_url}")
        store = seed_store(db)
        seed_settings(db, store)
        seed_sequences(db, store)
        admin = seed_admin(db, store)
        if admin is None:
            admin = (
                db.query(User)
                .filter(User.store_id == store.id, User.role == UserRole.ADMIN.value)
                .first()
            )
        if with_demo:
            seed_suppliers(db, store, admin)
            seed_doctors(db, store, admin)
            seed_demo_catalogue(db, store, admin)
        db.commit()
        print("Seed complete.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    try:
        run(with_demo="--no-demo" not in sys.argv)
    except Exception as exc:  # pragma: no cover
        print(f"Seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
