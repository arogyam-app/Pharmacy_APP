"""Every enum in the domain. Mirrored in frontend/src/constants.js.

Stored as strings with a CHECK constraint (portable, migration-friendly)
rather than a native DB enum type.
"""

from datetime import date as _date
from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    SALES_EXEC = "SALES_EXEC"


class AppModule(StrEnum):
    """A part of the app an admin can grant to or withhold from one person.

    The unit is a *screen and the endpoints behind it*, not a single action,
    because that is the size of thing an owner actually decides about: "Priya
    handles purchases, Ravi does not." The finer permission strings in
    `app/core/permissions.py` are derived from these.
    """

    POS = "POS"
    SALES = "SALES"
    RETURNS = "RETURNS"
    INVENTORY = "INVENTORY"
    PURCHASES = "PURCHASES"
    SUPPLIERS = "SUPPLIERS"
    MASTERS = "MASTERS"
    COMPOSITIONS = "COMPOSITIONS"
    MANUFACTURERS = "MANUFACTURERS"
    ALERTS = "ALERTS"
    ORDER_BOOK = "ORDER_BOOK"
    EXPIRY = "EXPIRY"
    REPORTS = "REPORTS"
    USERS = "USERS"
    SETTINGS = "SETTINGS"


class ItemType(StrEnum):
    """What kind of thing the shop is selling.

    A pharmacy counter also sells soap, sanitary pads, a thermometer. Those
    share everything that matters operationally with a medicine — a batch, a
    price, a GST rate, a place on the same bill — and share none of the
    pharmacological attributes: there is no salt in a comb, no schedule, no
    prescription. So they are one catalogue with two kinds of row rather than
    two catalogues, which is what keeps a mixed basket on one invoice.
    """

    MEDICINE = "MEDICINE"
    GENERAL = "GENERAL"


class DosageForm(StrEnum):
    TABLET = "TABLET"
    CAPSULE = "CAPSULE"
    SYRUP = "SYRUP"
    INJECTION = "INJECTION"
    OINTMENT = "OINTMENT"
    DROPS = "DROPS"
    POWDER = "POWDER"
    OTHER = "OTHER"
    #: Not a dosage form at all — the placeholder a general item carries so
    #: that pack resolution, which keys off this column, has something to
    #: work with. Never shown as a dosage form in the UI.
    GENERAL = "GENERAL"


class PackUnit(StrEnum):
    """How a pack is measured.

    "Strip of 10" only makes sense for tablets and capsules. A syrup is
    measured in ml, an ointment in grams, an injection in ml or vials — so
    the pack measure travels with the medicine rather than being assumed.
    """

    TABLET = "TABLET"
    CAPSULE = "CAPSULE"
    ML = "ML"
    GM = "GM"
    IU = "IU"
    VIAL = "VIAL"
    UNIT = "UNIT"
    #: One countable article — a soap, a razor, a nappy. Countable for the
    #: same reason a tablet is: a box of twelve is twelve things a customer
    #: can buy one of.
    PIECE = "PIECE"


class ScheduleType(StrEnum):
    OTC = "OTC"
    SCHEDULE_H = "SCHEDULE_H"
    SCHEDULE_H1 = "SCHEDULE_H1"
    SCHEDULE_X = "SCHEDULE_X"
    OTHER = "OTHER"


class GstRegistrationType(StrEnum):
    REGULAR = "REGULAR"
    COMPOSITION = "COMPOSITION"
    UNREGISTERED = "UNREGISTERED"


class PrintFormat(StrEnum):
    #: Full-page letterhead bill: shop logo, partner logo and an order QR.
    #: The shop's own default.
    A4 = "A4"
    THERMAL_80 = "THERMAL_80"
    A5 = "A5"
    #: A copy of A5, with its own markup and its own stylesheet so the two
    #: can be changed apart. Added as a second A5 to work on without
    #: disturbing the one the shop is currently billing on.
    A5_NEW = "A5_NEW"


class DocumentType(StrEnum):
    SALE = "SALE"
    RETURN = "RETURN"
    CREDIT_NOTE = "CREDIT_NOTE"
    PURCHASE = "PURCHASE"
    #: Goods sent back to a distributor. Its own series, because the document
    #: goes to the supplier and has to be quotable against their credit note.
    PURCHASE_RETURN = "PURCHASE_RETURN"
    ADJUSTMENT = "ADJUSTMENT"


class SaleStatus(StrEnum):
    DRAFT = "DRAFT"
    COMPLETED = "COMPLETED"
    VOID_REQUESTED = "VOID_REQUESTED"
    VOIDED = "VOIDED"
    PARTIALLY_RETURNED = "PARTIALLY_RETURNED"
    RETURNED = "RETURNED"


class PaymentMode(StrEnum):
    CASH = "CASH"
    CARD = "CARD"
    UPI = "UPI"
    NET_BANKING = "NET_BANKING"
    CREDIT_NOTE = "CREDIT_NOTE"
    OTHER = "OTHER"


class MovementType(StrEnum):
    """Signed stock movements written to stock_ledger."""

    OPENING = "OPENING"
    PURCHASE = "PURCHASE"
    SALE = "SALE"
    SALE_RETURN = "SALE_RETURN"
    PURCHASE_RETURN = "PURCHASE_RETURN"
    VOID_REVERSAL = "VOID_REVERSAL"
    ADJUSTMENT_IN = "ADJUSTMENT_IN"
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT"
    EXPIRY_WRITE_OFF = "EXPIRY_WRITE_OFF"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"


class ReferenceType(StrEnum):
    SALE = "SALE"
    SALE_ITEM = "SALE_ITEM"
    PURCHASE_ITEM = "PURCHASE_ITEM"
    RETURN_ITEM = "RETURN_ITEM"
    PURCHASE_RETURN_ITEM = "PURCHASE_RETURN_ITEM"
    ADJUSTMENT = "ADJUSTMENT"
    EXPIRY = "EXPIRY"


class PurchaseReturnStatus(StrEnum):
    """A debit note is a financial document: it is never edited, only voided.

    Cancelling puts the stock back and withdraws the credit, which is refused
    once any of that credit has been spent — see CLAUDE.md rule 5.
    """

    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SupplierCreditStatus(StrEnum):
    """How much of a debit note's value is still available to set against a
    future invoice from the same distributor."""

    OPEN = "OPEN"
    PARTIALLY_USED = "PARTIALLY_USED"
    EXHAUSTED = "EXHAUSTED"
    CANCELLED = "CANCELLED"


class AdjustmentType(StrEnum):
    IN = "IN"
    OUT = "OUT"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReturnStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REJECTED = "REJECTED"


class RefundMode(StrEnum):
    CASH = "CASH"
    CREDIT_NOTE = "CREDIT_NOTE"
    CARD_REVERSAL = "CARD_REVERSAL"


class ItemCondition(StrEnum):
    SALEABLE = "SALEABLE"
    DAMAGED = "DAMAGED"
    EXPIRED = "EXPIRED"


class CreditNoteStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_USED = "PARTIALLY_USED"
    FULLY_USED = "FULLY_USED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PurchasePaymentStatus(StrEnum):
    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


class ExpirySource(StrEnum):
    EXPIRED_STOCK = "EXPIRED_STOCK"
    SALE_RETURN_EXPIRED = "SALE_RETURN_EXPIRED"
    DAMAGED = "DAMAGED"


class ExpiryStatus(StrEnum):
    IDENTIFIED = "IDENTIFIED"
    WRITTEN_OFF = "WRITTEN_OFF"
    RETURNED_TO_SUPPLIER = "RETURNED_TO_SUPPLIER"
    CREDIT_RECEIVED = "CREDIT_RECEIVED"


class OrderBookTrigger(StrEnum):
    STOCK_DECLINE_60 = "STOCK_DECLINE_60"
    BELOW_REORDER_LEVEL = "BELOW_REORDER_LEVEL"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    MANUAL = "MANUAL"


class OrderBookStatus(StrEnum):
    OPEN = "OPEN"
    ORDERED = "ORDERED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    VOID = "VOID"
    PRINT = "PRINT"
    OVERRIDE = "OVERRIDE"


class ExpiryBasis(StrEnum):
    """How a batch's expiry date was arrived at.

    A medicine box prints an expiry. A bottle of shampoo prints a
    manufacturing date and "best before 24 months", and the shelf date has to
    be worked out from the two. A comb prints neither and never goes off.

    All three still land in `batches.expiry_date`, because FEFO allocation,
    the near-expiry alert and the expiry register are all built on that one
    column and would each need a null-safe variant otherwise. What differs is
    where the date came from, and whether it means anything at all — which is
    what this column records.
    """

    #: Printed on the pack and typed in as-is.
    EXPIRY_DATE = "EXPIRY_DATE"
    #: Computed from `mfg_date` + `best_before_months`.
    BEST_BEFORE = "BEST_BEFORE"
    #: The item has no shelf life. `expiry_date` holds NO_EXPIRY.
    NONE = "NONE"


#: The date stored for stock that never expires.
#:
#: A sentinel rather than a NULL: `expiry_date` is NOT NULL, indexed, part of
#: the batch's unique key, and compared against in FEFO ordering, the
#: sellable-batch filter, the alerts query and the expiry register. Making it
#: nullable would mean teaching every one of those what a missing date means,
#: and getting it wrong anywhere would either hide sellable stock or offer
#: expired stock. A date no consignment will ever reach sorts last under
#: FEFO, passes every "not yet expired" test and matches no near-expiry
#: window — which is exactly the behaviour wanted — and `expiry_basis` says
#: it is a sentinel so nothing ever prints it.
NO_EXPIRY = _date(9999, 12, 31)


def values(enum_cls: type[StrEnum]) -> list[str]:
    """Handy for CHECK constraints: values(SaleStatus)."""
    return [member.value for member in enum_cls]


# Only tablets and capsules come in countable strips; everything else is a
# single container measured by volume or weight. This table drives both the
# server-side pack rules and the dynamic field in the add-medicine form.
PACK_UNITS_BY_FORM: dict[str, list[PackUnit]] = {
    DosageForm.TABLET: [PackUnit.TABLET],
    DosageForm.CAPSULE: [PackUnit.CAPSULE],
    DosageForm.SYRUP: [PackUnit.ML],
    DosageForm.DROPS: [PackUnit.ML],
    DosageForm.INJECTION: [PackUnit.ML, PackUnit.VIAL, PackUnit.IU],
    DosageForm.OINTMENT: [PackUnit.GM, PackUnit.ML],
    DosageForm.POWDER: [PackUnit.GM, PackUnit.ML],
    DosageForm.OTHER: [PackUnit.UNIT, PackUnit.ML, PackUnit.GM],
    DosageForm.GENERAL: [PackUnit.PIECE, PackUnit.ML, PackUnit.GM, PackUnit.UNIT],
}

# Forms sold as individually countable pieces — the only ones where
# "units per pack" is a real quantity rather than always 1.
#
# GENERAL joins the two medicinal forms because the same fact holds: a box of
# twelve soaps is twelve things a customer can buy one of, exactly as a strip
# of ten tablets is ten. A general item that is *not* divisible — one shampoo
# bottle, one thermometer — is simply catalogued with a pack size of 1.
COUNTABLE_FORMS = {DosageForm.TABLET, DosageForm.CAPSULE, DosageForm.GENERAL}

# The finer test, and the one that actually decides units-per-pack. A general
# item can be a box of twelve soaps (PIECE — twelve sellable units) or a
# 500 ml bottle of floor cleaner (ML — one), so the form alone is not enough:
# it is the *unit* that says whether the pack holds countable articles.
COUNTABLE_UNITS = {PackUnit.TABLET, PackUnit.CAPSULE, PackUnit.PIECE}


def default_pack_unit(dosage_form: str) -> PackUnit:
    return PACK_UNITS_BY_FORM.get(dosage_form, [PackUnit.UNIT])[0]
