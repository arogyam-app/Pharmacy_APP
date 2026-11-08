# Pharmacy POS — Build Roadmap

Build in this order. Each phase ends **runnable and tested** — don't start the next until the acceptance criteria pass.

---

## Phase 0 — Scaffold & environment ✅
- `backend/` venv, `requirements.txt`, FastAPI app factory, CORS, `/health`, settings via `pydantic-settings` + `.env`.
- SQLAlchemy session/base, Alembic initialised, `DATABASE_URL=sqlite:///./pharmacy.db`.
- `frontend/` Vite + Vue 3 + Router + Pinia + Axios; base layout, nav shell, API client with auth interceptor.

**Done when:** both servers start, `/health` returns ok, the SPA renders a placeholder page fetching `/health`.

## Phase 1 — Auth, users, settings, audit ✅
- `stores`, `users`, `refresh_tokens`, `settings`, `audit_log`, `document_sequences` models + migration + seed.
- JWT login/refresh/logout, argon2 hashing, `require_role` dependency, audit middleware.
- Login view, auth store, route guards, role-driven menu, change-password.

**Done when:** admin logs in, creates a sales-exec, that exec logs in and sees a restricted menu; a 403 is returned (not just hidden in the UI) for admin routes; audit rows exist for each action.

## Phase 2 — Catalogue masters ✅
- `salts`, `compositions`, `composition_salts`, `manufacturers`, `medicines` + `search_text` maintenance.
- CRUD APIs, medicine CSV import, seed of ~30 demo medicines across ~12 compositions (≥3 brands sharing one composition).
- Masters screens with search + pagination.

**Done when:** `/medicines/search?q=` finds a medicine by brand **and** by generic name, and `/medicines/{id}/alternatives` returns the same-composition siblings.

## Phase 3 — Inventory & purchases ✅
- `batches`, `stock_ledger`, `suppliers`, `purchase_invoices`, `purchase_items`. Adjustments are ledger rows with a mandatory reason rather than their own table (docs/DATA_MODEL.md §4).
- Goods-inward posting service: creates or tops up batches, ledger rows, inward GST split, landed cost, `stock_baseline_units` reset, gap-free document number. Cancellation reverses stock through the ledger and is refused once anything has sold.
- Stock/batch/ledger APIs with cost fields gated to admin; inventory, suppliers & purchases screens.

**Done when:** a purchase entry increases stock, the ledger sum equals `qty_available_units` (asserted by a test), and a sales-exec token receives **no** cost field anywhere.

## Phase 4 — POS & billing *(the core)* ✅
- Pricing service (§3.3 of SPEC), FEFO batch allocator with multi-batch split, discount policy resolution, GST-inclusive tax break-up, round-off applied once on the invoice.
- `POST /pos/quote` (no writes) and `POST /sales` (transactional, idempotent, sequence-locked 8-digit invoice no).
- Payments (split modes), customer quick-create by mobile, doctor picker, prescription-required check. Credit-note redemption arrived with Phase 5.
- POS screen: keyboard-first search (F2/F4/F9/Esc), alternatives panel, cart, batch picker, totals card, payment dialog, localStorage cart persistence.
- Print views: 80 mm thermal + A5, print stylesheet, amount in words, counted reprints with a `REPRINT` watermark.

**Done when:** a sale of *1 tablet* plus *2 strips* of another medicine, with discount, prints a correct invoice; stock drops by the exact unit count; a double-submit with the same idempotency key returns the same invoice instead of a second one.

## Phase 5 — Register, void workflow, returns, credit notes ✅
- Sales register list + search by invoice no / mobile, reprint with counter *(built in Phase 4)*.
- Void request → admin approve/reject, with full stock reversal through fresh `VOID_REVERSAL` ledger rows. The invoice number is kept and never reissued.
- Returns against an invoice, capped at what was sold minus what has already come back; refund is the line's post-discount value pro-rata with GST reversed at the **snapshotted** rate. Damaged/expired goods are refunded but never restocked.
- Credit notes: customer-scoped, issued by a return, redeemable on a later sale under a row lock so a balance is consumed exactly once.

**Done when:** an exec cannot delete a sale by any route; a voided sale restores stock; a return issues a credit note whose balance is consumed exactly once on the next invoice.

## Phase 6 — Alerts, expiry register, order book ✅
- Alerts summary + the four buckets; POS-time near-expiry warning and expired-batch block *(the POS half shipped in Phase 4)*. Expired stock counts as **out of stock**, not as availability.
- Expiry scan (idempotent, one entry per batch), write-off through an `EXPIRY_WRITE_OFF` ledger row that also blocks the batch, return-to-supplier against a debit note, and recording the credit when it arrives.
- Order book on the decline rule and the reorder level: raised on sale, refreshed rather than duplicated, closed automatically by a purchase that covers it. Recompute sweeps the catalogue and closes entries whose stock has recovered. CSV export grouped by supplier.

**Done when:** selling stock past the 60 % threshold creates an `OPEN` order-book entry with a sensible suggested quantity, and receiving a purchase for it closes the entry and resets the baseline.

## Phase 7 — Reports
- All reports in `docs/API.md`, each with a JSON endpoint, CSV/XLSX export and a UI screen with filters.
- GST outward/inward/net-liability first (they're the compliance-critical ones), then movement and profitability.

**Done when:** the GST outward summary for a period reconciles line-for-line with the sum of that period's invoices, and fast/slow-moving lists are consistent with the ledger.

## Phase 8 — Hardening & handover
- Test suite green: pricing, allocation, discount limits, returns/credit notes, GST maths, ledger invariant, role isolation, idempotency.
- Rate-limited login, request logging, error boundary in the SPA, empty/loading/error states everywhere.
- Backup script, `.env.example`, README with the exact setup commands, seeded demo walkthrough.
- **PostgreSQL switch rehearsal:** point `DATABASE_URL` at Postgres, run `alembic upgrade head`, run the full test suite green.

---

## Deferred (design for, do not build)
Multi-store UI & store switcher · inter-store stock transfer · supplier payments/ledger · loyalty points · SMS/WhatsApp invoice delivery · barcode scanning · server-side PDF · e-invoice/e-way bill · offline-first PWA · Tally export.

## Definition of done (every phase)
1. Alembic migration committed and reversible.
2. Pydantic request **and** response schemas — no ORM objects returned raw.
3. Role check on the route + cost-field gating in the response schema.
4. Audit log row for every mutation.
5. Tests for the happy path **and** each documented error code.
6. UI has loading, empty and error states.
