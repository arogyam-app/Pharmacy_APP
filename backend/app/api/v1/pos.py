"""The price calculator behind the POS screen.

`POST /pos/quote` writes nothing. It is called on every cart change, so the
operator always sees the price that will actually be charged — the same
costing code runs again inside `POST /sales`.

Quoting is deliberately strict: an expired batch, a discount past the limit
or a scheduled drug without a doctor all fail *here*, at the keyboard,
rather than at the moment the customer is handing over money.
"""

from decimal import Decimal

from fastapi import APIRouter

from app.core.deps import CurrentSettings, CurrentUser, DbSession
from app.db.types import quantize_qty
from app.schemas.sale import QuotedLineOut, QuoteOut, QuoteRequest, TotalsOut
from app.services import inventory, sales
from app.utils.money_words import amount_in_words
from app.utils.clock import business_date

router = APIRouter()


@router.post("/quote", response_model=QuoteOut)
def quote(
    payload: QuoteRequest, user: CurrentUser, settings: CurrentSettings, db: DbSession
) -> QuoteOut:
    customer = sales.resolve_customer_readonly(
        db, store_id=user.store_id, payload=payload.customer
    )
    result = sales.build_quote(
        db,
        store_id=user.store_id,
        items=payload.items,
        settings=settings,
        customer=customer,
        doctor_id=payload.doctor_id,
        is_admin=user.is_admin,
        discount_override_reason=payload.discount_override_reason,
    )

    today = business_date()
    lines = []
    for line in result.lines:
        medicine, batch, price = line.medicine, line.batch, line.price
        # Shown next to the discount input so the operator can see the room
        # they have before the server refuses.
        _, ceiling = inventory.effective_discounts(batch, settings)
        lines.append(
            QuotedLineOut(
                line_no=line.line_no,
                medicine_id=medicine.id,
                medicine_name=medicine.brand_name,
                pack_label=medicine.pack_label,
                composition_text=(
                    medicine.composition.composition_text if medicine.composition else None
                ),
                batch_id=batch.id,
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                units_per_pack=batch.units_per_pack,
                qty_units=price.qty_units,
                qty_packs=quantize_qty(
                    price.qty_units / Decimal(batch.units_per_pack or 1)
                ),
                unit_mrp=price.unit_mrp,
                mrp_per_pack=batch.mrp_per_pack,
                gross_amount=price.gross_amount,
                discount_pct=price.discount_pct,
                max_discount_pct=ceiling,
                discount_amount=price.discount_amount,
                net_amount=price.net_amount,
                gst_rate=price.gst_rate,
                taxable_value=price.taxable_value,
                cgst_amount=price.cgst_amount,
                sgst_amount=price.sgst_amount,
                igst_amount=price.igst_amount,
                is_near_expiry=line.is_near_expiry,
                # Null for goods with no shelf life — there is no countdown.
                days_to_expiry=(
                    (batch.expiry_date - today).days if batch.has_expiry else None
                ),
            )
        )

    return QuoteOut(
        lines=lines,
        totals=TotalsOut(**{k: v for k, v in result.totals.as_dict().items()}),
        is_intra_state=result.is_intra_state,
        requires_doctor=result.requires_doctor,
        warnings=result.warnings,
        amount_in_words=amount_in_words(result.totals.net_amount),
    )
