# Pharmacy POS — Data Model

SQLAlchemy 2.0 declarative models, Alembic-migrated, portable between SQLite (dev) and PostgreSQL (prod).

## Global conventions

- PK: `id` — `UUID` (RFC 9562 **version 7**), generated in Python by `app.db.types.uuid7`. Not autoincrement: the www app writes to Supabase while the counter writes here, and two databases handing out the same integer is silent corruption at the next sync.
- Every business table: `store_id UUID NOT NULL REFERENCES stores(id)` — the multi-store seam.
- Audit columns on every table: `created_at`, `updated_at` (timezone-aware UTC, server default `now()`), `created_by_id`, `updated_by_id` → `users.id`.
- Soft delete only on **masters**: `is_active BOOLEAN NOT NULL DEFAULT true`. Transactions are never deleted or soft-deleted.
- Money `Numeric(12,2)`; per-unit prices `Numeric(12,4)`; percentages/rates `Numeric(6,2)`; quantities in **units** as `Numeric(12,3)` (allows a half tablet if ever needed).
- Enums stored as `String` with a `CHECK` constraint + Python `enum.StrEnum` (portable, migration-friendly).

---

## 1. Tenancy, users, settings

### `stores`
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| name | String(150) | |
| legal_name, address_line1/2, city, state, state_code, pincode | String | `state_code` drives intra/inter-state GST |
| gstin | String(15) | nullable |
| drug_licence_no_20b, drug_licence_no_21b | String(50) | |
| phone, email | String | |
| is_active | Bool | |

### `users`
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| store_id | FK stores | |
| full_name | String(120) | |
| email | String(160) | **UNIQUE**, login id |
| phone | String(15) | |
| password_hash | String(255) | argon2 |
| role | Enum `ADMIN` \| `SALES_EXEC` | |
| is_active | Bool | disabled users can't log in |
| last_login_at | DateTime | |
| must_change_password | Bool | true for admin-created accounts |

`refresh_tokens`: id, user_id, token_hash, expires_at, revoked_at, user_agent.

### `settings` (one row per store, or key/value — use one typed row per store)
`store_id` (unique), `default_discount_pct` (10.00), `max_discount_pct` (20.00), `gst_enabled` (true), `gst_registration_type`, `price_includes_gst` (true), `near_expiry_days` (90), `low_stock_units` (10), `order_book_decline_pct` (60), `return_window_days` (7), `credit_note_validity_days` (180), `invoice_prefix` (display only), `invoice_footer_text`, `print_format` (`A4` \| `THERMAL_80` \| `A5`, default `A4`), `allow_negative_stock` (false), `round_off_enabled` (true), and the letterhead branding `logo_image`, `partner_logo_image`, `order_qr_image` (data URIs or URLs, ≤512 KB of text each) and `order_qr_caption`.

> A CHECK constraint lists the allowed `print_format` values by name, so **adding a format needs a migration that rebuilds the constraint**, not just a new enum member — the tests build their tables from the model metadata and would not notice. `tests/test_migrations.py` guards this by inspecting a database that has actually been migrated.

### `audit_log`
id, store_id, user_id, action (`CREATE|UPDATE|DELETE|LOGIN|APPROVE|REJECT|VOID|PRINT|OVERRIDE`), entity_type, entity_id, before_json (Text), after_json (Text), reason (Text), ip_address, created_at.
Index: `(entity_type, entity_id)`, `(store_id, created_at)`.

### `document_sequences`
store_id, doc_type (`SALE|RETURN|CREDIT_NOTE|PURCHASE|ADJUSTMENT`), next_number BIGINT. **UNIQUE(store_id, doc_type)**. Incremented under row lock inside the transaction → gap-free 8-digit numbers.

Each series opens where `services.sequences.starting_number()` says. **`SALE` opens at `settings.invoice_number_start` (default `10000001`)** — a shop moving off a paper book cannot restart at 1 beside numbers customers already hold. Every other series is an internal document and starts at 1. The setting is read only when the row is created, by the one-time seed; changing it later does nothing, because a gap-free counter carries on from where it is.

---

## 2. Product catalogue

### `salts`
id, name (String(255), **UNIQUE**), strength_hint, is_active.
*A single active ingredient, e.g. "Paracetamol".*

### `compositions`
id, composition_text (String(400), **UNIQUE** — canonical, sorted, e.g. `AMOXICILLIN 500MG + CLAVULANIC ACID 125MG`), therapeutic_class (String(150)), notes, is_active.
*The full combination. **Two medicines are alternatives of each other iff they share `composition_id`.***

### `composition_salts` (M:N)
composition_id FK, salt_id FK, strength (String(50)), UNIQUE(composition_id, salt_id).
*Enables searching by any single generic name inside a combination.*

### `manufacturers`
id, name (**UNIQUE**), address, phone, email, is_active.

### `medicines`
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| store_id | FK | |
| brand_name | String(255) | indexed |
| manufacturer_id | FK manufacturers | |
| item_type | Enum `MEDICINE\|GENERAL`, indexed | migration 0016. A pharmacy also sells soap, nappies, a thermometer: same batch, MRP, GST rate and place on the bill, none of the pharmacological attributes. One catalogue with two kinds of row is what lets a mixed basket bill as one invoice |
| composition_id | FK compositions, **nullable** | **the alternative-suggestion key**. NULL only for a general item — soap has no salt. CHECK `item_type <> 'MEDICINE' OR composition_id IS NOT NULL` |
| dosage_form | Enum `TABLET\|CAPSULE\|SYRUP\|INJECTION\|OINTMENT\|DROPS\|POWDER\|OTHER\|GENERAL` | `GENERAL` is not a dosage form — it is the placeholder a general item carries so pack resolution, which keys off this column, still has something to work with |
| pack_unit | Enum `TABLET\|CAPSULE\|ML\|GM\|IU\|VIAL\|UNIT\|PIECE` | how the pack is measured; must suit the dosage form (`PACK_UNITS_BY_FORM`) |
| pack_size | Numeric(12,3) | 10 tablets, 100 ml, 21.8 g, 12 soaps |
| pack_label | String(60) | e.g. "10 tab strip", "100 ml bottle" — auto-composed when blank |
| units_per_pack | Integer, default 1 | **derived**: `pack_size` when the pack unit is countable (`TABLET`, `CAPSULE`, `PIECE`), else 1 |

> **Countability follows the pack *unit*, not the dosage form.** A box of twelve soaps is twelve things a customer can buy one of, exactly as a strip of ten tablets is ten — so `PIECE` is countable and a general item gets `units_per_pack = pack_size`. A general item measured in ml or grams (a 500 ml bottle of floor cleaner) falls through to 1, by the same rule that stops the POS selling a millilitre of syrup.

> **No *billing* money on the medicine master.** MRP, GST rate and the discount limits live on `batches` — every consignment arrives with its own printed price and whatever tax slab applies that day, so holding one figure on the master would be a lie the moment a second batch lands. `hsn_code` stays here: it is a stable product classification. A medicine's *displayed* price is resolved from the batch FEFO would sell next, and is `null` when nothing sellable is in stock.
>
> The one exception is `list_mrp_per_pack` (migration 0009): the figure a manufacturer's price list quotes, carried by the bulk price-list upload. It is a **reference only** — nothing in pricing reads it, and the POS still refuses to sell a medicine with no stock. It exists so a catalogue loaded before the first delivery has a price to show, and so the MRP field can be seeded when a batch is entered. It is deliberately *not* the `mrp_per_pack` dropped in migration 0004.
| schedule_type | Enum `OTC\|SCHEDULE_H\|SCHEDULE_H1\|SCHEDULE_X\|OTHER` | H1 ⇒ prescriber mandatory |
| hsn_code | String(10) | |
| list_mrp_per_pack | Numeric(12,2) nullable | the price list's MRP; **reference only, never bills** |
| gst_rate | Numeric(6,2) default 12.00 | current master rate |
| default_discount_pct | Numeric(6,2) nullable | falls back to settings |
| max_discount_pct | Numeric(6,2) nullable | falls back to settings |
| reorder_level_units | Numeric(12,3) nullable | low-stock trigger |
| stock_baseline_units | Numeric(12,3) default 0 | set at each goods-inward; order-book reference |
| rack_location | String(50) | |
| is_prescription_required | Bool | |
| search_text | String(600), indexed | `brand_name + composition_text + manufacturer` lowercased, refreshed on save |
| is_active | Bool | |

Indexes: `(store_id, brand_name)`, `(store_id, composition_id)`, `search_text`.
UNIQUE: `(store_id, brand_name, manufacturer_id, pack_label)`.

---

## 3. Parties

### `customers`
id, store_id, mobile (String(15), **UNIQUE per store**, the lookup key), name, address, email, date_of_birth, gstin (nullable, for B2B), state_code, credit_balance `Numeric(12,2)` (cached sum of open credit notes), is_active.

### `doctors`
id, store_id, name, registration_no, specialization, hospital_clinic, address, phone, is_active. Index `(store_id, name)`.

### `suppliers`
id, store_id, name, gstin, state_code, address, phone, email, contact_person, payment_terms_days, is_active.

---

## 4. Purchases (goods inward) & inventory

### `purchase_invoices`
id, store_id, supplier_id FK, supplier_invoice_no, supplier_invoice_date, received_date, doc_number (internal 8-digit), subtotal_taxable, discount_amount, cgst_amount, sgst_amount, igst_amount, other_charges, round_off, total_amount, payment_status (`UNPAID|PARTIAL|PAID`), notes, is_intra_state Bool, is_cancelled Bool, cancelled_at, cancelled_by_id FK, cancel_reason.
UNIQUE `(store_id, supplier_id, supplier_invoice_no)` — this is what stops one consignment being received twice. UNIQUE `(store_id, doc_number)`.

A posted purchase is **not** editable: only `payment_status` and `notes` can change. A mistake is *cancelled* — the stock is reversed through fresh `PURCHASE_RETURN` ledger rows while the invoice and its figures stay on record — and re-entered. Cancelling is refused once any unit has been sold from the batches it created.

### `purchase_items`
id, purchase_invoice_id FK, medicine_id FK, **batch_id FK nullable**, batch_no, expiry_date (Date), qty_packs, free_qty_packs, units_per_pack *(snapshot)*, qty_units *(total incl. free)*, cost_per_pack, mrp_per_pack, discount_pct *(trade discount)*, gst_rate *(snapshot)*, taxable_value, discount_amount, cgst_amount, sgst_amount, igst_amount, line_total.

→ Creates one `batches` row, **or tops up an existing one** when the same batch number and expiry arrives again. That is why the FK runs line → batch rather than batch → line: several invoice lines can feed one batch. A repeat lot keeps the commercial terms it was opened with; a differing MRP is returned as a warning, not applied to stock already on the shelf.

**Purchase line arithmetic** — a distributor bills GST-*exclusive*, unlike retail MRP which is GST-inclusive:

```
gross         = cost_per_pack × qty_packs        # free goods are not billed
taxable_value = gross − trade discount
gst_amount    = round(taxable_value × gst_rate/100, 2)
    intra-state → cgst = sgst = gst_amount / 2   # odd paise go to CGST
    inter-state → igst = gst_amount
```

`batches.cost_per_pack` is the **landed** cost, not the invoice rate: the taxable value spread over the whole quantity *including* free goods, plus the input GST only when the store cannot claim it back (`settings.gst_registration_type ≠ REGULAR`). Ten strips plus two free at ₹100 land at ₹83.33, and recording ₹100 would overstate cost on every later margin report.

### `batches` — **the inventory table**
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| store_id | FK | |
| medicine_id | FK | indexed |
| batch_no | String(60) | |
| expiry_date | Date, NOT NULL | indexed. **Always set** — see the note below |
| expiry_basis | Enum `EXPIRY_DATE\|BEST_BEFORE\|NONE` | migration 0016; where `expiry_date` came from |
| mfg_date | Date nullable | set only for `BEST_BEFORE` |
| best_before_months | Integer nullable | set only for `BEST_BEFORE`; `expiry_date = mfg_date + n months` |
| pack_unit / pack_size | String(20) / Numeric(12,3) | snapshot of the pack at receipt |
| units_per_pack | Integer | snapshot; the only pack→unit conversion |
| qty_received_units | Numeric(12,3) | |
| qty_available_units | Numeric(12,3) | **cached projection of `stock_ledger`** |
| mrp_per_pack | Numeric(12,2) | |
| unit_mrp | Numeric(12,4) | `mrp_per_pack / units_per_pack`, stored |
| cost_per_pack | Numeric(12,2) | **admin-only in all API responses**; landed cost |
| unit_cost | Numeric(12,4) | admin-only |
| gst_rate | Numeric(6,2) | snapshot |
| default_discount_pct / max_discount_pct | Numeric(6,2) nullable | NULL falls back to store policy |
| supplier_id | FK nullable | who supplied the lot; null for opening stock |
| received_date | Date | |
| is_blocked | Bool | quarantine → excluded from sale |
| block_reason | String(200) | |

UNIQUE `(store_id, medicine_id, batch_no, expiry_date)`.
Index `(store_id, medicine_id, expiry_date)` — drives FEFO.

> **Three ways a pack states its shelf life, one column to query.** A medicine box prints an expiry. A bottle of shampoo prints "MFG 03/2026, best before 24 months" and the shelf date is arithmetic. A comb prints neither and never goes off. `expiry_basis` records which of the three it was, and `expiry_date` always holds the resolved date, because FEFO ordering, the sellable-batch filter, the near-expiry alert and the expiry register are all built on that one column — teaching each of them what a NULL means would be four chances to hide sellable stock or offer expired stock.
>
> Stock with no shelf life carries the sentinel **9999-12-31** (`enums.NO_EXPIRY`). It sorts last under FEFO, passes every "not yet expired" test and matches no near-expiry window, which is exactly the wanted behaviour. `Batch.has_expiry` is the property to read; API responses carry it, and nothing prints the sentinel. Resolution lives in one place: `services.inventory.resolve_expiry()`.

### `stock_ledger` — append-only, the single source of truth
id, store_id, medicine_id, batch_id, movement_type (`PURCHASE | SALE | SALE_RETURN | PURCHASE_RETURN | VOID_REVERSAL | ADJUSTMENT_IN | ADJUSTMENT_OUT | EXPIRY_WRITE_OFF | OPENING | TRANSFER_IN | TRANSFER_OUT`), qty_units_delta `Numeric(12,3)` (**signed**: + in, − out), balance_after_units, reference_type (`SALE|SALE_ITEM|PURCHASE_ITEM|RETURN_ITEM|ADJUSTMENT|EXPIRY`), reference_id, reason, created_at, created_by_id.
Index `(batch_id, created_at)`, `(store_id, medicine_id, created_at)`.
**Invariant (tested):** `batches.qty_available_units == SUM(stock_ledger.qty_units_delta WHERE batch_id = …)`.

### stock adjustments
No separate table: an adjustment **is** a `stock_ledger` row of type `ADJUSTMENT_IN` / `ADJUSTMENT_OUT` with a mandatory `reason`, posted through `POST /inventory/batches/{id}/adjust`. A parallel table would only duplicate the ledger it has to write anyway, and give two places to disagree about the same correction.

---

## 5. Sales

### `sales`
| column | type | notes |
|---|---|---|
| id | UUID PK | |
| store_id | FK | |
| invoice_no | String(8) | zero-padded 8-digit, **UNIQUE per store**, from `document_sequences` |
| invoice_date | Date, indexed | |
| invoice_datetime | DateTime(tz) | |
| customer_id | FK nullable | null = walk-in |
| customer_mobile_snapshot | String(15), indexed | so register search works for walk-ins |
| customer_name_snapshot | String(150) | |
| doctor_id | FK nullable | prescribed by |
| prescription_ref | String(100) | |
| sales_user_id | FK users | who billed |
| gross_amount | Numeric(12,2) | Σ qty × unit_mrp |
| discount_amount | Numeric(12,2) | |
| taxable_amount | Numeric(12,2) | |
| cgst_amount / sgst_amount / igst_amount | Numeric(12,2) | |
| round_off | Numeric(12,2) | |
| net_amount | Numeric(12,2) | payable |
| paid_amount | Numeric(12,2) | Σ payments |
| status | Enum `DRAFT\|COMPLETED\|VOID_REQUESTED\|VOIDED\|PARTIALLY_RETURNED\|RETURNED` | |
| is_intra_state | Bool | |
| reprint_count | Integer default 0 | |
| idempotency_key | String(64), UNIQUE | blocks duplicate submits |
| notes | Text | |

Indexes: `(store_id, invoice_no)`, `(store_id, customer_mobile_snapshot)`, `(store_id, invoice_date)`.

### `sale_items`
id, sale_id FK, medicine_id FK, batch_id FK, **medicine_name_snapshot**, batch_no_snapshot, expiry_date_snapshot, units_per_strip_snapshot, qty_units `Numeric(12,3)`, unit_mrp `Numeric(12,4)`, mrp_per_strip, gross_amount, discount_pct, discount_amount, net_amount, gst_rate *(snapshot)*, taxable_value, cgst_amount, sgst_amount, igst_amount, returned_qty_units (default 0), unit_cost_snapshot *(admin-only; enables margin reports)*, line_no.

### `payments`
id, sale_id FK, mode (`CASH|CARD|UPI|NET_BANKING|CREDIT_NOTE|OTHER`), amount, reference_no (txn/UTR/last-4), credit_note_id FK nullable, received_at, received_by_id.
*A sale may have several rows — split payment. Σ amounts must equal `net_amount`.*

### `sale_void_requests`
id, sale_id FK (unique per open request), requested_by_id, request_reason (required), requested_at, status (`PENDING|APPROVED|REJECTED`), reviewed_by_id, review_note, reviewed_at.
Approval → reverse ledger, restore `batches.qty_available_units`, set `sales.status = VOIDED`.

---

## 6. Returns & credit notes

### `sale_returns`
id, store_id, return_no (8-digit), sale_id FK, return_date, reason, refund_mode (`CASH|CREDIT_NOTE|CARD_REVERSAL`), gross_amount, discount_amount, taxable_amount, cgst/sgst/igst_amount, total_refund_amount, processed_by_id, approved_by_id (nullable — required outside the return window), status (`COMPLETED|PENDING_APPROVAL|REJECTED`), restock (Bool).

### `sale_return_items`
id, sale_return_id FK, sale_item_id FK, medicine_id, batch_id, qty_units, unit_mrp, discount_pct, gst_rate, taxable_value, cgst/sgst/igst_amount, refund_amount, condition (`SALEABLE|DAMAGED|EXPIRED`).
`SALEABLE` → ledger `SALE_RETURN` (+qty) into the original batch; otherwise → `expiry_register`.

### `credit_notes`
id, store_id, credit_note_no (8-digit), customer_id FK, sale_return_id FK, issue_date, amount_issued, amount_used (default 0), balance_amount, expires_on, status (`OPEN|PARTIALLY_USED|FULLY_USED|EXPIRED|CANCELLED`), notes.

### `credit_note_usages`
id, credit_note_id FK, sale_id FK, payment_id FK, amount_used, used_at, used_by_id.
**Invariant:** `amount_used == Σ usages`, `balance == issued − used ≥ 0`.

---

## 7. Expiry, order book

### `expiry_register`
id, store_id, medicine_id, batch_id, batch_no, expiry_date, qty_units, cost_value *(admin-only)*, mrp_value, source (`EXPIRED_STOCK|SALE_RETURN_EXPIRED|DAMAGED`), status (`IDENTIFIED|WRITTEN_OFF|RETURNED_TO_SUPPLIER|CREDIT_RECEIVED`), action_date, supplier_id, debit_note_ref, credit_received_amount, actioned_by_id, notes.

### `order_book`
id, store_id, medicine_id FK, trigger_reason (`STOCK_DECLINE_60|BELOW_REORDER_LEVEL|OUT_OF_STOCK|MANUAL`), baseline_units, available_units_at_trigger, suggested_qty_units, suggested_qty_strips, avg_monthly_consumption_units, days_of_cover, preferred_supplier_id FK nullable, last_purchase_cost *(admin-only)*, status (`OPEN|ORDERED|RECEIVED|CANCELLED`), ordered_at, purchase_invoice_id FK nullable, closed_at, notes.
UNIQUE partial: one `OPEN` row per `(store_id, medicine_id)`.

---

## 8. Derived / helper views (define as SQL views or service queries)

- `v_medicine_stock` — medicine_id, total_available_units, batch_count, nearest_expiry_date, min/max unit_mrp.
- `v_sales_register` — invoice_no, invoice_datetime, customer_mobile, net_amount, status, sales_user (backs the register grid).
- `v_gst_outward_summary` — invoice_date, gst_rate, taxable, cgst, sgst, igst.

## 9. Seed data (`app/db/seed.py`)

1. Store #1 from `.env`.
2. Admin user (email/password from `.env`, `must_change_password = true`).
3. Settings row with the defaults in §1 of this doc.
4. `document_sequences` rows starting at 1 for every doc type.
5. ~30 demo medicines across ~12 compositions (deliberately including 3+ brands sharing a composition so the alternatives panel is demonstrable), 5 manufacturers, 2 suppliers, 3 doctors, sample opening-stock batches with varied expiries (one already expired, one within 30 days) so alerts light up.

## 10. Migration to PostgreSQL

- Keep `DATABASE_URL` the only switch (`sqlite:///./pharmacy.db` → `postgresql+psycopg://…`).
- **Money/quantity columns use `app.db.types.PreciseDecimal`** (`Money()`, `UnitPrice()`, `Quantity()`, `Rate()`), never bare `Numeric` — SQLite would round-trip those through float and lose paise. It stores scaled integers on SQLite and real `NUMERIC` on PostgreSQL, so `ORDER BY`, comparisons and `SUM()` stay exact on both.
- **Primary and foreign keys use `app.db.types.UUIDPK()`** — native `uuid` on PostgreSQL, `CHAR(32)` on SQLite. It also coerces the string form, because an id makes a round trip through JSON and comes back as text.
- **Keys are UUIDv7, not v4.** The high bits are a millisecond timestamp, so consecutive inserts still land next to each other in the btree — a random v4 key would scatter every insert across the index and undo the work the indexes are there to do.
- **Document numbers are not keys and are not UUIDs.** `invoice_no`, `return_no` and `credit_note_no` stay a gap-free consecutive series from `document_sequences`: CGST Rule 46(b) requires a consecutive serial number of at most sixteen characters, and a customer has to be able to read it out.
- Avoid: string-concat date maths, `GROUP BY` on non-aggregated columns, boolean-as-integer comparisons.
- Use `sqlalchemy.func` (`func.now()`, `func.date_trunc` guarded per-dialect) rather than literal SQL functions.
- `SELECT … FOR UPDATE` is a no-op on SQLite: wrap batch allocation and sequence issue in a service-level lock so the Postgres path is already correct.
