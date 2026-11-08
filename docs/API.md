# Pharmacy POS — REST API

Base: `/api/v1` · JSON only · JWT bearer auth · roles: **A** = admin only, **S** = sales exec + admin.

Standard list query: `?page=1&page_size=25&q=&sort=-created_at&date_from=&date_to=`
Standard list response: `{"items":[…],"total":0,"page":1,"page_size":25}`
Standard error: `{"detail":{"code":"MACHINE_CODE","message":"Human text","field":"optional"}}`

---

## Auth
| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/auth/login` | — | `{email, password, remember_me?}` → `{access_token, refresh_token, user}`, plus the session cookie |
| POST | `/auth/refresh` | — | rotate refresh token; reads the cookie, or `{refresh_token}` for API clients |
| POST | `/auth/logout` | S | revoke the refresh token and clear the cookie |
| GET | `/auth/me` | S | current user + role + store + effective `permissions` |
| POST | `/auth/change-password` | S | `{current_password, new_password}` |

### Staying signed in

**No password is ever stored by the client, the server, or a cookie.** A password
exists in one form only — an argon2 hash in `users.password_hash` — and there is no
code path that turns one back into text. "Stay signed in" is done with a session
token, which unlike a password can be revoked, rotated and expired.

`POST /auth/login` sets `pharmacy_session`, an **httpOnly, SameSite=Lax** cookie
scoped to `/api/v1/auth`, holding the refresh token:

* **httpOnly** — script cannot read it. This is why the token no longer lives in
  `localStorage`, where any injected script could lift a week-long session.
* **SameSite=Lax** — withheld on cross-site POSTs, which keeps CSRF off `/auth/refresh`.
* **Secure** — set `COOKIE_SECURE=true` wherever the app is served over HTTPS. It
  defaults to `false` only so the plain-http dev setup works; a `Secure` cookie is
  silently dropped on an http origin, which looks exactly like a broken login.
* `remember_me: true` gives the cookie a `Max-Age` of `REMEMBER_ME_DAYS` (30) and the
  token a matching lifetime. Without it the cookie is a **session cookie** that dies
  with the browser — the right default for a shared counter machine. Rotation
  preserves whichever lifetime the session started with.
* The refresh token is still returned in the body for non-browser API clients. When
  both a cookie and a body token arrive, **the cookie wins**.

Because the cookie is `SameSite=Lax`, the SPA and the API must be on the **same site**:
use `127.0.0.1` for both or `localhost` for both. Mixing them (`localhost:5173` calling
`127.0.0.1:8000`) makes the request cross-site and the browser sends no cookie at all,
so sessions will not survive a reload. Axios sends `withCredentials: true`, and CORS is
configured with explicit origins and `allow_credentials`.

## Dates and times

Two different things, deliberately kept apart:

* **Instants** — `invoice_datetime`, `created_at`, `used_at`, `requested_at`, `printed_at` — are UTC, and are returned as ISO-8601 **with the offset** (`2026-08-02T19:26:00+00:00`). A client can convert them; a naive string it would have read as local time.
* **Business dates** — `invoice_date`, `issue_date`, `expires_on`, `return_date`, `action_date`, and every `date_from`/`date_to` filter — are the date at the counter (IST). They come from `utils.clock.business_date()`.

The two disagree between 00:00 and 05:30 IST, when UTC is still on the previous day. A sale rung up at 01:00 carries `invoice_date` of the day that has just begun and an `invoice_datetime` from the evening before — both correct, and derived from the same instant.

## Users & settings
| Method | Path | Role |
|---|---|---|
| GET/POST | `/users` | A — create sales-exec logins |
| GET/PATCH | `/users/{id}` | A |
| POST | `/users/{id}/reset-password` | A |
| POST | `/users/{id}/deactivate` | A |
| GET | `/users/permissions/catalogue` | A — every capability, its group, label and `delegable` flag |
| GET | `/users/{id}/permissions` | A — defaults, overrides, effective set, locked/unavailable |
| PUT | `/users/{id}/permissions` | A — `{permissions: [...], note?}`; the **full** desired set, not a diff |
| GET | `/settings` | S (cost-free view) |
| PATCH | `/settings` | A — also carries the letterhead branding: `logo_image`, `partner_logo_image`, `order_qr_image` (a `data:image/…` URI or an `https://` link, ≤512 KB of text; `""` clears one) and `order_qr_caption`. Anything that is not an image is rejected `422` — these values are rendered into an `<img src>` on a customer's bill |
| GET/PATCH | `/store` | A — store profile, GSTIN, licences |

### Per-user access

An admin can switch any **capability** — one verb on one thing, `medicines:create`
rather than "the medicines screen" — on or off for one person, without changing what
the role means for everybody else.

**What a sales executive holds by default:**

| Area | Default |
|---|---|
| Counter | full — sell, view the register, reprint, request a void, accept returns, look up credit notes |
| Inventory, Alerts, Expiry register | **view only**; adjusting stock, scanning and writing off stay with the owner |
| Medicines, compositions, manufacturers | **add** only — editing and deleting stay with the owner |
| Customers and doctors | add and edit, but **never** delete or disable |
| Purchases, order book, reports, users, settings | none |

**What an admin can hand over** (off by default, `delegable` in the catalogue):
`purchases:read` + `purchases:receive` (receive goods), `order_book:read` +
`order_book:manage`, `customers:delete` and `doctors:delete` (one tick covers both
disable and delete), plus the master edit/delete verbs and `inventory:adjust`.

**What can never be handed over:** `cost:view` — cost price never leaves the API for a
non-admin (CLAUDE.md rule 4) — and `reports:view`, `users:manage`, `settings:manage`,
`store:manage`, which would let a non-admin rewrite the policy governing them or promote
themselves. The catalogue flags these so the UI disables the box instead of offering a
tick the server would refuse (`409 PERMISSION_NOT_DELEGABLE`).

Other rules:

* **It is enforced**, not just hidden: a switched-off capability returns
  `403 PERMISSION_FORBIDDEN` from the route. A role that never held it keeps returning
  the long-standing `403 FORBIDDEN_ROLE`, so nothing an existing client relies on changes.
* **Only exceptions are stored.** A user with no overrides follows the role policy,
  including any later change to it. Putting somebody back to their defaults deletes
  their rows.
* **Reads shared with the counter stay open.** The catalogue, batches, customer/doctor
  pickers and credit-note lookup answer to `pos:sell` as well as to the capability that
  owns them, so an executive who can sell can always finish a sale.
* Two rails: nobody edits their own access (`409 CANNOT_EDIT_OWN_ACCESS`), and an admin
  always keeps `users:manage` (`409 PERMISSION_LOCKED`). An unrecognised key is
  `400 UNKNOWN_PERMISSION`. Every change writes an audit row with before/after lists.
* Sessions are **not** revoked: the gates resolve per request, so a change takes effect
  on the target's next call without interrupting a sale in progress.

### A bill settled with a credit note

A credit note is a **payment, not a discount**: the invoice value, its taxable amount and the GST on it are unchanged, and `sale.net_amount` still states what the sale was worth. What changes is how much the customer hands over, so the print payload carries all three figures:

* `sale.net_amount` — the invoice amount, printed as *Invoice Amount*;
* `credit_note_applied` — the total taken off, printed as *Less credit note CN-…* per note;
* `amount_payable` (+ `amount_payable_in_words`) — **what is left to pay**, printed as *Net Amount Payable*. Zero when the note covers the bill.

Each `payments` entry with `mode: "CREDIT_NOTE"` also carries `credit_note_no` and `credit_note_balance` (what remains on that note afterwards), so the bill can name the note and tell the customer what is still on account.

`credit_note_applied + amount_payable == sale.net_amount`, always.

`GET /sales/{id}/print` returns the branding on `store` alongside the address, licence and GSTIN, so one payload renders any of the four layouts (`print_format`: `A4` letterhead, `THERMAL_80`, `A5`, and `A5_NEW`, which began as a copy of `A5` and now has its own markup and stylesheet, so the two can be changed apart).

`A5_NEW` prints from the same payload with nothing added to it, and is worth reading as a statement of what a bill needs: `store.phone`/`store.email` paired on one line and `gstin`/`drug_licence_no_20b` on the next; per-item `hsn_code` and `discount_amount` in their own columns; `gst_rate` per line with the rupees left to `tax_summary`; and `units_per_pack` deciding whether `pack_label` is printed at all — a strip sold loose is counted in tablets, so its pack size says nothing, while a 100 ml bottle's does.

## Catalogue / masters
| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/medicines` | S | filters: `q`, `composition_id`, `manufacturer_id`, `in_stock_only`, `is_active` |
| POST / PATCH | `/medicines`, `/medicines/{id}` | A | includes `default_discount_pct`, `max_discount_pct`, `gst_rate`, `hsn_code`, `reorder_level_units` |
| GET | `/medicines/{id}` | S | admin response adds cost fields |
| GET | **`/medicines/match`** | S | **Goods-inward lookup** — `?q=<the product name as the supplier wrote it>&limit=&item_type=`. Matches on *tokens*, not the whole string, so a trailing form word (`TAB`, `SYP`, `CAP`), a different word order and punctuation all stop mattering. Returns `{query, terms, pack_hint, matches[], has_pack_match}` and **never chooses one** — a distributor's abbreviation is not evidence enough to bind money to a row, so the client shows the list and the operator picks. `pack_hint` is a size read off the text (`30GM`, `100ML`, `15'S` → a strip count); each match carries `pack_matches`, and `has_pack_match: false` with a hint set means *the brand is on the master but that size is not* — `Volini Gel 10GM` and `Volini Gel 30GM` are two products, so the answer is to add it, not to accept the near miss |
| GET | **`/medicines/search`** | S | **POS search** — `?q=&limit=20&item_type=`; matches brand **or** salt/composition. Ranked in three tiers, because that is the order the counter's question gets answered in: **(1)** what was asked for and is in stock, **(2)** a same-composition substitute that is in stock — included even though its own name does not match, flagged `is_alternative`, cheapest per unit first — **(3)** everything out of stock. A brand that has run out is not an answer to "do you have it?"; it is listed so nobody searches twice. Returns per row: returns per row: id, brand, composition_text, manufacturer, pack_size, pack_unit, pack_label, units_per_pack, is_countable, mrp_per_pack, unit_mrp, available_units, nearest_expiry, has_near_expiry, effective_max_discount_pct, effective_default_discount_pct |
| GET | **`/medicines/{id}/alternatives`** | S | same `composition_id`, different medicine, **sorted by `unit_mrp` ascending** (unpriced last) — powers the "same-salt alternatives" panel. Per-unit, not per-pack, so unequal pack sizes compare honestly |
| GET/POST/PATCH | `/compositions`, `/salts`, `/manufacturers` | A (GET: S) | |
| GET | **`/compositions/import-template?format=csv\|xlsx`** | A | the blank upload template, header row only. The XLSX carries a second sheet explaining each column. Generated from the same column list the importer validates against |
| POST | **`/compositions/import`** | A | CSV **or** XLSX bulk upload. Only `composition` is required; `therapeutic_class` and `notes` are optional. Rows are re-canonicalised, so a formulation already on file is reported as `skipped`, never duplicated. `?update_existing=true` fills in a **blank** class or note on an existing row and never overwrites one already set |
| GET | **`/medicines/import-template?format=csv\|xlsx&layout=standard\|price_list`** | A | the blank upload template in either layout; the XLSX carries a second sheet explaining each column |
| POST | `/medicines/import` | A | CSV/XLSX bulk upload, returns row-level errors. `?layout=auto\|standard\|price_list` — `auto` reads the layout from the header row. The response names the `layout` it used and carries any `warnings` |

Both importers run each row in its own savepoint, so a rejected row is reported against its own line number (the one the spreadsheet shows) and every other row still lands. Limits: 5 MB, 5000 rows. Error codes: `MISSING_COLUMNS`, `FILE_TOO_LARGE`, `FILE_NOT_UTF8`, `FILE_UNREADABLE`, `FILE_EMPTY`, `PACK_INFO_UNREADABLE`.

### The two medicine layouts

**`standard`** is the shop's own template: `brand_name`, `manufacturer`, `composition` (required), then `therapeutic_class`, `dosage_form`, `pack_size`, `pack_unit`, `pack_label`, `schedule_type`, `hsn_code`, `list_mrp_per_pack`, `reorder_level_units`, `rack_location`.

**`price_list`** is the shape a manufacturer's or portal's export arrives in: `Medicine Name`, `Price (INR)`, `Manufacturer`, `Salt/Composition`, `Pack Info`, `Rx Required`. Only the name, manufacturer and composition are required. Headers are matched ignoring case and punctuation, so `Price (INR)` and `price_inr` are the same column.

* **`Pack Info`** is parsed by `services.packinfo` into a dosage form, a pack size and a pack unit: *strip of 10 tablets* → TABLET / 10 / TABLET; *bottle of 30 ml Oral Suspension* → SYRUP / 30 / ML; *vial of 1 Powder for Injection* → INJECTION / 1 / VIAL; *tube of 20 gm Cream* → OINTMENT / 20 / GM. Litres and kilos are converted. A phrase naming no form yields `OTHER` rather than a guess — a wrong form would change whether the medicine is sellable piece by piece. An unreadable phrase rejects that row with `PACK_INFO_UNREADABLE`.
* **`Rx Required`** yes/no marks the medicine prescription-only and records it as `SCHEDULE_H`. The response carries a warning saying so, because H1 and X carry a register obligation the file cannot express.
* **`Price (INR)`** lands in `medicines.list_mrp_per_pack` — a **reference price only**. Nothing prices a sale from it; billing MRP comes from the batch being sold (which is why `medicines.mrp_per_pack` was dropped in migration 0004). Re-uploading with `update_existing=true` refreshes it without wiping the rack location, reorder level or HSN a price list does not carry.

## Parties
| Method | Path | Role |
|---|---|---|
| GET | `/customers` | S — the register. `q` matches mobile **or** name; `has_credit=true` narrows to holders of open credit; `sort` is one of `name`, `-name`, `mobile`, `-created_at`, `-credit_balance`. Every row carries `sale_count`, `last_purchase_on` and `total_spend`, all computed with voided invoices excluded |
| GET | `/customers/by-mobile/{mobile}` | S — the counter's lookup; same history fields |
| GET | `/customers/{id}` | S — one customer's card: details, `first_purchase_on`, `open_credit` (recomputed from the notes, so a drift from the cached `credit_balance` is visible), `recent_sales` (newest first, `?sales_limit=` up to 100) and every `credit_notes` row with `days_to_expiry` |
| POST/PATCH | `/customers`, `/customers/{id}` | S (quick-create at the counter; a sale with an unknown mobile creates the record itself). PATCH cannot change `mobile` — it is the identity the POS and credit notes match on |
| DELETE | `/customers/{id}` | A — deletes an unused record, deactivates one with invoices |
| GET/POST/PATCH | `/doctors`, `/doctors/{id}` | S — the counter must be able to name a prescriber mid-sale |
| DELETE | `/doctors/{id}` | A — deletes an unused record, deactivates one named on an invoice |
| GET/POST/PATCH/DELETE | `/suppliers`, `/suppliers/{id}` | A — DELETE removes an unused supplier, deactivates one with purchase history |

## Inventory
| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/inventory/stock` | S | product-wise totals; cost columns admin-only. `?q=&low_stock_only=&in_stock_only=&stocked_only=`. **`stocked_only`** keeps medicines with any batch on file, including one now empty or expired — "ever purchased", where `in_stock_only` asks the narrower "units on the shelf today". Rows carry `pack_unit` and `units_per_pack` so a caller can talk in tablets rather than units |
| GET | `/inventory/batches?medicine_id=` | S | FEFO-ordered; `include_expired=`, `include_empty=` |
| POST | `/inventory/batches` | A\* | new batch + opening ledger row; `409 BATCH_EXISTS` on a repeated number *and* expiry. Shelf life is stated one of three ways — see below |
| PATCH | `/inventory/batches/{id}` | A\* | commercial terms only — expiry, MRP, cost, GST, discount limits, block/unblock. Never quantity |
| POST | `/inventory/batches/{id}/adjust` | A\* | `{qty_units_delta, reason}` → signed `ADJUSTMENT_IN`/`ADJUSTMENT_OUT` ledger row. Reason is required (min 3 chars) |
| DELETE | `/inventory/batches/{id}` | A | only while nothing has been sold from it; `409 BATCH_HAS_MOVEMENTS` otherwise |
| GET | `/inventory/ledger?batch_id=&medicine_id=` | A | append-only movement history |

### Shelf life on a batch

Three things can be printed on a carton, and a batch payload states whichever one it is:

| `expiry_basis` | send | resolved `expiry_date` |
|---|---|---|
| `EXPIRY_DATE` (default) | `expiry_date` | as sent |
| `BEST_BEFORE` | `mfg_date` + `best_before_months` | `mfg_date` + n months, computed server-side |
| `NONE` | nothing | the never-expires sentinel |

Omit `expiry_basis` and the server infers it from what is filled in, so a client that only knows about expiry dates is unaffected. Errors: `400 EXPIRY_REQUIRED`, `400 EXPIRY_INVALID`.

Batch responses carry `expiry_basis`, `mfg_date`, `best_before_months` and **`has_expiry`**. When `has_expiry` is false, `expiry_date` holds a sentinel and must not be displayed; `days_to_expiry` is `null`. The same four fields exist on purchase lines (`POST /purchases`), which create batches the same way.

**A\*** — `inventory:adjust` **or** `purchases:receive`. Receiving goods *is* writing a batch, so the Update stock panel answers to either; a non-admin holding one of them gets the cost-free response model, exactly as the list endpoint does (rule 4).

## Purchases (goods inward)
| Method | Path | Role |
|---|---|---|
| GET | `/purchases` | A — register; `?q=` matches either invoice number, plus `supplier_id`, `date_from/to`, `include_cancelled` |
| POST | `/purchases` | A — header + items; creates/tops up batches, writes `PURCHASE` ledger rows, resets `stock_baseline_units`, returns non-fatal `warnings[]` (Phase 6 will also close matching order-book entries) |
| GET | `/purchases/{id}` | A — full document with lines |
| GET | `/purchases/import-template` | A — `?format=csv\|xlsx`; the blank item list. **Items only** — supplier, invoice number and date identify the document and are keyed on screen |
| POST | `/purchases/import-preview` | A — upload a CSV/XLSX item list. **Receives nothing.** Returns `{filename, total_rows, ready_rows, lines[], missing_columns[]}`; each line carries its parsed figures, its `errors[]`, the `matches[]` its product text might mean, a `pack_hint` and `has_pack_match`. No `medicine_id` — resolving the text to a product is the operator's decision, and the resolved lines come back through `POST /purchases` |
| POST | `/purchases/{id}/items` | A — **adds** lines to an invoice already received: `{items: [...]}`, same line shape as `POST /purchases`. Creates the batches and ledger rows, recomputes the totals from all lines, writes an audit row with before/after. `409 PURCHASE_CANCELLED` on a cancelled invoice. Additions only — correcting or removing a line is still cancel-and-re-enter |
| PATCH | `/purchases/{id}` | A — `payment_status` and `notes` only; the figures are the supplier's |
| POST | `/purchases/{id}/cancel` | A — `{reason}`; reverses stock through the ledger. `409 PURCHASE_HAS_SALES` once a unit has been sold, `409 PURCHASE_STOCK_SHORT` if the shelf is already below what the purchase brought in |

Line quantities are in **packs**; the server converts to sellable units with the medicine's `units_per_pack`. `free_qty_packs` is stocked but not billed. Cost is GST-exclusive (see docs/DATA_MODEL.md §4 for the arithmetic and the landed-cost rule).

## POS / sales
| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/pos/quote` | S | **price calculator** — send cart lines `[{medicine_id, qty_units, batch_id?, discount_pct?}]`; server allocates FEFO batches, validates discounts, returns full priced cart + tax break-up + `warnings[]`. **No DB writes** — an unknown mobile quotes as a walk-in rather than creating a customer. Call on every cart change. |
| POST | `/sales` | S | **confirm sale** — `{customer:{mobile,name?}|customer_id, doctor_id?, items:[…], payments:[{mode,amount,reference_no?,credit_note_id?}], idempotency_key, discount_override_reason?}` → issues invoice no, decrements stock, writes ledger + payments + audit. A repeat of the same `idempotency_key` returns the invoice already issued, with a warning saying so. |
| GET | `/sales` | S | **sales register** — `?q=` matches invoice no **or** mobile; filters `date_from/to`, `status`, `sales_user_id`. Row: invoice_no, invoice_datetime, customer_mobile, net_amount, status |
| GET | `/sales/{id}` | S | full sale with items (cost fields stripped for sales exec) |
| GET | `/sales/by-invoice/{invoice_no}` | S | |
| GET | `/sales/{id}/print` | S | print payload (store header, items, tax summary, amount in words) — a **read**: it counts nothing. `is_reprint` says whether the bill has been printed *before*. Carries `credit_note_applied`, `amount_payable` and `amount_payable_in_words` — see below |
| POST | `/sales/{id}/printed` | S | records one print at the moment the operator commits to it → `{invoice_no, reprint_count, is_reprint, printed_at}`. The first is the original (`is_reprint: false`); every later one is a duplicate. Writes an `audit_log` row |
| POST | `/sales/{id}/void-request` | S | `{reason}`, min 5 chars → status `VOID_REQUESTED`. Refused with `409 SALE_HAS_RETURNS` when goods have already come back. |
| GET | `/void-requests?status=PENDING` | A | |
| POST | `/void-requests/{id}/approve` | A | reverses stock + ledger, sale → `VOIDED` |
| POST | `/void-requests/{id}/reject` | A | `{review_note}` → sale back to `COMPLETED` |

**Never implemented:** `DELETE /sales/*`.

## Returns & credit notes
| Method | Path | Role |
|---|---|---|
| GET | `/returns/returnable/{sale_id}` | S — sold qty minus already returned, per line, plus the window state |
| POST | `/returns` | S — `{sale_id, items:[{sale_item_id, qty_units, condition}], refund_mode, reason}` → restocks saleable units, issues a credit note or records a cash refund. **Past `return_window_days` an admin token is required** (`409 RETURN_WINDOW_EXCEEDED`), and the approver is recorded on the document. |
| GET | `/returns`, `/returns/{id}` | S |
| GET | `/credit-notes?customer_id=&mobile=&status=&open_only=` | S |
| GET | `/credit-notes/{id}` | S — with every redemption against it |
| POST | `/credit-notes/{id}/cancel` | A — `409 CREDIT_NOTE_IN_USE` once any part is spent |

Credit-note redemption happens inside `POST /sales` as a payment line with `mode=CREDIT_NOTE` + `credit_note_id`; the server validates balance and expiry atomically.

## Alerts & order book
| Method | Path | Role |
|---|---|---|
| GET | `/alerts/summary` | S — the five counts plus `near_expiry_days` and `expired_value` |
| GET | `/alerts/expiring?days=` | S — defaults to `settings.near_expiry_days`; cost per batch is admin-only |
| GET | `/alerts/expired` | S |
| GET | `/alerts/low-stock`, `/alerts/out-of-stock` | S — expired and blocked units are **not** counted as available |
| GET | `/order-book?status=OPEN&supplier_id=` | A |
| POST | `/order-book/recompute` | A — re-evaluates the decline rule across the catalogue and closes entries whose stock has recovered |
| PATCH | `/order-book/{id}` | A — suggested packs (units re-derived), supplier, status; `ORDERED` stamps `ordered_at` |
| GET | `/order-book/export?status=OPEN` | A — CSV grouped by supplier |

## Expiry register
| Method | Path | Role |
|---|---|---|
| GET | `/expiry-register?status=` | A |
| POST | `/expiry-register/scan` | A — pulls expired batches with stock into the register; **idempotent**, one entry per batch |
| POST | `/expiry-register/{id}/write-off` | A — zeroes stock via an `EXPIRY_WRITE_OFF` ledger row and blocks the batch |
| POST | `/expiry-register/{id}/return-to-supplier` | A — `{supplier_id, debit_note_ref, credit_expected?}`; the reference is required |
| POST | `/expiry-register/{id}/record-credit` | A — `{amount}`, only after a return (`409 EXPIRY_NOT_RETURNED`) |

## Reports (all **A**, all accept `date_from`/`date_to`, all support `?format=json|csv|xlsx`)
| Path | Report |
|---|---|
| `/reports/gst/outward-summary` | rate-wise taxable/CGST/SGST/IGST on sales, B2C vs B2B |
| `/reports/gst/inward-summary` | supplier & rate-wise purchase register |
| `/reports/gst/net-liability` | outward − inward per rate per month |
| `/reports/sales/daybook` | invoice-wise + payment-mode-wise daily collection |
| `/reports/sales/by-executive` | invoices, value, discount, void/return rate |
| `/reports/inventory/fast-moving?top=50` | units sold, turnover ratio, days of cover |
| `/reports/inventory/slow-moving?threshold_units=` | below-threshold/zero movers, locked value |
| `/reports/inventory/valuation` | batch-wise qty × cost and × MRP |
| `/reports/inventory/expiry-ageing` | expired / ≤30 / 31–60 / 61–90 / 91–180 buckets |
| `/reports/profitability?group_by=medicine\|manufacturer\|month` | sale value − cost value, margin % |
| `/reports/customers/top`, `/reports/doctors/top` | ranked lists |
| `/reports/credit-notes/ledger` | issued / used / outstanding / expiring |
| `/reports/audit` | voids, discount overrides, approvals |

Every report returns one envelope, so the SPA renders them all with a single table
and `?format=` is implemented once:

```json
{"report": "gst_outward_summary", "title": "GST outward (sales) summary",
 "date_from": "2026-07-01", "date_to": "2026-07-31",
 "generated_at": "2026-08-03T09:12:44+00:00",
 "columns": [{"key": "gst_rate", "label": "GST %", "type": "rate"}],
 "rows": [{"gst_rate": "12.00"}], "totals": {"taxable_value": "1000.00"},
 "meta": {"gstin": "18AABCU9603R1ZM"}}
```

Column `type` is one of `text | int | money | qty | rate | date | datetime`; it drives
formatting, alignment and the XLSX cell type. Numbers are **strings at their column's
scale** (`"1000.00"`), as everywhere else in the API. `date_from` defaults to the 1st of
the current month and `date_to` to today (IST); a backwards range is `400
INVALID_DATE_RANGE`, one over 732 days `400 DATE_RANGE_TOO_WIDE`. `/inventory/valuation`
and `/inventory/expiry-ageing` are snapshots of current stock — they accept the range but
report as at today, stated in `meta.as_at`.

## Health
`GET /health` → `{"status":"ok","db":"ok","version":"x.y.z"}`

---

## Key validation codes
`INVALID_CREDENTIALS`, `TOKEN_EXPIRED`, `FORBIDDEN_ROLE`, `DISCOUNT_LIMIT_EXCEEDED`, `INSUFFICIENT_STOCK`, `BATCH_EXPIRED`, `BATCH_BLOCKED`, `PAYMENT_MISMATCH`, `CREDIT_NOTE_EXPIRED`, `CREDIT_NOTE_INSUFFICIENT_BALANCE`, `RETURN_QTY_EXCEEDS_SOLD`, `RETURN_WINDOW_EXCEEDED`, `SALE_ALREADY_VOIDED`, `SALE_HAS_RETURNS`, `DUPLICATE_IDEMPOTENCY_KEY`, `MOBILE_ALREADY_EXISTS`, `PRESCRIPTION_REQUIRED`.

Built so far: `EXPIRY_ALREADY_ACTIONED`, `EXPIRY_NOT_RETURNED`, `VOID_ALREADY_REQUESTED`, `VOID_ALREADY_REVIEWED`, `CREDIT_NOTE_REQUIRED`, `CREDIT_NOTE_WRONG_CUSTOMER`, `CREDIT_NOTE_NEEDS_CUSTOMER`, `CREDIT_NOTE_IN_USE`, `CREDIT_NOTE_CANCELLED`, `MEDICINE_EXISTS`, `MEDICINE_IN_USE`, `MEDICINE_INACTIVE`, `COMPOSITION_EXISTS`, `COMPOSITION_IN_USE`, `MANUFACTURER_EXISTS`, `SALT_EXISTS`, `BATCH_EXISTS`, `BATCH_HAS_MOVEMENTS`, `DISCOUNT_POLICY_INVALID`, `DISCOUNT_OVERRIDE_REASON_REQUIRED`, `SUPPLIER_EXISTS`, `SUPPLIER_IN_USE`, `PURCHASE_EXISTS`, `PURCHASE_CANCELLED`, `PURCHASE_HAS_SALES`, `PURCHASE_STOCK_SHORT`, `CUSTOMER_IN_USE`, `DOCTOR_EXISTS`, `DOCTOR_IN_USE`, `INVALID_QUANTITY`, `RETURN_INVALID`, `SEQUENCE_EXHAUSTED`, `INVALID_DATE_RANGE`, `DATE_RANGE_TOO_WIDE`, `PERMISSION_FORBIDDEN`, `PERMISSION_LOCKED`,
`PERMISSION_NOT_DELEGABLE`, `CANNOT_EDIT_OWN_ACCESS`, `UNKNOWN_PERMISSION`.
