# AI Expense Intelligence Platform — Implementation Plan (Revised)

## Context

`/Users/pranjalivedpathak/Documents/Project26/expense tracker` is empty. This is a greenfield build of a multi-tenant Indian personal-finance platform whose core promise is: **a user drags in bank and credit-card statement PDFs and never types a transaction.**

Indian statement PDFs are wildly inconsistent across banks, and the naive fix — hand the PDF to an LLM — is both unreliable and a privacy disaster (account numbers, PAN, UPI IDs, card numbers leaving the perimeter). The architecture inverts that:

> **Deterministic extraction and validation produce the truth. The LLM is a narrow enrichment component that never sees raw documents, never touches the database, never performs financial arithmetic, and never overrides a human correction.**

This revision narrows AI to its smallest defensible surface (merchant→category enrichment and natural-language phrasing over pre-computed aggregates), moves all money math into a deterministic **Financial Intelligence Engine**, makes confidence multidimensional, ships the UI from phase zero so it is validated continuously, holds extraction to published accuracy targets scored **against ground truth rather than against what was successfully extracted**, requires exact reconciliation before a statement is trusted, and adds a real-statement validation phase after the synthetic one.

**This is the frozen architecture.** Implementation proceeds P0 → P9 without further redesign.

**Decisions carried forward:** whole platform, phase by phase · **Gemini implemented first** behind a provider abstraction (others are future-ready interfaces, not MVP code) · synthetic Indian statement PDFs as demo data *and* golden fixtures · **HDFC / ICICI / SBI / Axis** dedicated parsers, other four extend a tuned generic parser.

---

## Repository layout

Built at the working-directory root (the working directory *is* `expense-ai`).

```
docker-compose.yml · docker-compose.prod.yml · .env.example · Makefile · README.md
docker/           api, worker, frontend, nginx Dockerfiles + nginx.conf
infrastructure/   postgres init + RLS, minio bootstrap, prometheus, grafana, loki
backend/app/
  core/           config, logging (+ redaction), security, deps, errors, rate_limit
  db/ models/ schemas/ alembic/
  api/v1/         auth users accounts statements transactions categories review
                  intelligence assistant privacy audit notifications export health
  services/       accounts statements transactions categorization dedup reconciliation
                  confidence movement merchant audit export retention
  intelligence/   ← Financial Intelligence Engine (deterministic, zero LLM)
                  analytics recurring anomaly budgets insights timeline forecasting
  extraction/     classifier text_extract table_extract ocr bank_detect pipeline
  privacy/        gateway allowlist scrubber detectors injection_guard output_validator
  ai/             base(ABC) providers/gemini.py providers/_future/* router cost prompts
  ingestion/      base(ABC) sources/pdf_upload.py sources/_future/account_aggregator.py
  assistant/      tools executor orchestrator
  security/ observability/
parsers/          base registry canonical banks/* merchants/* normalizers/*
workers/          celery_app tasks/{ingest,extract,intelligence,maintenance} scheduler/
frontend/         Next.js 15 App Router · app/(auth) + app/(app)/<17 routes> · components/ lib/
tools/            statement_generator/   → PDF + expected.json fixture pairs
                  accuracy_harness/      → scorecard vs. targets
tests/            parsers pipeline privacy security intelligence api accuracy
```

---

## Non-negotiable invariants

| Invariant | How it is enforced (structurally, not by convention) |
|---|---|
| No PII reaches an LLM | `AIPayload` Pydantic model, `extra="forbid"` — an account number is *unrepresentable*. Post-build detector re-scan; any hit aborts the call, fail closed. |
| LLM performs no financial math | All amounts, totals, deltas, trends, anomalies computed in `intelligence/` in Python/SQL. The model receives only pre-computed, rounded, bucketed aggregates for phrasing. |
| LLM has no DB/FS/SQL/tool access | Categorization path passes zero tools. Assistant path exposes only 7 typed server-executed functions. |
| Tenant isolation | `tenant_id` everywhere + app-layer scoping **and** PostgreSQL RLS via `SET LOCAL app.current_tenant_id`; app connects as non-superuser with `FORCE ROW LEVEL SECURITY`. |
| Verified corrections survive | Service guard **plus** a DB trigger rejecting any AI-sourced write to a row with `is_verified = true`. |
| Original data immutable | `original_*` columns never updated; corrections land in `corrected_*`; effective value is a coalesce. |
| Money is exact | `NUMERIC(18,2)` in PG, `Decimal` in Python, **string** over JSON, `Intl.NumberFormat('en-IN')` in UI. No float, anywhere. |
| Transfers aren't expenses | `is_expense` + `transfer_group_id` pairing; every analytics query filters on `is_expense`. |
| Duplicates can't land | Deterministic fingerprint over `tenant_id + account_id + date + amount + direction + normalized description` — **balance deliberately excluded** — plus `UNIQUE (tenant_id, account_id, fingerprint)`. The database refuses, not just the code. |
| A statement is trusted only if it reconciles | `statements.trust_status` reaches `trusted` **only** on an exact ₹0.00 reconciliation. No tolerance band. |
| **Logs carry no financial or personal data** | Allow-list logging (below) + a test that greps a full pipeline run's log stream for leaks. |

### Logging policy — explicitly prohibited content

Structured logging accepts **only** these fields: `request_id, user_id, tenant_id, job_id, statement_id, account_id, transaction_id, bank_code, stage, status, duration_ms, count, error_code, model_name`.

**Never logged, at any level, including DEBUG and exception tracebacks:** exact amounts · balances · transaction descriptions (raw or normalized) · merchant strings · account or card numbers · UPI IDs · IFSC/PAN/Aadhaar · names, emails, phones, addresses · uploaded filenames · PDF bytes, paths, or page text · AI prompts, payloads, or raw responses.

Enforced three ways: (1) a `RedactingFilter` scrubbing the same detector set used by the privacy gateway, (2) exception handlers that log `error_code` + type only, never `repr(exc)` of a domain object, (3) `tests/security/test_log_leakage.py` runs a full upload→ledger pipeline capturing all log output and asserts zero digit-runs ≥6, zero `@`, and zero occurrences of any fixture merchant/description string.

---

## The pipeline

Celery chain; states `queued → processing → extracting → validating → categorizing → review_required | completed | failed`, each transition written to `job_events` and streamed to the UI.

1. **Validate** — magic bytes, size/page caps, `pikepdf` structural scan rejecting `/JavaScript`, `/OpenAction`, `/Launch`, `/EmbeddedFile`; zip-bomb guard; optional ClamAV behind a compose profile.
2. **Store** — MinIO, SSE-C with a per-tenant key derived from a master KEK via HKDF; key `tenants/{tid}/statements/{uuid}.pdf`; 5-minute presigned URLs only.
3. **Classify document** — bank statement vs credit-card statement vs unknown.
4. **Extract** — PyMuPDF text layer; below a text-density threshold → OCR fallback (`pdf2image` + Tesseract, OpenCV deskew). Tables via pdfplumber (lattice + stream), Camelot fallback for ruled grids.
5. **Detect bank** — IFSC prefix, header/footer phrases, logo text → `(bank_code, confidence)`.
6. **Parse** — registry dispatch → `list[CanonicalTransaction]` + `StatementMetadata` (masked account, period, opening/closing balance).
7. **Reconcile** — `opening + credits − debits == closing`, **exactly, to the paisa**; row-by-row running-balance continuity; page-sequence continuity; date-in-period and direction sanity. A statement earns `trust_status = trusted` **only on a ₹0.00 delta** — there is no tolerance band. Any discrepancy → `untrusted`, surfaced on **Statement Health** with the exact rupee gap and the first row where the running balance diverges; its transactions still land in the ledger (so nothing is hidden) but are excluded from AI narrative input and marked in every analytics response, so no total is ever silently wrong.
8. **Deduplicate** — primary fingerprint:

   ```
   sha256(tenant_id | account_id | txn_date | amount | direction | normalized_merchant_or_description)
   ```

   **Running balance is deliberately not part of the key.** The same transaction legitimately carries different balances across re-issued, corrected or overlapping statements, so folding balance in would produce a different hash for an identical transaction and silently defeat dedup — the exact failure this system must not have. Balance is instead *supporting evidence*: a tie-breaker when a fingerprint collides on a genuinely distinct same-day/same-amount pair, and a signal feeding `confidence_validation`. A fuzzy near-dup pass (same date/amount/direction, description similarity ≥ 0.92) catches formatting drift across overlapping periods.
9. **Classify movement** — transfer / card-payment / refund / salary / investment / EMI / cash / ATM. Cross-account pairing: opposite direction, equal amount, ≤3 days apart, same tenant → shared `transfer_group_id`, both `is_expense = false`.
10. **Normalize merchant** — strip UPI handles, ref/terminal IDs, then dictionary + fuzzy match (`SWIGGY*ORDER`, `SWIGGYINSTAMART`, `UPI-SWIGGY@ybl` → **Swiggy**).
11. **Categorize** — cascade below, with a recorded reason.
12. **Score confidence** — four independent dimensions, below.
13. **Persist** → trusted ledger; emit summary (`1,248 extracted · 1,191 verified · 42 review · 15 unclassified`).

### Multidimensional confidence

One overall number hides the failure that matters. Four independent scores are stored per transaction:

| Dimension | Measures | Signal sources |
|---|---|---|
| `confidence_extraction` | Did we read the row correctly? | text-layer vs OCR, column alignment, date/amount parse cleanliness, per-field scores in `field_confidence` |
| `confidence_merchant` | Did we identify who it was? | exact dictionary hit / fuzzy ratio / unmatched residue length |
| `confidence_category` | Is the category right? | which cascade tier fired, rule specificity, historical sample count, AI self-reported score |
| `confidence_validation` | Does the statement hang together? | reconciliation delta, balance continuity, page continuity, duplicate cleanliness |

**The gate is `min()` of the four, never an average** — a perfect category on a misread amount is worthless, and averaging hides exactly that.

- `min ≥ 0.97` → **auto-approved**
- `0.90 ≤ min < 0.97` → **flagged** (in the ledger, visibly marked, counted in totals)
- `min < 0.90` → **mandatory review** (in the ledger so reconciliation still balances, but banner-surfaced and excluded from AI narrative inputs until resolved)

The UI shows the four as a compact bar and names the weakest dimension, so a reviewer knows *what* to check.

### Categorization cascade + explainability

`User Rule → Verified Merchant Rule → Deterministic Rule → Historical User Pattern (≥3 confirmed) → AI → Other`

Every transaction stores `category_source` (enum) and `category_reason` (JSONB: matched rule id, matched pattern, fuzzy score, historical sample count, AI model+version+score). The detail panel renders **"Why was this categorized this way?"** as a plain sentence with a link to the governing rule — e.g. *"Your rule: merchant 'Swiggy' → Food, created 12 Mar, applied 47 times"* or *"AI (gemini, 0.94) — no rule matched; 'BLINKIT' resolved to Grocery."* A user correction writes a `user_category_rules` row that outranks AI permanently.

22 categories with subcategories: Food, Grocery, Rent, Utilities, Shopping, Travel, Fuel, Entertainment, Subscriptions, Healthcare, Insurance, Education, EMI, Investment, Salary, Bank Charges, Taxes, Cash Withdrawal, Transfers, Credit Card Payment, Refund, Other.

### Privacy Gateway

**Allow-list only**, violations structurally impossible. The entire payload an LLM may ever see:

```
merchant_normalized · description_sanitized · amount_bucket · direction
payment_method · mcc_hint · day_of_week
```

- Scrub *before* allow-listing: PAN `[A-Z]{5}[0-9]{4}[A-Z]`, Aadhaar (12-digit + Verhoeff), IFSC `[A-Z]{4}0[A-Z0-9]{6}`, account numbers, card PANs (Luhn), UPI IDs, `+91` phones, emails, GSTIN, statement numbers, long digit runs.
- **Re-scan the finished payload.** Any hit → abort, route to review, write `privacy_incidents`. Fail closed, always.
- Amounts are **bucketed, never exact** — exact rupee values add negligible categorization signal and materially aid re-identification.
- **Injection defense:** untrusted text is never interpolated into instructions; it is fenced in `<untrusted_data>` under an explicit data-not-instructions rule, plus a heuristic detector that quarantines instruction-shaped merchant strings and skips AI entirely.
- **Output:** Gemini `responseSchema` structured mode → Pydantic validation → category must be in the fixed enum → output scanned for PII echo, URLs, tool-call-shaped strings. Off-schema → `Other` + review.
- Every gateway decision increments counters that the **Privacy Center** screen renders: calls made, fields sent, payloads blocked, injection quarantines, incidents.

---

## Financial Intelligence Engine — `backend/app/intelligence/`

A dedicated, fully deterministic module. **No LLM is invoked anywhere inside it.** Everything the dashboard, insights, budgets and assistant report originates here, which is what makes the numbers auditable.

| Sub-module | Responsibility |
|---|---|
| `analytics.py` | monthly totals, income vs expense, net cash flow, savings rate, MoM deltas, category rollups, daily series, top merchants — all SQL aggregates over `is_expense`-filtered rows |
| `recurring.py` | subscription detection: merchant grouping → interval clustering (weekly/monthly/quarterly/annual) → cadence stability score → next expected charge, annual cost |
| `anomaly.py` | **statistical only** — per-category robust z-score (median + MAD), merchant first-seen-large, month-over-month category spike vs trailing baseline, duplicate-charge proximity. Outputs a reason string with the actual numbers. Never called "fraud". |
| `budgets.py` | budget progress, burn rate, projected month-end via elapsed-day linear projection + recurring-charge lookahead |
| `timeline.py` | unified chronological event stream (transactions, statement imports, large charges, budget breaches, subscription renewals) powering **Financial Timeline** |
| `insights.py` | assembles the monthly report *as structured data*: largest category, fastest-growing category, largest transaction, savings opportunities, recurring load |
| `forecasting.py` | simple deterministic run-rate projection; no ML, no LLM |

The AI's only role downstream is optional: turn an already-computed `MonthlyInsightSnapshot` into prose. If AI is disabled, the UI renders the same snapshot as structured cards — **the product is fully functional with no API key.**

### AI Assistant — read-only, tool-gated, arithmetic-free

Seven tools: `get_monthly_spending`, `get_category_spending`, `get_transactions`, `get_top_merchants`, `get_recurring_expenses`, `compare_months`, `get_anomalies`. Each is a thin wrapper over the Intelligence Engine.

- The model chooses tools and arguments; **`tenant_id`/`user_id` are injected server-side and are not fields the model can express.**
- Tool results are pre-computed aggregates, redacted through the allow-list, and rounded to whole rupees.
- The model composes phrasing only — it never adds, compares or projects. Numbers in the answer must be traceable to a tool result; a post-check flags any figure not present in the tool output.
- Budget ≤5 tool calls, hard timeout, rate limited. No SQL, no filesystem, no object storage.
- The same orchestrator backs the **dashboard AI query box**, which additionally offers one-tap canned questions and renders results as a chart-or-table card, with an "open in Transactions" link that reproduces the query as real filters.

---

## Data model

`tenants · users · refresh_tokens · accounts · statements · statement_pages · statement_health · transactions · transaction_audit · categories · subcategories · merchants · merchant_aliases · user_category_rules · processing_jobs · job_events · ai_classifications · privacy_incidents · privacy_counters · subscriptions · budgets · insight_snapshots · anomalies · timeline_events · audit_logs · notifications · transfer_groups · ingestion_sources · extraction_accuracy_runs`

Changes from the first draft:

- **`transactions`** — single `confidence` replaced by `confidence_extraction`, `confidence_merchant`, `confidence_category`, `confidence_validation`, plus generated `confidence_min` and `review_status` (`auto_approved|flagged|review_required|resolved`). Adds `category_source` enum and `category_reason JSONB` for explainability. Keeps frozen `original_*`, separate `corrected_*`, `is_verified`, `is_expense`, `movement_type`, `transfer_group_id`, `fingerprint`, `source_page`, `source_row`, `field_confidence JSONB`.
- **`statements`** — adds `trust_status` (`pending | trusted | untrusted`), set to `trusted` only on an exact ₹0.00 reconciliation.
- **`statement_health`** — per-statement reconciliation delta, first divergent row, expected vs actual row count, page continuity, OCR usage ratio, extraction score, per-metric pass/fail. Powers the Statement Health screen.
- **`insight_snapshots` / `anomalies` / `timeline_events`** — Intelligence Engine outputs, computed by the scheduler, so dashboards are fast and the assistant reads settled aggregates.
- **`privacy_counters`** — gateway telemetry for the Privacy Center.
- **`extraction_accuracy_runs`** — scorecard history tagged `corpus` (`synthetic | real`), with separate columns for recall, precision and the absolute counts `missing_transactions, extra_transactions, wrong_amount, wrong_date, wrong_direction, wrong_merchant, wrong_category`, plus target, passed and commit.
- **`ingestion_sources`** — `source_type` enum (`pdf_upload | csv | api | account_aggregator`); only `pdf_upload` implemented, the rest reserved so the pluggable ingestion model is real rather than aspirational.

---

## Golden-fixture accuracy framework

`tools/statement_generator/` emits **paired artifacts**: a realistic fictional PDF *and* an `expected.json` holding the exact ground-truth transaction list, opening/closing balances and metadata. Fixtures cover HDFC, ICICI, SBI, Axis, a generic bank, and a credit-card statement, plus deliberately hostile variants: multi-page with carry-forward, OCR-only scans, negative/credit rows, embedded newlines in descriptions, `1,23,456.78` lakh grouping, `DR`/`CR` suffixes, and a period overlapping another fixture (duplicate test).

### Transaction-level scoring — the anti-gaming rule

`tools/accuracy_harness/` aligns extracted rows against ground truth by fingerprint, then classifies **every ground-truth row**:

```
matched   — found, compared field by field
missing   — in ground truth, never extracted   → recall failure
extra     — extracted, not in ground truth     → precision failure (phantom / duplicated row)
```

**The denominator for every field metric is the ground-truth transaction count, and a missing transaction counts as a failure in every field metric.** This is the rule that matters: a harness that scores only successfully-extracted rows can report 99.9% amount accuracy while silently dropping 40% of a statement. That number would be a lie, and this harness is built so it cannot be told. Recall and precision are reported separately and never averaged into a single headline figure.

| Metric | Formula | Target | Gate |
|---|---|---|---|
| Transaction recall | `1 − missing / expected` | ≥ 99% | ✓ |
| Transaction precision | `1 − extra / extracted` | ≥ 99.5% † | ✓ |
| Date accuracy | `correct_date / expected` | ≥ 99.5% | ✓ |
| Amount accuracy | `correct_amount / expected` | ≥ 99.9% | ✓ |
| Debit/Credit direction | `correct_direction / expected` | ≥ 99.9% | ✓ |
| Merchant normalization | `correct_merchant / expected` | ≥ 98% | ✓ |
| Category assignment | `correct_category / expected` | ≥ 95% | ✓ |
| **Financial reconciliation** | exact, per statement | **100% — no tolerance** | ✓ |

† You did not specify a precision target; 99.5% is my chosen default — say the word and I'll change it.

Alongside percentages the scorecard prints **absolute counts** — `missing_transactions`, `extra_transactions`, `wrong_amount`, `wrong_date`, `wrong_direction`, `wrong_merchant`, `wrong_category` — per statement and per bank, because "3 missing transactions in HDFC-Mar-2024, first at row 47" is actionable and "99.4%" is not. Each is a separately stored column in `extraction_accuracy_runs`, so regressions are visible over time.

`make accuracy` prints per-bank and aggregate scorecards, writes an `extraction_accuracy_runs` row, and exits non-zero if any target is missed. This is a phase gate, not a report — **P4 is not done until the scorecard is green.**

---

## Real-world statement validation (P4.5)

Synthetic fixtures prove the framework is *correct*. They cannot prove it is *sufficient* — I generated them, so they contain only the layout quirks I already thought of. Real statements are where parsers actually fail: rotated pages, bank-changed templates mid-period, footer noise bleeding into the table, wrapped descriptions, and per-branch formatting drift. P4.5 exists to find those before you do.

**`tools/corpus/`** — three pieces, run locally, never networked:

1. **Anonymizer** — redacts names, addresses, account and card numbers, PAN, Aadhaar, UPI IDs, phones, emails and statement numbers **while preserving structure**: same character classes, same field widths, consistent per-statement pseudonyms, and *amounts and balances left untouched* so the statement still reconciles. A redaction that breaks arithmetic is useless for parser validation.
2. **Ground-truth builder** — a review UI that shows the PDF page beside the parser's proposed rows, lets you correct and confirm each one, and emits an `expected.json` in exactly the synthetic generator's shape. This is the slow part and it's unavoidable: real validation needs human-verified truth.
3. **Validation runner** — the *same* harness, the *same* seven metrics, a separate scorecard tagged `corpus=real`.

Corpus lives in `tests/fixtures/real/`, **gitignored by default**, encrypted at rest, loaded only by an explicitly opted-in run (`make validate-real`). It is never committed and never leaves the machine.

Each discrepancy drives per-bank parser tuning, and every fix lands as a permanent regression fixture — so the corpus compounds in value rather than being a one-off exercise.

**Dependency you need to know about:** I cannot source real HDFC/ICICI/SBI/Axis statements — I have none, and I won't fabricate something and call it real. P4.5 delivers the complete anonymization, ground-truth and validation machinery, and it runs the moment you drop statements in. Until then the real-corpus scorecard honestly reports `no corpus supplied` rather than a green tick, and the trust claims the system makes stay scoped to synthetic fixtures. If you can supply even two or three statements per bank, the phase becomes a real gate.

---

## API contracts (`/api/v1`)

```
auth           POST /register /login /refresh /logout · GET /me · GET /oauth/google{,/callback}
accounts       GET / · GET /{id} · PATCH /{id} · DELETE /{id}      (masked numbers only)
statements     POST /upload (multi) · GET / · GET /{id} · GET /{id}/health
               GET /{id}/download-url · POST /{id}/reprocess · DELETE /{id}
jobs           GET /{id} · GET /{id}/events (SSE progress stream)
transactions   GET / (filter: date, category, account, merchant, amount, direction,
                       review_status, confidence, is_expense)
               GET /{id} · PATCH /{id} · GET /{id}/explain · GET /{id}/audit
               POST /bulk-approve · POST /apply-to-similar
review         GET /queue · GET /stats · POST /{id}/approve · POST /{id}/edit
categories     GET / · POST /rules · GET /rules · DELETE /rules/{id}
intelligence   GET /summary · /trend · /categories · /daily · /top-merchants
               GET /recurring · /anomalies · /timeline · /insights/{yyyy-mm}
budgets        GET / · POST / · PATCH /{id} · DELETE /{id} · GET /progress
assistant      POST /query · GET /suggestions          (tool-gated, read-only)
privacy        GET /summary · GET /incidents           (Privacy Center)
export         POST /transactions?format=csv|json|pdf
audit          GET /logs        notifications  GET / · POST /{id}/read
health         GET /health · /health/ready · /metrics
```

OpenAPI is generated and served; every list endpoint is cursor-paginated and tenant-scoped by dependency, never by caller-supplied id.

---

## Frontend routes (17 + auth)

`/dashboard` *(with AI query box)* · `/transactions` · `/timeline` **new** · `/upload` · `/review` · `/accounts` · `/statements` · `/statements/health` **new** · `/categories` · `/subscriptions` · `/budgets` · `/insights` · `/assistant` · `/privacy` **new** · `/notifications` · `/audit` · `/settings` — plus `/login`, `/signup`, `/auth/callback`.

**Financial Timeline** — a chronological narrative of the financial year: transactions, statement imports, budget breaches, subscription renewals and anomalies on one scrollable spine with month anchors, filters and a density toggle.

**Statement Health** — per-statement trust report: reconciliation delta in ₹, expected vs extracted row count, page continuity, OCR usage, the four confidence dimensions aggregated, and a re-process action. This is where a user learns whether to trust an import.

**Privacy Center** — what has and has not left the system: total AI calls, the exact allow-listed field set (rendered from the actual Pydantic model, not hardcoded), payloads blocked, injection attempts quarantined, incident log, per-provider usage and cost, and a global "disable AI enrichment" switch.

Stack: Next.js 15 App Router · TypeScript strict · Tailwind v4 + shadcn/ui · TanStack Query · Recharts · React Hook Form + Zod · next-themes.

Palette → CSS variables. Light: bg `#F8FAFC`, surface `#FFFFFF`, fg `#0B1220`, muted `#64748B`. Dark: bg `#0B1220`, surface `#111827`, fg `#F8FAFC`. Primary `#10B981`, accent `#14B8A6`; success `#10B981`, warning `#F59E0B`, error `#EF4444`, info `#3B82F6`. The `dataviz` skill is loaded before any chart work so charts read as one system in both themes.

---

## Docker Compose architecture

| Service | Notes |
|---|---|
| `nginx` | TLS-ready reverse proxy, upload body limits, security headers, SSE pass-through |
| `frontend` | Next.js standalone build |
| `api` | FastAPI + uvicorn, healthcheck `/health/ready` |
| `worker-extract` | **dedicated heavy queue** — OCR/Camelot/Ghostscript/Tesseract image, CPU-bound, scaled independently |
| `worker-default` | light queue — categorization, intelligence, notifications, exports |
| `scheduler` | Celery beat — nightly intelligence snapshots, subscription refresh, anomaly sweep, retention |
| `postgres` | 16, init scripts create the non-superuser `expense_app` role + RLS policies |
| `redis` | broker + rate limiting + cache |
| `minio` + `minio-init` | encrypted object storage, bucket/policy bootstrap |
| `flower` | profile `debug` |
| `prometheus` `grafana` `loki` `promtail` | profile `observability` |
| `clamav` | profile `security` (optional, ~1 GB) |

Named volumes for pg/redis/minio/grafana; healthchecks and `depends_on: service_healthy` throughout; separate `docker-compose.prod.yml` overlay with secrets, resource limits and no dev mounts.

---

## Phases — each runnable and tested before the next begins

| # | Phase | Definition of done |
|---|---|---|
| **P0** | **Foundation + Design System + App Shell** — compose, Dockerfiles, nginx, FastAPI skeleton, redacting logger, health checks; **and** Next.js scaffold, palette tokens, shadcn primitives, light/dark, app shell, sidebar + mobile bottom nav, all 17 routes as skeleton screens, shared components (StatCard, DataTable, ConfidenceBars, EmptyState, Skeletons) | `docker compose up` → shell renders in both themes on desktop/tablet/mobile, every route navigable, `/health` green |
| **P1** | **Data layer** — SQLAlchemy 2.0 models, Alembic baseline, indexes/FKs, RLS policies + `expense_app` role, category + merchant-dictionary seeds | migrations apply clean; RLS test proves a wrong tenant GUC returns zero rows |
| **P2** | **Auth & tenancy (full stack)** — Argon2id, access JWT + rotating refresh with family reuse-detection, Google OAuth behind a flag, CSRF, rate limiting, tenant dependency wiring the RLS GUC, audit logging; login/signup/session UI wired | real login works in the browser; `test_tenant_isolation.py` passes on every endpoint |
| **P3** | **Ingestion + Statement Health (full stack)** — upload API, validation/malware gates, encrypted MinIO, presigned URLs, Celery job state machine, SSE progress; Upload page with drag-drop and live stages, Statements list, Statement Health screen | drag PDFs in the browser, watch stages advance, see the statement listed with a health record |
| **P4** | **Extraction, parsers & accuracy harness** — statement generator (PDF + `expected.json`), accuracy harness, PyMuPDF/pdfplumber/Camelot, OCR fallback, doc classifier, bank detector, registry, 4 dedicated + 4 generic-backed + generic card parser | **`make accuracy` green against all seven targets**, reconciliation exactly 100% |
| **P4.5** | **Real-world statement validation** — corpus anonymizer (structure-preserving, arithmetic-preserving), ground-truth builder UI, validation runner over `tests/fixtures/real/`; per-bank parser tuning driven by real discrepancies, each fix frozen as a regression fixture | machinery runs end to end and reports a `corpus=real` scorecard; **numeric gate applies once you supply statements** — until then it reports `no corpus supplied`, never a false green |
| **P5** | **Trust layer + Transactions UI** — reconciliation, fingerprint dedup, movement/transfer/refund/salary/EMI/ATM detection, merchant normalization, four-dimensional confidence, review routing; Transactions ledger, detail side panel, Review Center | upload → ledger end to end in the browser; re-uploading the same statement creates **zero** new rows |
| **P6** | **Privacy Gateway + Gemini enrichment** — gateway, scrubbers, injection guard, output validator; `AIProvider` ABC with **Gemini only** implemented, others registered as `NotImplementedError` stubs; cascade, explainability, correction learning; Privacy Center + "Why this category?" panel | adversarial PII and prompt-injection corpora pass; log-leakage test passes; Privacy Center shows live counters; **works with `AI_ENABLED=false`** |
| **P7** | **Financial Intelligence Engine (deterministic)** — analytics, recurring, anomaly, budgets, timeline, insights, forecasting + scheduler jobs; Dashboard with AI query box, Insights, Subscriptions, Budgets, Financial Timeline | every dashboard number reproduced by an independent SQL assertion in tests; engine tests run with AI disabled |
| **P8** | **Assistant & narrative layer** — tool registry, executor with server-injected identity, orchestrator, number-traceability post-check; Assistant screen + dashboard query box wired; monthly narrative from snapshots only | the 7 canonical questions answered correctly; cross-tenant tool authorization tests pass; every figure traceable to a tool result |
| **P9** | **Hardening & delivery** — full test sweep, Prometheus metrics + Grafana/Loki, CSV/JSON/PDF export, retention + account deletion, demo tenant seed, README, future-ready interfaces documented | clean `docker compose up` from scratch on an empty volume set; whole suite green; accuracy scorecard green |

### Reserved as future-ready interfaces (not MVP code)

`AccountAggregatorSource` (implements the `IngestionSource` ABC, raises `NotImplementedError`) · net-worth/assets-liabilities module (design note + reserved schema shape, no migration) · native mobile (responsive PWA manifest only) · OpenAI / Azure OpenAI / Anthropic / Azure AI Foundry providers (registered names with docstrings mapping each to the ABC; adding one is a single file). The Anthropic provider, when built, will be written against the `claude-api` skill rather than from memory.

---

## Verification

```bash
cp .env.example .env && docker compose up -d --build
curl localhost/api/v1/health         # api · postgres · redis · minio · workers green
make seed                             # demo tenant, categories, merchant dictionary
make gen-fixtures                     # synthetic PDFs + expected.json → tests/fixtures/statements
make accuracy                         # synthetic scorecard vs. the eight targets; non-zero exit on miss
make validate-real                    # same harness over tests/fixtures/real (opt-in, gitignored corpus)
make test                             # full backend suite
```

Targeted suites: `tests/security/test_tenant_isolation.py` (cross-tenant 404 at every endpoint) · `tests/security/test_log_leakage.py` (no PII in logs across a full pipeline run) · `tests/privacy/` (PII never leaves; injection corpus never leaks) · `tests/parsers/` (golden fixtures) · `tests/pipeline/` (reconciliation, dedup, confidence gating) · `tests/intelligence/` (engine outputs vs independent SQL, AI disabled).

**Manual end-to-end:** drag all generated PDFs into Upload → watch stages → check the summary counts → open Statement Health and confirm reconciliation delta is ₹0.00 → Review Center holds only `min < 0.97` rows → open a Swiggy row, read "Why was this categorized this way?", correct it to Food → re-upload the *same* statement: zero new transactions, and the correction now auto-applies with source `user_rule` → confirm Dashboard totals exclude transfers and card payments → ask the dashboard query box "How much did I spend on food this month?" and reconcile the figure against the Transactions filter → set `AI_ENABLED=false`, reload, and confirm every screen still works with structured output in place of prose.

## Known limitations, stated up front

- **Synthetic accuracy is not real-world accuracy, and the scorecard will say so.** Green synthetic numbers mean the framework is correct against layouts I authored — nothing more. P4.5 exists precisely to close that gap, and it needs a corpus from you; I cannot source real statements and will not fabricate any and label them real. Until a corpus exists, the real scorecard reads `no corpus supplied`.
- **Architecture is frozen after this revision.** P0 begins on approval and I will not redesign mid-build; if a genuinely blocking implementation issue surfaces, I'll stop, tell you what it is and what it costs, and get your call before changing anything structural.
- **Gemini model ID is env-configured** (`AI_MODEL_CATEGORIZE`, `AI_MODEL_ASSISTANT`) so it can track current releases without a code change.
- **ClamAV is optional** (compose profile, ~1 GB); the default gate is structural PDF safety analysis.
- **Google OAuth is disabled by default** — it needs your own client ID/secret in `.env`.
- **Anomalies are statistical outliers with stated reasons**, never fraud claims — the system has no ground truth for that.
