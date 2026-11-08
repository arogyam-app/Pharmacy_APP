"""Single import surface for Alembic autogenerate.

Every model module must be imported here, or its table will be missing from
``Base.metadata`` and Alembic will happily generate a migration that drops it.
"""

from app.db.base_class import Base  # noqa: F401
from app.models import (  # noqa: F401
    AuditLog,
    Batch,
    Composition,
    CompositionSalt,
    CreditNote,
    CreditNoteUsage,
    Customer,
    Doctor,
    DocumentSequence,
    ExpiryRegisterEntry,
    Manufacturer,
    Medicine,
    OrderBookEntry,
    Payment,
    PurchaseInvoice,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    RefreshToken,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    SaleVoidRequest,
    Salt,
    StockLedger,
    Store,
    StoreSettings,
    Supplier,
    SupplierCreditUsage,
    User,
)

# Phase 7 adds nothing new — reports read what is already here.
