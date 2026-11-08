"""Model registry. Import models from here, not from their modules."""

from app.models.audit import AuditLog, DocumentSequence
from app.models.catalog import Composition, CompositionSalt, Manufacturer, Medicine, Salt
from app.models.inventory import Batch, StockLedger
from app.models.party import Customer, Doctor, Supplier
from app.models.purchase import PurchaseInvoice, PurchaseItem
from app.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnItem,
    SupplierCreditUsage,
)
from app.models.returns import (
    CreditNote,
    CreditNoteUsage,
    SaleReturn,
    SaleReturnItem,
    SaleVoidRequest,
)
from app.models.sale import Payment, Sale, SaleItem
from app.models.stockwatch import ExpiryRegisterEntry, OrderBookEntry
from app.models.store import Store, StoreSettings
from app.models.user import RefreshToken, User, UserPermissionAccess

__all__ = [
    "AuditLog",
    "Batch",
    "Composition",
    "CompositionSalt",
    "CreditNote",
    "CreditNoteUsage",
    "Customer",
    "Doctor",
    "DocumentSequence",
    "ExpiryRegisterEntry",
    "Manufacturer",
    "Medicine",
    "OrderBookEntry",
    "Payment",
    "PurchaseInvoice",
    "PurchaseItem",
    "PurchaseReturn",
    "PurchaseReturnItem",
    "RefreshToken",
    "Sale",
    "SaleItem",
    "SaleReturn",
    "SaleReturnItem",
    "SaleVoidRequest",
    "Salt",
    "StockLedger",
    "Store",
    "StoreSettings",
    "Supplier",
    "SupplierCreditUsage",
    "User",
    "UserPermissionAccess",
]
