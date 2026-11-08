"""Aggregates every /api/v1 router. New phases append here.

Each router is gated here on the capability that *reaching* it needs, in one
line, so the rule is visible in one place rather than repeated on forty
routes. The finer verbs — create, edit, delete — are enforced inside the
routers, on the individual routes that perform them.

Several gates list more than one capability, and always for the same reason:
the endpoint genuinely serves more than one job.

* the catalogue (`/medicines`, `/compositions`, `/manufacturers`, `/salts`)
  and `/inventory` are read by the POS screen every time somebody types a
  brand name, so their reads answer to selling as well as to maintaining;
* `/customers` and `/doctors` are read *and added to* at the counter — the
  quick-create on a bill is not a master-data screen;
* `/credit-notes` is read at the counter to redeem one;
* `/suppliers` is read by the goods-inward form, which has to offer the
  choice, while maintaining the list stays with whoever keeps it.

So an executive who can sell can always finish a sale, whatever else has been
switched off for them.
"""

from fastapi import APIRouter, Depends

from app.api.v1 import (
    auth,
    catalog,
    customers,
    health,
    inventory,
    medicines,
    pos,
    purchase_returns,
    purchases,
    reports,
    returns,
    sales,
    settings,
    stockwatch,
    suppliers,
    users,
)
from app.core.deps import require_permission


def gate(*permissions: str):
    return [Depends(require_permission(*permissions))]


# Reads shared with the selling screen.
COUNTER_CATALOGUE = gate(
    "catalog:read", "pos:sell", "inventory:read", "medicines:create",
    "compositions:create", "manufacturers:create",
)
COUNTER_STOCK = gate("inventory:read", "pos:sell")
COUNTER_PARTIES = gate("customers:read", "doctors:read", "pos:sell")
COUNTER_CREDIT = gate("credit_notes:read", "pos:sell")

api_router = APIRouter()

# Open to any authenticated user: health, signing in, and who am I.
api_router.include_router(health.router)
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

api_router.include_router(
    users.router, prefix="/users", tags=["users"], dependencies=gate("users:manage")
)
# GET /settings and GET /store stay open — the POS screen and every printed
# invoice need the shop's name, GSTIN and print format. The writes inside
# carry `settings:manage`.
api_router.include_router(settings.router, tags=["settings"])

api_router.include_router(catalog.router, dependencies=COUNTER_CATALOGUE)
api_router.include_router(
    medicines.router, prefix="/medicines", tags=["medicines"], dependencies=COUNTER_CATALOGUE
)
api_router.include_router(
    inventory.router, prefix="/inventory", tags=["inventory"], dependencies=COUNTER_STOCK
)
api_router.include_router(
    suppliers.router, prefix="/suppliers", tags=["suppliers"],
    dependencies=gate("suppliers:read", "suppliers:manage", "purchases:read"),
)
api_router.include_router(
    purchases.router, prefix="/purchases", tags=["purchases"],
    dependencies=gate("purchases:read", "purchases:receive"),
)
api_router.include_router(
    # Its own prefix rather than nested under /purchases: a debit note is a
    # document in its own right, not a sub-resource of the invoice the goods
    # happened to arrive on — most returns cannot name one.
    purchase_returns.router, prefix="/purchase-returns", tags=["purchases"],
    dependencies=gate("purchases:read", "purchases:return"),
)
api_router.include_router(
    customers.customers_router, prefix="/customers", tags=["parties"],
    dependencies=COUNTER_PARTIES,
)
api_router.include_router(
    customers.doctors_router, prefix="/doctors", tags=["parties"], dependencies=COUNTER_PARTIES
)
api_router.include_router(
    pos.router, prefix="/pos", tags=["pos"], dependencies=gate("pos:sell")
)
# The register is the Sales screen; raising an invoice is the POS act.
api_router.include_router(
    sales.router, prefix="/sales", tags=["sales"], dependencies=gate("sales:read", "pos:sell"),
)
api_router.include_router(
    returns.returns_router, prefix="/returns", tags=["returns"],
    dependencies=gate("returns:create", "returns:approve"),
)
api_router.include_router(
    returns.credit_notes_router, prefix="/credit-notes", tags=["returns"],
    dependencies=COUNTER_CREDIT,
)
api_router.include_router(
    returns.void_router, prefix="/void-requests", tags=["returns"],
    dependencies=gate("sales:request_void", "sales:approve_void"),
)
api_router.include_router(
    stockwatch.alerts_router, prefix="/alerts", tags=["alerts"],
    dependencies=gate("alerts:read"),
)
api_router.include_router(
    stockwatch.expiry_router, prefix="/expiry-register", tags=["alerts"],
    dependencies=gate("expiry:read", "expiry:manage"),
)
api_router.include_router(
    stockwatch.order_book_router, prefix="/order-book", tags=["alerts"],
    dependencies=gate("order_book:read", "order_book:manage"),
)
api_router.include_router(
    reports.router, prefix="/reports", tags=["reports"], dependencies=gate("reports:view")
)
