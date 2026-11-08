# Pharmacy POS — Agent Guide

Retail pharmacy Point-of-Sale, inventory and reporting web app. Single store today, **multi-store ready by design** (every business table carries `store_id`).

## Read these before building

| Document | Contents |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | Functional requirements, roles, business rules, reports |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Complete schema, columns, constraints, enums |
| [docs/API.md](docs/API.md) | Every REST endpoint, request/response shapes |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Build phases with acceptance criteria |

## Stack

- **Backend:** Python 3.11+, FastAPI (pure JSON API — no templates, no HTML from the server), SQLAlchemy 2.0 ORM, Alembic migrations, Pydantic v2 schemas.
- **DB:** SQLite for dev/test → PostgreSQL later. Never write raw SQLite-only SQL; keep everything ORM/Alembic-portable.
- **Frontend:** Vue 3 SPA (`<script setup>` Composition API), Vite, Vue Router, Pinia, Axios, plain CSS or Tailwind. Separate dev server, talks to the API over CORS.
- **Auth:** JWT access + refresh tokens, password hashing with `passlib[argon2]`.

## Repository layout

```
Pharmacy_APP/
├── CLAUDE.md
├── docs/                     # SPEC.md, DATA_MODEL.md, API.md, ROADMAP.md
├── backend/
│   ├── .venv/                # virtualenv (git-ignored)
│   ├── app/
│   │   ├── main.py           # FastAPI app factory, CORS, routers
│   │   ├── core/             # config, security, deps, exceptions
│   │   ├── db/               # session, base, seed
│   │   ├── models/           # SQLAlchemy models (one file per domain)
│   │   ├── schemas/          # Pydantic request/response models
│   │   ├── api/v1/           # routers: auth, catalog, inventory, pos, sales,
│   │   │                     #   returns, customers, doctors, purchases,
│   │   │                     #   reports, admin, alerts
│   │   ├── services/         # business logic — pricing, batch allocation,
│   │   │                     #   invoicing, credit notes, order book, GST
│   │   └── utils/
│   ├── alembic/
│   ├── tests/
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── api/              # axios client + typed service modules
    │   ├── stores/           # pinia: auth, cart, alerts, settings
    │   ├── router/
    │   ├── views/            # Login, POS, Sales, Inventory, Purchases,
    │   │                     #   Reports, Admin, Masters
    │   ├── components/
    │   └── composables/
    └── vite.config.js
```

## Environment setup (do this first, once)

```powershell
# Backend
cd e:\Pharmacy_APP\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
alembic upgrade head
python -m app.db.seed          # roles, admin user, settings, sample masters
uvicorn app.main:app --reload   # http://127.0.0.1:8000/docs

# Frontend (second terminal)
cd e:\Pharmacy_APP\frontend
npm install
npm run dev                     # http://127.0.0.1:5173
```

`requirements.txt` baseline: `fastapi`, `uvicorn[standard]`, `sqlalchemy>=2.0`, `alembic`, `pydantic>=2`, `pydantic-settings`, `python-jose[cryptography]`, `passlib[argon2]`, `python-multipart`, `python-dateutil`, `openpyxl`, `pytest`, `httpx`.

## Non-negotiable engineering rules

1. **Money is `Decimal`, never `float`.** Use the column types in `app/db/types.py` — `Money()` (12,2), `UnitPrice()` (12,4), `Quantity()` (12,3), `Rate()` (6,2) — never bare `Numeric`, and `UUIDPK()` for primary keys and every foreign key. Round only at the final line/invoice total, half-up. **Never `CHECK` a decimal column against a numeric literal:** SQLite stores these scaled (`10.00` → `1000`), so the constraint would mean something different on each dialect. Compare two same-scale columns, and enforce ranges in the Pydantic schema.
2. **Stock is tracked in the smallest sellable unit**, never in packs. A pack carries `pack_unit` (TABLET, CAPSULE, ML, GM, IU, VIAL, UNIT, PIECE) and `pack_size`; `units_per_pack` is derived from them by `services.catalog.resolve_pack()` and is the **only** pack→unit conversion. A pack of **countable articles** — `COUNTABLE_UNITS` = TABLET, CAPSULE, PIECE — gets `units_per_pack = pack_size`, which is what makes a single-tablet sale work, and equally a single soap out of a box of twelve. Everything else is one sellable unit per container (`units_per_pack = 1`): a 100 ml syrup is one bottle, not 100 units, and so is a 500 ml floor cleaner. Countability follows the pack *unit*, not the dosage form.
   - **The catalogue holds two kinds of row.** `medicines.item_type` is MEDICINE or GENERAL; a pharmacy also sells soap, nappies and a thermometer, which share a batch, an MRP, a GST rate and a place on the same bill with medicines and share none of the pharmacological attributes. A general item has no `composition_id`, no schedule and no prescription flag, and `dosage_form` carries the GENERAL placeholder only so pack resolution has something to key off.
   - **A batch's shelf life is stated three ways** — a printed expiry, a manufacturing date plus best-before months, or none at all — and `services.inventory.resolve_expiry()` is the only place that turns them into one date. `batches.expiry_date` stays NOT NULL because FEFO, the sellable filter, alerts and the expiry register all query it; stock with no shelf life carries the `enums.NO_EXPIRY` sentinel. Read `Batch.has_expiry`, never the raw date, before showing or printing an expiry.
3. **Every stock movement writes a `stock_ledger` row.** `batches.qty_available` is a cached projection and must always equal the ledger sum for that batch; a test asserts this.
4. **Cost price never leaves the API for a non-admin.** Enforce in the response schema layer (separate `…Public` vs `…Admin` Pydantic models), not just the UI.
5. **Financial documents are immutable.** Sales, returns and credit notes are never `DELETE`d or silently updated — corrections happen through the void-approval or return flow. A **purchase invoice** is the one document that may still be *added to* (`POST /purchases/{id}/items`), because goods from one supplier invoice reach the shelf over more than one sitting and a medicine and a general item are keyed from different screens. Additions only: nothing existing is edited or removed, the totals are recomputed from the lines, and every addition writes an audit row. Correcting or removing a purchase line is still cancel-and-re-enter.
6. **Every write endpoint records an `audit_log` row** (actor, action, entity, before/after JSON).
7. **All monetary/stock mutations run inside one DB transaction** with `SELECT … FOR UPDATE` semantics on batches (on SQLite, a service-level lock; the code must be Postgres-correct).
8. **Server is the authority on price and discount.** The client sends `medicine_id`, `qty`, requested discount; the server recomputes everything and rejects out-of-policy discounts.
9. **Timestamps are UTC; business dates are IST.** An *instant* (`created_at`, `invoice_datetime`, `used_at`) is timezone-aware UTC in the DB, and the API returns it as ISO-8601 **carrying its offset** — SQLite hands back naive datetimes, so serialise through `utils.clock.utc_iso()`, never bare `.isoformat()`; a naive string is read by the browser as local time. A *business date* — what goes on an invoice, when a batch expires, how a return window is measured — is the date at the counter, and comes from `utils.clock.business_date()`, never from `datetime.now(timezone.utc).date()`. The two disagree every night between 00:00 and 05:30 IST, which is exactly when nobody is looking.
10. Write tests alongside features — pricing, batch allocation (FEFO), discount limits, returns/credit notes and GST maths are mandatory test targets.

## Conventions

- API prefix `/api/v1`, plural kebab-free snake resources (`/sales`, `/stock-batches` → `/batches`).
- Errors: `{"detail": {"code": "DISCOUNT_LIMIT_EXCEEDED", "message": "...", "field": "discount_pct"}}`.
- List endpoints: `?page=1&page_size=25&q=&sort=-created_at`, response `{"items": [], "total": n, "page": 1, "page_size": 25}`.
- Enums live in `app/models/enums.py` and are mirrored in `frontend/src/constants.js`.
- Migrations: one Alembic revision per logical change, never edit an applied revision.
