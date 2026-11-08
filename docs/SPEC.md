# Pharmacy POS — Functional Specification

## 1. Purpose & scope

A web application to run the counter of a retail pharmacy: sell medicines, manage batch-wise inventory with expiry, handle returns and credit notes, and give the owner GST and movement reports.

- **Now:** one physical store, one admin, N sales executives.
- **Later (design for it, don't build it):** multiple stores, per-store stock and reporting, stock transfer between stores.
- Every business table therefore carries `store_id` (FK → `stores.id`, default store id `1`), and every query is scoped by the store on the caller's JWT.

## 2. Roles & permissions

### 2.1 Sales Executive
- Search medicines by **brand name** or **generic (salt) name**.
- When the search matched a brand, the UI must simultaneously show, **in a separate panel**, alternative medicines with the **same salt composition** (different brand/manufacturer) — each with its **pack MRP and price per sellable unit**, ordered cheapest-per-unit first, so the customer can be offered a cheaper equivalent. Comparing pack prices alone would mislead whenever pack sizes differ (a ₹14 strip of 10 beats a ₹27 strip of 15). Items with no price on record sort last.
- Add / remove / edit-quantity of cart lines.
- Sell in strips **and in loose units** — "1 tablet" is a valid sale line.
- Apply a discount per line, up to the maximum the admin has fixed for that medicine (falls back to the store-wide max). The server rejects anything higher.
- **Cannot see cost price, purchase margin, or supplier data** anywhere in the app or API.
- Complete the sale, collect payment in one or more modes (Cash / Card / UPI / Credit Note redemption), and print the invoice.
- After the invoice is generated the sale **cannot be deleted**. The executive may raise a *void request* ("wrong sale") with a reason; it only takes effect when an admin approves it.
- Record a sales return against an existing invoice (subject to the return window; a credit note is issued).
- See alerts: near-expiry stock, expired stock, low stock.
- View and reprint sales records (all users see the sales register).

### 2.2 Admin
Everything a sales executive can do, plus:
- Create/disable sales executive logins, reset passwords.
- Add & edit masters: medicines, salts/compositions, manufacturers, suppliers, doctors, customers.
- Record purchases (goods inward) — creates batches with cost price, MRP, batch no., expiry, inward GST.
- Set **per-medicine default discount % and maximum discount %**; set the store-wide defaults (**default/minimum 10 %, maximum 20 %**).
- Approve or reject void requests; approve returns beyond policy.
- Write off expired stock into the expiry register; mark it returned to supplier.
- See cost price, margin, and all reports (GST, fast/slow moving, order book, profitability).
- Change store settings (GST mode, near-expiry window, low-stock rule, invoice header/footer, printer format).

### 2.3 Common to all authenticated users
- Sales register listing **invoice no · customer mobile · date · amount** per row, with status badges.
- Search the register by **invoice number** or **mobile number** (also by date range, customer name, executive).
- Reprint any invoice (reprints are counted and stamped `REPRINT`).

## 3. Core domain rules

### 3.1 Units of measure
- A pack is described by **`pack_unit`** (TABLET, CAPSULE, ML, GM, IU, VIAL) and **`pack_size`**. The dosage form decides which units are legal — a syrup is measured in ml, an ointment in grams, an injection in ml or vials. "Units per strip" is a tablet/capsule idea and is never asked for on the others.
- **`units_per_pack` is derived, never entered:** `pack_size` for tablets and capsules, `1` for everything else. A 100 ml syrup is **one** sellable unit, not 100 — otherwise the POS would offer to sell a single millilitre.
- **Inventory is stored in these units.** `packs × units_per_pack + loose_units = total units`.
- MRP is captured **per pack** (as printed). Per-unit MRP = `mrp_per_pack / units_per_pack`, kept at 4 decimals; line amounts round to 2.
- A cart line stores `qty_units`; for countable forms the UI lets the operator type either strips or loose units and shows both.

### 3.2 Batches & allocation
- Stock lives in **batches** — same medicine, different batch no., expiry, MRP and cost.
- Default allocation is **FEFO (First Expiry, First Out)**; the operator may override and pick a specific batch.
- A single cart line may be split across batches when one batch can't cover the quantity; each split is its own `sale_item` (same MRP shown to the customer only if MRPs match — if they differ, the UI shows separate lines).
- **Expired batches can never be sold** — hard block, no override.
- Blocked/quarantined batches (`is_blocked`) are excluded from sale.

### 3.3 Pricing & discount
```
line_gross     = qty_units × unit_mrp
discount_amt   = round(line_gross × discount_pct / 100, 2)
line_net       = line_gross − discount_amt          # GST-inclusive
taxable_value  = round(line_net / (1 + gst_rate/100), 2)
gst_amount     = line_net − taxable_value
    intra-state → cgst = sgst = gst_amount / 2
    inter-state → igst = gst_amount
invoice_total  = Σ line_net  → round_off to nearest ₹1 → payable
```
- MRP is treated as **GST-inclusive** (Indian retail pharma norm). This is configurable per store (`settings.price_includes_gst`).
- Discount resolution order for a line: `batches.max_discount_pct` → `settings.max_discount_pct` (default 20). Suggested discount: `batches.default_discount_pct` → `settings.default_discount_pct` (default 10). The suggestion is clamped to the ceiling. Limits sit on the batch alongside MRP and GST because all three move together when a new consignment arrives — and a near-expiry lot is often cleared at a deeper discount than fresh stock.
- An admin may override up to 100 % but must supply a reason (logged).
- Server-side validation error code: `DISCOUNT_LIMIT_EXCEEDED`.

### 3.4 GST — "GST agnostic"
The app **records** GST everywhere but never forces a tax regime on the seller:
- `settings.gst_enabled` (bool), `settings.gst_registration_type` (`regular` | `composition` | `unregistered`), store GSTIN, state code.
- Each medicine carries `hsn_code` and `gst_rate` (0 / 5 / 12 / 18 / 28); the **rate in force at transaction time is snapshotted** onto every purchase item, sale item and return item — later master changes never rewrite history.
- **Inward GST** is captured on purchase invoices (supplier GSTIN, taxable, CGST/SGST/IGST, invoice no & date).
- **Outward GST** is captured on every sale/credit note.
- Intra-state vs inter-state is decided by comparing the store's state code with the counter-party's.
- When `gst_enabled = false` the UI hides tax columns but the columns are still stored (zero rate), so a later switch-on loses nothing.

### 3.5 Invoice numbering
- 8-digit numeric, zero-padded, gap-free, allocated from a `document_sequences` row locked inside the sale transaction (e.g. `00000001`).
- Separate sequences per store and per document type (`SALE`, `RETURN`, `CREDIT_NOTE`, `PURCHASE`).
- Optional non-numeric display prefix lives in settings and is **not** part of the stored number.

### 3.6 Sale lifecycle
```
DRAFT ──confirm──▶ COMPLETED ──void request──▶ VOID_REQUESTED ──admin approve──▶ VOIDED
                        │                              └──admin reject──▶ COMPLETED
                        └──return──▶ PARTIALLY_RETURNED / RETURNED
```
- `COMPLETED` is reached the moment the invoice number is issued; stock is decremented then.
- `VOIDED` returns every unit to its original batch and reverses the ledger; the invoice number is retained and marked void (never reused).
- A sale with returns against it cannot be voided; void it by reversing the returns first.
- Deleting any of these rows is not implemented anywhere in the codebase.

### 3.7 Returns & credit notes
- Return is always **against an existing invoice** (`sale_id` + `sale_item_id`), quantity ≤ sold minus already-returned.
- Return window: `settings.return_window_days` (default 7); beyond it, only an admin can approve.
- Returned units go **back into the original batch** unless flagged damaged/expired → then into the expiry/damage register instead of saleable stock.
- Refund value = the line's net (post-discount) value pro-rata; GST is reversed on the same snapshotted rate.
- Settlement: **cash refund** or **credit note**. Default is a credit note.
- A credit note has `amount_issued`, `amount_used`, `balance`, `expires_on` (`settings.credit_note_validity_days`, default 180), status `OPEN | PARTIALLY_USED | FULLY_USED | EXPIRED | CANCELLED`.
- On a later sale, an open credit note for that customer is offered as a payment mode; redemption is atomic and creates a `credit_note_usage` row. Over-redemption is impossible (`CREDIT_NOTE_INSUFFICIENT_BALANCE`).
- Credit notes are customer-scoped (matched by mobile number); an anonymous sale's credit note requires a mobile number before it can be issued.

### 3.8 Alerts (badge counts + a dedicated Alerts page)
| Alert | Rule | Config |
|---|---|---|
| Expired | `expiry_date < today` and `qty_available > 0` | — |
| Near expiry | `expiry_date <= today + near_expiry_days` | `near_expiry_days`, default 90 |
| Low stock | total available units ≤ `medicines.reorder_level_units` | per medicine; fallback `settings.low_stock_units` |
| Out of stock | available = 0 but the medicine is active | — |
| Order book | see 3.9 | `order_book_decline_pct`, default 60 |

Sold-out and expiring stock must also warn **inside the POS screen** when the operator adds such a line (near-expiry warning is a confirm; expired is a block).

### 3.9 Order book (auto purchase suggestion)
- Each medicine keeps a `stock_baseline_units` — the total available units immediately after the most recent goods-inward for that medicine.
- When `available_units <= stock_baseline_units × (1 − order_book_decline_pct/100)` (i.e. stock has declined by 60 %), or when available ≤ reorder level, an **open order-book entry** is created (or its suggested quantity refreshed).
- Suggested qty = `max(baseline − available, reorder_level − available)`, rounded up to a whole strip/pack.
- Entry carries the preferred supplier (last supplier for that medicine), average monthly consumption (last 90 days), and days-of-cover.
- Statuses: `OPEN → ORDERED → RECEIVED / CANCELLED`. Receiving a purchase that covers the medicine auto-closes matching `ORDERED` entries and resets the baseline.
- Admin can export the order book grouped by supplier as CSV/XLSX.

### 3.10 Expiry register
- Batches crossing expiry appear in the expiry register automatically (a nightly job / on-demand recompute).
- Admin actions per entry: `WRITE_OFF` (loss booked, stock zeroed via ledger) or `RETURNED_TO_SUPPLIER` (with debit-note ref, supplier, credit expected).
- The register keeps medicine, batch, qty, cost value, MRP value, expiry date, action, action date, actor.

## 4. Screens (Vue SPA)

1. **Login** — email + password, JWT stored in memory + refresh in httpOnly-ish storage; role drives the menu.
2. **POS / New Sale** *(default landing page for sales exec)* — keyboard-first:
   - search box (debounced, ≥2 chars, brand **or** salt), results grid with brand · salt · manufacturer · MRP · available units · nearest expiry;
   - **"Same-salt alternatives" side panel** driven by the highlighted result;
   - cart table with qty (strips + units), MRP, discount %, amount, batch picker;
   - customer strip (mobile lookup → autofill / quick-create), doctor picker;
   - totals card: gross, discount, taxable, CGST/SGST/IGST, round-off, payable;
   - payment dialog with split modes + credit-note redemption;
   - `F2` search, `F4` customer, `F9` pay, `Esc` clear line — shortcuts shown in a help strip.
3. **Sales Register** — searchable list (invoice no / mobile / date range / status), row = invoice no, mobile, date-time, amount, status; actions: view, reprint, return, request void.
4. **Sale Detail / Invoice** — full document, print view (A4 letterhead, 80 mm thermal, A5), reprint counter.
5. **Returns** — pick invoice → choose lines & quantities → refund mode → credit note preview.
6. **Inventory** *(admin)* — medicine list with total stock, batch drill-down, blocked flag, stock adjustment with reason.
7. **Purchases / Goods Inward** *(admin, or a sales executive granted the Purchases module — who books the delivery in but sees no cost, taxable or invoice-total figure)* — supplier, invoice no/date, line items (batch, expiry, qty, free qty, cost, MRP, GST), totals reconciliation.
8. **Masters** *(admin)* — medicines, salts/compositions, manufacturers, suppliers, doctors, customers; CSV/Excel bulk upload for the medicine and composition masters, each with a downloadable template.
9. **Alerts** — the five buckets from 3.8 as tabs.
10. **Order Book** — open suggestions, group by supplier, mark ordered/received, export.
11. **Reports** — see §5.
12. **Admin / Settings** — users, discount policy, GST settings, store profile, invoice branding, thresholds.

## 5. Reports (admin, all exportable to CSV/XLSX, all date-range filtered)

| Report | Contents |
|---|---|
| **GST outward (sales) summary** | Per GST rate: taxable, CGST, SGST, IGST, total; plus B2C/B2B split by customer GSTIN |
| **GST inward (purchase) summary** | Per supplier & rate: taxable, tax, invoice refs — purchase register |
| **GST net liability** | Outward tax − inward credit, per rate, per month |
| **Daily sales / day-book** | Invoice-wise and payment-mode-wise collection, discounts given, returns, net cash |
| **Fast moving inventory** | Top N by units sold in period, with turnover ratio and days-of-cover |
| **Slow / non-moving inventory** | Zero or below-threshold sales in period, value locked, days since last sale |
| **Stock on hand / valuation** | Batch-wise qty, cost value, MRP value, expiry bucket |
| **Expiry ageing** | Buckets: expired, ≤30, 31–60, 61–90, 91–180 days, with value at risk |
| **Sales by executive** | Invoices, value, average basket, discount given, void/return rate |
| **Profitability** | Sale value vs cost value → gross margin by medicine / manufacturer / period *(admin only)* |
| **Customer / doctor analysis** | Top customers, repeat rate; prescriptions by doctor |
| **Credit note ledger** | Issued, used, balance outstanding, expiring soon |
| **Void & audit report** | Void requests, approvers, reasons; discount overrides |

## 6. Printing

- Server returns JSON; **the SPA renders the invoice** with a print stylesheet. Three layouts: **A4 letterhead (default)** — shop logo, partner logo, licence and GST numbers in the header, an `# / Product / HSN no. / Batch / Qty / MRP / GST / Amount` table, SGST+CGST beside the gross/discount/net block, the amount in words, and the order QR — plus 80 mm thermal and A5.
- Invoice content: store name, address, GSTIN, DL numbers, invoice no, date-time, customer name/mobile, doctor, line items (brand, batch, exp, qty, MRP, disc, amount), tax break-up per rate, total in words, payment modes, executive name, footer/terms, `REPRINT` watermark when `reprint_count > 0`.
- Optional later: server-side PDF for emailing/WhatsApp.

## 7. Non-functional

- **Security:** argon2 password hashing, JWT (access 30 min / refresh 7 days), role checks as FastAPI dependencies on *every* route, rate-limited login, no cost data in non-admin responses, parameterised queries only.
- **Auditability:** `audit_log` on every mutation; financial rows immutable.
- **Performance:** medicine search must respond < 150 ms for a 50 k-row master — index `brand_name`, `salt_name`, and use a `search_text` column; POS list endpoints paginated.
- **Offline/robustness:** POS cart is persisted in localStorage so a refresh doesn't lose the basket; the confirm-sale call is **idempotent** via a client-generated `idempotency_key` so a double-click can never create two invoices.
- **Data safety:** nightly SQLite file backup (dev) / `pg_dump` (prod) script in `backend/scripts/`.
- **Portability:** no SQLite-specific SQL; `DATABASE_URL` env var switches to Postgres with zero code change.
