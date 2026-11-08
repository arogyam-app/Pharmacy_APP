"""Goods coming back against an invoice.

The refund is the line's own post-discount value, pro-rata by quantity, with
GST reversed at the rate **snapshotted on the sale** — not at today's rate.
A slab change between the sale and the return must not alter what the
customer gets back.

What happens to the goods depends on their condition:

* `SALEABLE` — back into the original batch through a `SALE_RETURN` ledger
  row, ready to sell again;
* `DAMAGED` / `EXPIRED` — refunded but **not** restocked. They are recorded
  against the return line and picked up by the expiry/damage register
  (Phase 6). Putting them back on the shelf is how expired stock gets sold.

Settlement is cash or a credit note. A credit note needs a customer, so a
walk-in either supplies a mobile number or takes cash.
"""

from uuid import UUID

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db.types import quantize_money, quantize_qty
from app.models.enums import (
    DocumentType,
    ItemCondition,
    MovementType,
    RefundMode,
    ReferenceType,
    ReturnStatus,
    SaleStatus,
)
from app.models.inventory import Batch
from app.models.party import Customer
from app.models.returns import SaleReturn, SaleReturnItem
from app.models.sale import Sale, SaleItem
from app.models.store import StoreSettings
from app.models.user import User
from app.services import credit_notes, inventory, pricing, sequences
from app.utils.clock import business_date

ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")


class ReturnError(AppError):
    code = "RETURN_INVALID"


def returnable_items(db: Session, sale: Sale) -> list[dict]:
    """Per line: what was sold, what has already come back, what is left."""
    rows = []
    for item in sale.items:
        remaining = quantize_qty(item.qty_units - item.returned_qty_units)
        rows.append(
            {
                "sale_item_id": item.id,
                "line_no": item.line_no,
                "medicine_id": item.medicine_id,
                "medicine_name": item.medicine_name_snapshot,
                "batch_id": item.batch_id,
                "batch_no": item.batch_no_snapshot,
                "expiry_date": item.expiry_date_snapshot,
                "qty_sold_units": item.qty_units,
                "qty_returned_units": item.returned_qty_units,
                "qty_returnable_units": max(ZERO_QTY, remaining),
                "unit_mrp": item.unit_mrp,
                "discount_pct": item.discount_pct,
                "gst_rate": item.gst_rate,
                # What a full return of the remainder would refund.
                "refund_if_all": _line_refund(item, remaining, sale.is_intra_state)["refund"],
            }
        )
    return rows


def _line_refund(item: SaleItem, qty_units: Decimal, is_intra_state: bool) -> dict:
    """Pro-rata share of the line, taxed at the snapshotted rate."""
    qty = quantize_qty(qty_units)
    if qty <= ZERO_QTY:
        return {
            "refund": ZERO, "taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO,
        }

    priced = pricing.price_line(
        qty_units=qty,
        unit_mrp=item.unit_mrp,
        gst_rate=item.gst_rate,
        discount_pct=item.discount_pct,
        is_intra_state=is_intra_state,
        # The sale's own net was GST-inclusive, so the reversal is too.
        price_includes_gst=True,
    )
    return {
        "refund": priced.net_amount,
        "taxable": priced.taxable_value,
        "cgst": priced.cgst_amount,
        "sgst": priced.sgst_amount,
        "igst": priced.igst_amount,
        "gross": priced.gross_amount,
        "discount": priced.discount_amount,
    }


def post_return(
    db: Session,
    *,
    store_id: UUID,
    sale: Sale,
    payload,
    settings: StoreSettings,
    user: User,
) -> SaleReturn:
    """Take goods back against an invoice and settle the refund."""
    if sale.status == SaleStatus.VOIDED.value:
        raise ConflictError(
            f"Invoice {sale.invoice_no} is void — there is nothing to return against it.",
            code="SALE_ALREADY_VOIDED",
        )
    if not payload.items:
        raise ReturnError("Choose at least one line to return.", field="items")

    today = business_date()
    days_since = (today - sale.invoice_date).days

    # Beyond the window a return is the owner's call, not the counter's.
    outside_window = days_since > settings.return_window_days
    if outside_window and not user.is_admin:
        raise ConflictError(
            f"Invoice {sale.invoice_no} is {days_since} days old, past the "
            f"{settings.return_window_days}-day return window. An administrator has to "
            f"approve this one.",
            code="RETURN_WINDOW_EXCEEDED",
        )

    by_id = {item.id: item for item in sale.items}
    sale_return = SaleReturn(
        store_id=store_id,
        return_no=sequences.next_document_number(db, store_id, DocumentType.RETURN),
        sale_id=sale.id,
        return_date=today,
        reason=payload.reason,
        refund_mode=str(payload.refund_mode),
        processed_by_id=user.id,
        processed_by_name=user.full_name,
        status=ReturnStatus.COMPLETED.value,
        days_since_sale=days_since,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    if outside_window:
        # Recorded on the document: an old return should be visibly one.
        sale_return.approved_by_id = user.id
        sale_return.approved_by_name = user.full_name
        sale_return.approved_at = datetime.now(timezone.utc)
    db.add(sale_return)
    db.flush()

    totals = {
        "gross": ZERO, "discount": ZERO, "taxable": ZERO,
        "cgst": ZERO, "sgst": ZERO, "igst": ZERO, "refund": ZERO,
    }
    any_restocked = False

    for index, line in enumerate(payload.items):
        item = by_id.get(line.sale_item_id)
        if item is None:
            raise NotFoundError(
                f"Line {index + 1} does not belong to invoice {sale.invoice_no}.",
                field=f"items.{index}.sale_item_id",
            )

        qty = quantize_qty(line.qty_units)
        remaining = quantize_qty(item.qty_units - item.returned_qty_units)
        if qty <= ZERO_QTY:
            raise ReturnError(
                f"{item.medicine_name_snapshot}: a return must be for more than zero.",
                field=f"items.{index}.qty_units",
            )
        if qty > remaining:
            raise ConflictError(
                f"{item.medicine_name_snapshot}: {remaining} unit(s) can still be "
                f"returned, but {qty} was entered.",
                code="RETURN_QTY_EXCEEDS_SOLD",
                field=f"items.{index}.qty_units",
            )

        money = _line_refund(item, qty, sale.is_intra_state)
        condition = str(line.condition or ItemCondition.SALEABLE)
        restock = condition == ItemCondition.SALEABLE.value

        return_item = SaleReturnItem(
            sale_return_id=sale_return.id,
            sale_item_id=item.id,
            medicine_id=item.medicine_id,
            batch_id=item.batch_id,
            medicine_name_snapshot=item.medicine_name_snapshot,
            batch_no_snapshot=item.batch_no_snapshot,
            qty_units=qty,
            unit_mrp=item.unit_mrp,
            discount_pct=item.discount_pct,
            gst_rate=item.gst_rate,
            taxable_value=money["taxable"],
            cgst_amount=money["cgst"],
            sgst_amount=money["sgst"],
            igst_amount=money["igst"],
            refund_amount=money["refund"],
            condition=condition,
            restocked=restock,
        )
        db.add(return_item)
        db.flush()

        if restock:
            batch = db.get(Batch, item.batch_id)
            if batch is None:
                raise ReturnError(
                    f"The batch behind {item.medicine_name_snapshot} is missing, so the "
                    f"stock cannot be put back.",
                    code="BATCH_MISSING",
                )
            inventory.post_movement(
                db,
                batch=batch,
                movement_type=MovementType.SALE_RETURN,
                qty_units_delta=qty,
                actor_id=user.id,
                reference_type=ReferenceType.RETURN_ITEM,
                reference_id=return_item.id,
                reason=f"Return {sale_return.return_no} against invoice {sale.invoice_no}",
            )
            # A return is not a fresh receipt, so it must not inflate the
            # quantity the batch claims to have received.
            batch.qty_received_units = quantize_qty(batch.qty_received_units - qty)
            any_restocked = True

        item.returned_qty_units = quantize_qty(item.returned_qty_units + qty)

        totals["gross"] += money.get("gross", ZERO)
        totals["discount"] += money.get("discount", ZERO)
        totals["taxable"] += money["taxable"]
        totals["cgst"] += money["cgst"]
        totals["sgst"] += money["sgst"]
        totals["igst"] += money["igst"]
        totals["refund"] += money["refund"]

    sale_return.gross_amount = quantize_money(totals["gross"])
    sale_return.discount_amount = quantize_money(totals["discount"])
    sale_return.taxable_amount = quantize_money(totals["taxable"])
    sale_return.cgst_amount = quantize_money(totals["cgst"])
    sale_return.sgst_amount = quantize_money(totals["sgst"])
    sale_return.igst_amount = quantize_money(totals["igst"])
    sale_return.total_refund_amount = quantize_money(totals["refund"])
    sale_return.restocked = any_restocked

    sale.status = _sale_status_after(sale)
    sale.updated_by_id = user.id
    db.flush()
    return sale_return


def settle(
    db: Session,
    *,
    sale_return: SaleReturn,
    sale: Sale,
    settings: StoreSettings,
    user: User,
):
    """Issue the credit note, or record that cash was handed back.

    Returns the note when one was raised, otherwise None.
    """
    if sale_return.refund_mode != RefundMode.CREDIT_NOTE.value:
        return None

    customer = db.get(Customer, sale.customer_id) if sale.customer_id else None
    if customer is None:
        raise ConflictError(
            "A credit note has to belong to someone. Record the customer's mobile "
            "number on the invoice first, or refund cash instead.",
            code="CREDIT_NOTE_NEEDS_CUSTOMER",
            field="refund_mode",
        )

    return credit_notes.issue(
        db,
        store_id=sale_return.store_id,
        customer=customer,
        amount=sale_return.total_refund_amount,
        settings=settings,
        sale_return_id=sale_return.id,
        actor_id=user.id,
        notes=f"Return {sale_return.return_no} against invoice {sale.invoice_no}",
    )


def _sale_status_after(sale: Sale) -> str:
    returned = sum((item.returned_qty_units for item in sale.items), ZERO_QTY)
    sold = sum((item.qty_units for item in sale.items), ZERO_QTY)
    if returned <= ZERO_QTY:
        return sale.status
    return (
        SaleStatus.RETURNED.value if returned >= sold else SaleStatus.PARTIALLY_RETURNED.value
    )
