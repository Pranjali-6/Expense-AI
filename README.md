# Expense AI

A multi-tenant personal financial intelligence platform for Indian users. Drag in
your bank and credit-card statement PDFs; never type a transaction.

```
PDF upload → validation → encrypted storage → background job → classification
→ deterministic text/table extraction (OCR only if scanned) → bank detection
→ pluggable parser → canonical schema → reconciliation → duplicate detection
→ movement detection → merchant normalisation → categorisation cascade
→ confidence scoring → review queue → trusted PostgreSQL ledger → analytics
```

## The one idea this is built around

> **Deterministic extraction and validation produce the truth. The LLM is a narrow
> enrichment component that never sees a raw document, never touches the database,
> never performs financial arithmetic, and never overrides a human correction.**

Handing a statement PDF to a language model is both unreliable and a privacy
disaster — account numbers, PAN, UPI IDs and card numbers leaving your perimeter
in a prompt. So the architecture inverts it. PyMuPDF, pdfplumber and Camelot read
the statement. Arithmetic reconciles it. Only then, and only for transactions no
deterministic rule could categorise, does a heavily-restricted, PII-free fragment
reach a model.

**The platform is fully functional with `AI_ENABLED=false`.** Without a key it runs
entirely on deterministic rules and routes uncertain transactions to review.

## Quick start

```bash
make init        # writes .env from the template with freshly generated secrets
make bootstrap   # builds, starts, migrates and seeds — everything
make health      # api · postgres · redis · minio · workers
make test        # the suite
```

Then open <http://localhost>.

The first build takes several minutes — the worker image carries the full
extraction toolchain (Tesseract, Poppler, Ghostscript, OpenCV). Subsequent builds
are cached.

If a port is already taken on your machine, override it in `.env`
(`HTTP_HOST_PORT`, `API_HOST_PORT`, `POSTGRES_HOST_PORT`, …). Everything is
reachable through nginx on `:80` regardless; the rest are conveniences.

Run `make help` for the full target list.

## Architecture

| Service | Role |
|---|---|
| `nginx` | Reverse proxy, TLS-ready, upload limits, security headers, SSE pass-through |
| `frontend` | Next.js 15 · App Router · Tailwind v4 · shadcn-style primitives |
| `api` | FastAPI. Deliberately slim — it never touches a PDF's bytes |
| `worker-extract` | Heavy queue: extraction, OCR, table parsing. CPU-bound, scaled separately |
| `worker-default` | Light queue: categorisation, intelligence, notifications, exports |
| `scheduler` | Celery beat — analytics snapshots, subscription refresh, retention |
| `postgres` | Source of truth. Row Level Security, `NUMERIC(18,2)` money |
| `redis` | Broker, rate limiting, cache. Holds no financial records |
| `minio` | Encrypted statement storage, per-tenant keys, presigned URLs only |

Optional profiles: `--profile debug` (Flower), `--profile observability`
(Prometheus, Grafana, Loki), `--profile security` (ClamAV).

```
backend/app/
  core/           config, logging (+ redaction), security, errors
  intelligence/   Financial Intelligence Engine — deterministic, zero LLM
  extraction/     classifier, text/table extraction, OCR, bank detection
  privacy/        gateway, scrubbers, injection guard, output validator
  ai/             provider abstraction (Gemini implemented)
  assistant/      read-only tool registry and orchestrator
parsers/          bank parser registry + canonical transaction schema
workers/          Celery app and task modules
tools/            synthetic statement generator, accuracy harness, corpus tooling
```

## Invariants

These are enforced structurally, not by convention or code review.

| Invariant | Enforcement |
|---|---|
| No PII reaches an LLM | `AIPayload` forbids unknown fields — an account number is *unrepresentable*. The finished payload is re-scanned; any detection aborts the call. |
| LLM does no financial math | Every total, delta and trend is computed in `intelligence/`. The model receives finished numbers and only phrases them. |
| LLM has no DB/SQL/FS access | Categorisation passes zero tools. The assistant gets seven typed, server-executed functions. |
| Tenant isolation | `tenant_id` everywhere, plus PostgreSQL RLS. The app connects as a role created `NOSUPERUSER NOBYPASSRLS` — connecting as the owner would silently bypass every policy. |
| Verified corrections survive | Service guard *and* a database trigger rejecting AI writes to verified rows. |
| Originals are immutable | `original_*` columns are never updated; corrections go to `corrected_*`. |
| Money is exact | `NUMERIC(18,2)` → `Decimal` → **string** over JSON → string-based Indian grouping in the UI. No float anywhere. |
| Transfers aren't expenses | `is_expense` flag and paired `transfer_group_id`. |
| Duplicates can't land | Fingerprint over tenant, account, date, amount, direction and normalised description, with a `UNIQUE` constraint. Balance is deliberately *excluded* — the same transaction carries different balances across re-issued statements. |
| A statement is trusted only if it reconciles | `trust_status` reaches `trusted` only at an exact ₹0.00 delta. No tolerance band. |
| Logs carry no financial or personal data | Field allow-list plus value redaction, verified by a leakage test. |

### Logging

Log events may only carry: `request_id, user_id, tenant_id, job_id, statement_id,
account_id, transaction_id, bank_code, stage, status, duration_ms, count,
error_code, model_name` and a few bounded operational fields.

Never logged at any level, including tracebacks: amounts, balances, descriptions,
merchant strings, account or card numbers, UPI IDs, IFSC, PAN, Aadhaar, names,
emails, phones, filenames, PDF content, AI prompts or responses.

Anything not on the allow-list is dropped before rendering — only its *name* is
reported. Whatever survives is scrubbed by the same detectors the privacy gateway
uses. Third-party loggers are bridged through the same pipeline, so a library
that helpfully logs failing SQL with bound parameters is caught too.

### Authentication

Two credentials with different jobs. The **access token** is a 15-minute JWT held
in memory by the client — never in localStorage, where any injected script could
read it. The **refresh token** is opaque, random, stored only as a SHA-256 hash,
and delivered in an httpOnly cookie scoped to `/api/v1/auth`.

Refresh tokens rotate on every use and carry a family id. If a token that has
already been rotated is presented again, we cannot tell a racing client from a
thief — so the whole family is revoked and everyone re-authenticates. Rotation
without that check is close to decorative: someone who copies a token would
simply refresh it forever alongside the real user.

Two throttles guard login and they defend different things: a per-IP rate limit
stops one source hammering many accounts, and a per-account lockout stops many
sources hammering one account. An unknown email and a wrong password return an
identical response — the login form is not an account-existence oracle.

Three tables form the entire pre-authentication surface — `tenants`, `users` and
`refresh_tokens`. Each keeps RLS enabled but drops FORCE, so exactly one narrow
`SECURITY DEFINER` function per table can perform the single cross-tenant lookup
that resolving a credential requires. Everything else is fully policed.

### Statement storage

Statement PDFs are encrypted by the **application** with AES-256-GCM under a
per-tenant key derived from the master KEK, then handed to MinIO as ciphertext.
Deliberately not SSE-C: under server-side encryption the object store receives
the key on every request, whereas here it never sees a key at all. A
misconfigured bucket, a stray public policy or a backup copied somewhere wrong
yields nothing readable.

The trade-off is that a presigned URL would hand a browser ciphertext, so
downloads stream back through the API, which authorises and decrypts. That also
means every download is auditable rather than being a bearer URL that works for
whoever obtains it.

Uploads are validated **before** anything is stored. A PDF can carry JavaScript,
launch external programs, open a URL on load, embed arbitrary files and expand a
few kilobytes into gigabytes — so the file is scanned for those constructs and
refused at the door, and the worker re-validates whatever it reads back.

### Reading a statement

Deterministic, layered, and an LLM is **never** the extraction engine. Asked to
read a statement a language model will produce a plausible transaction list
whether or not it could see the numbers, and a plausible wrong number in a
ledger is worse than a loud failure.

```
PDF → text layer (PyMuPDF) → OCR only if a page has none (Tesseract)
    → tables (pdfplumber lines → pdfplumber text → Camelot lattice)
    → classify: bank statement vs credit card
    → detect issuer → dispatch → parse → canonical transactions
```

**Twelve parsers.** Dedicated: HDFC, ICICI, SBI, Axis. Generic-backed: Kotak,
IDFC, IndusInd, Yes Bank. Plus a generic bank parser, a generic card parser and
HDFC/ICICI card variants. A bank earns dedicated code by having a quirk worth
code, not by existing — everything else declares its name and inherits the
shared tabular reader.

Two readers underneath. Where extraction found a grid, columns are mapped by
header text and rows read positionally. Where it did not — always the case for a
scan — rows come from raw text lines and **direction is taken from the running
balance delta**, the one signal a scan cannot smear: if the balance fell, it was
a debit.

Issuer detection reads only the **masthead**, never the transaction table.
Narrations routinely name other banks — a UPI payment carries the payee's bank —
and scanning the table lets a counterparty out-score the actual issuer.

Categories come from a merchant dictionary plus deterministic rules, so the
product categorises an Indian statement correctly with `AI_ENABLED=false`.
Structure outranks identity: an Amazon refund has Amazon as its merchant and
*Refund* as its category, and is not spending.

### Accuracy

`make gen-fixtures` writes statement PDFs paired with `expected.json` ground
truth — written from the same ledger the PDF was rendered from, so it is the
document's source rather than a transcription of it. `make accuracy` scores the
parsers and **exits non-zero on a miss**. It is a gate, not a report.

The rule the harness exists to enforce: **every field metric divides by the
ground-truth transaction count, and a missing transaction counts as a failure in
every one of them.** A scorer that measures only the rows it extracted can
report 99.9% amount accuracy on a statement it read half of. Recall and
precision are reported separately and never averaged. `tests/accuracy/` mutation-
tests the harness itself — dropping rows, inventing rows, flipping directions —
because a green scorecard is worth nothing if the scorer cannot go red.

| Metric | Target | Measured (synthetic) |
|---|---|---|
| Transaction recall | ≥ 99% | 100% |
| Transaction precision | ≥ 99.5% | 100% |
| Date accuracy | ≥ 99.5% | 100% |
| Amount accuracy | ≥ 99.9% | 100% |
| Debit/Credit direction | ≥ 99.9% | 100% |
| Merchant normalization | ≥ 98% | 99.34% |
| Category assignment | ≥ 95% | 100% |
| **Financial reconciliation** | **100%, no tolerance** | **15/15** |

761 transactions across 15 fixtures, including a Dr/Cr single-column layout,
lakh-grouped six-figure amounts, six pages of brought/carried-forward lines, a
credit-card statement whose refunds print as credits, a re-issued duplicate, and
a scanned copy with no text layer at all.

### The trust layer

Between "we read a PDF" and "this is what happened to your money" sits one
ordered chain:

```
resolve account → reconcile → fingerprint → detect duplicates
    → score confidence → write → pair internal movements
```

**Reconciliation runs before insertion**, because its verdict is an input to
every row's confidence: if a statement does not add up, no transaction on it can
be fully trusted — including the ones that look immaculate, because the misread
might be that one. `trust_status` reaches `trusted` on an exact **₹0.00** delta
and nothing else. There is no tolerance band.

Three checks, because they fail differently. Totals catch a lost or invented row
but say nothing about where; running-balance continuity turns "₹4,955.94
unaccounted" into "row 23 on page 2"; page continuity catches a missing page
whose debits and credits happen to cancel.

**Every transaction is written, including the doubtful ones.** A statement that
reconciles must reconcile with all of its rows present — holding some back until
reviewed would leave the ledger unable to reproduce the arithmetic that made it
trustworthy. Doubt is expressed as `review_status`, never as absence.

### Duplicates

A fingerprint over `tenant + account + date + amount + direction + normalized
text`, with `UNIQUE (tenant_id, account_id, fingerprint)` — so a re-upload is
refused by PostgreSQL, not by whichever code path remembered to check.

**Running balance is deliberately excluded.** The same transaction carries
different balances across re-issued or overlapping statements, so folding
balance in would give an identical transaction a different hash and silently
defeat deduplication — the exact failure this ledger must not have.

**Genuine repeats are not duplicates.** Two ₹200 Swiggy orders on the same day
are two transactions, so identical rows within one statement are numbered by
occurrence — stable across re-uploads, distinct within a statement. A naive
fingerprint would reject the second one, losing real money from the ledger in
order to "protect" it.

Near-duplicates — same date and amount, narration drifted — are **flagged, never
dropped**. Both mistakes cost money, and only a person can settle which it is.

### The privacy perimeter

An AI model in this system sees **six fields**, and the guarantee is structural
rather than procedural. `AIPayload` is a Pydantic model with `extra="forbid"`,
so an account number is not filtered out — it is *unrepresentable*. There is no
argument you can pass to send one.

| sent | never sent |
|---|---|
| `merchant` (see below) | the PDF, or any part of it |
| `amount_bucket` — a range, never the amount | exact amounts and balances |
| `direction` | transaction descriptions |
| `payment_method` | names of people, including payees |
| `mcc_hint` | account/card numbers, UPI IDs, IFSC, PAN, Aadhaar, GSTIN |
| `day_of_week` — never the date | emails, phones, addresses, statement numbers |

**Which merchant names may be sent is decided by the payment rail, not by the
caller.** A name matched in the seeded dictionary is a business. An *unmatched*
name on a card rail is still a business — a card swipe happens at a registered
merchant — so it is sent, and that is what lets the model categorise shops the
dictionary has never heard of. An unmatched name on a transfer rail (IMPS,
NEFT, RTGS) is withheld, because that is where a counterparty is a person; those
transactions skip AI entirely and go to review.

There is no description field. An earlier version had one — letters only, digits
stripped — and it defeated the whole perimeter on its first test: for
`IMPS-…-RAHUL SHARMA-HDFC-…` the merchant was correctly withheld and the hint
sent the name anyway. Nothing about the shape of a name distinguishes "Rahul
Sharma" from "Rahul Sweets", so the field was removed rather than filtered.

**Everything fails closed.** Injection-shaped text is quarantined and skipped,
never sanitised and sent. A payload that trips a detector on the post-build
re-scan is abandoned, not cleaned and retried. A response that echoes an
identifier, returns an off-list category or contains a URL or tool-call shape is
rejected, not repaired. Every one of those paths routes the transaction to human
review and records an incident naming *which detector fired* — never what it
matched, because storing the evidence would itself be the leak.

The **Privacy Center** renders the allow-list from the payload model's own
fields, so the screen cannot claim a narrower perimeter than the code enforces.

### Categorisation

`User Rule → Verified Merchant Rule → Deterministic Rule → Historical Pattern →
AI → Other`

AI is the narrowest tier deliberately: it runs only where every deterministic
tier missed *and* the gateway is willing to send something. On the demo data,
**87 of 87 transactions are categorised with no model involved.** A correction
writes a standing rule at the top of the cascade, and a database trigger rejects
any AI-sourced write to a verified row — the model cannot overrule a person, by
construction rather than by policy.

### Financial intelligence

`backend/app/intelligence/` computes every number the dashboard, insights page
and budgets show. **No language model is invoked anywhere in the package**, and
a test asserts it: each figure is reproducible by a query a person can read, and
the suite recomputes every one independently in Python and asserts the two agree.

Three rules run through it. Only `is_expense` rows are spending, so transfers,
card settlements and cash withdrawals never inflate a total. Refunds are
reported rather than netted away silently — a ₹500 purchase and a ₹500 refund is
both facts, not one. And every aggregate carries a **data-quality block** saying
how many of its transactions came from statements that did not reconcile or are
still awaiting review, because a total built partly on unverified numbers is
still the best available answer, but presenting it without saying so would not
be.

**Subscriptions** are found by measuring the gaps between charges: at least
three occurrences, and intervals that agree with each other. Real monthly
billing scores ~0.97 on that consistency measure; takeaway ordered every couple
of weeks scores ~0.73, so the floor sits at 0.80. The next charge advances by
*calendar months* preserving the billing day — adding a median day count
predicts the 10th for a subscription billed on the 9th, and drifts further every
month.

**Anomalies are statistical outliers with stated reasons, never fraud claims.**
The system has no ground truth for fraud and every signal has innocent
explanations. Detection uses median and median absolute deviation rather than
mean and standard deviation: one rent payment in a month of groceries would drag
a mean far enough that nothing else could look unusual — and would make the rent
itself look normal.

Derived outputs are rebuilt nightly and after every import, never patched in
place: a subscription is a statement about a merchant's whole history, and
updating one incrementally is how a cancelled service stays "active" forever.

### The assistant

Seven read-only functions, and no eighth. `get_monthly_spending`,
`get_category_spending`, `get_transactions`, `get_top_merchants`,
`get_recurring_expenses`, `compare_months`, `get_anomalies` — each a thin
wrapper over the intelligence engine, so an answer and the chart beside it are
drawn from the same arithmetic.

**Identity is not something the model can express.** Every function's arguments
are a Pydantic model with `extra="forbid"` and no tenant, user or account field.
The session is scoped from the access token before the model is involved. An
attempt to pass `tenant_id` is a validation error rather than an ignored key,
because there is nowhere to put it.

**The model does no arithmetic.** Deltas, shares and percentages are computed
before it sees them — a comparison arrives as `change_rupees` already worked
out. Then every figure in the answer is checked against the figures the
functions returned, matched by kind: a rupee amount against the rupee amounts,
a percentage against the percentages. **An answer quoting a figure that came
from no function is discarded, not annotated.** The question is re-answered from
the same results by the code that produced them, so a rejected answer costs
phrasing rather than correctness — and a warning printed beside a confident
wrong number is not something people read.

The failure this catches is derivation, not fabrication. Given two category
totals and asked for their sum, a model will produce the right answer most of
the time; nothing downstream can tell which time it is.

**Payee names are filtered by payment rail.** A recognised business is named. An
unmatched name on a card, NACH or ACH rail is a business — you cannot swipe a
card at a person, and a direct-debit mandate is registered by a corporate — so
it is named too. Everything else reaches the model as "an unnamed payee" while
keeping its amount, because dropping the row would make a total wrong. You still
see the real name on screen: the restriction is on what leaves, not on what you
may read about your own money.

**It works with no API key, which is the default.** Questions are matched to a
function by an explicit rule cascade and answered from the ledger; the same
deterministic renderer is the fallback when a model's wording is rejected. Every
answer is labelled with where its wording came from, and carries an "Open in
Transactions" link that reproduces it as real filters — the practical way to
check a sentence rather than trust it.

The monthly narrative on Insights follows the same rule: it is written from the
stored snapshot, never from the ledger, so the paragraph and the cards beneath it
cannot disagree. It is null whenever AI is off, and the screen renders the same
report either way.

### Confidence

Four independent scores per transaction — `extraction`, `merchant`, `category`,
`validation` — and **the gate is `min()`, never an average**. It is a generated
column in PostgreSQL (`LEAST(...)`), so the definition cannot drift from the
code that reads it.

Why it matters concretely: a row with amount confidence 0.89 and everything else
near-perfect averages to *exactly* 0.97. A blended gate would auto-approve a
probably-misread amount as settled fact; the minimum sends it to review.

- `≥ 0.97` approved automatically
- `0.90 – 0.97` flagged, but counted in every total
- `< 0.90` held for review

The detail panel shows all four and names the weakest, because a reviewer needs
to know *what* to check.

### Observability

`docker compose --profile observability up -d` adds Prometheus, Grafana, Loki
and promtail. Grafana is on `localhost:3001` with one provisioned dashboard —
provisioned from a file, so the screen an operator opens during an incident is
reviewable in a diff rather than living in a volume.

**Every label is a shape, never a value.** A bank code, a stage, a review
status, an outcome. No merchant, no amount, no tenant id appears on any series:
a metrics endpoint is unauthenticated by convention, and a label would make it a
data leak with a fifteen-second scrape interval. It is also why there is no
per-tenant AI-cost series — that would put the customer list in `/metrics` and
grow the series count with the business.

Workers are scraped like anything else. Celery forks, and a counter incremented
in a forked child is invisible to the parent, so the worker containers run
`prometheus_client` in multiprocess mode and serve `/metrics` on port 9100 from
a shared sample directory. Without that, every extraction, dedup and AI counter
would export a confident zero.

**Counters are process-lifetime; the durable numbers come from PostgreSQL.**
Review-queue depth, ledger size and unreconciled statement count are read back
on a timer via `ops_platform_counters()` — a `SECURITY DEFINER` function that
takes no arguments and returns three integers. Row Level Security correctly
refuses an unscoped session, and the fix for that is one narrow, auditable
exemption returning aggregates, not a connection that can read everyone's rows.

### Taking your data out, and deleting it

Export is CSV, JSON or PDF, filtered by whatever the Transactions screen is
filtered by, so the file and the table agree. It is **streamed, never stored**:
the obvious design writes the file to object storage and returns a presigned
URL, which puts a plaintext copy of an entire financial history at rest for as
long as a retention sweep takes to notice it. Money is written as an exact
decimal string — `1234.56`, not `₹1,234.56` — because the second is prettier and
the first is the one a spreadsheet adds up correctly. The PDF is the exception;
it is for reading, so it is formatted.

Deletion is real deletion. `DELETE /auth/account` needs the account password
*and* the exact phrase `DELETE MY DATA`, then removes the stored PDFs first and
the rows second — the storage keys live in the rows, so the other order strands
financial documents in a bucket with nothing left that knows they exist. The
audit trail goes too, including the entry recording the request; the append-only
trigger permits `DELETE` only when `app.allow_audit_purge` is set for the
transaction, and erasure is what sets it.

Retention runs nightly against the configured windows and **cannot remove a
transaction** — there is a test asserting that. It deletes the objects belonging
to statements it removed, reading their keys before the rows go.

There is a separate job that compares object storage against the database, and
it is deliberately timid: it **reports by default and deletes only when asked**,
and refuses outright if it enumerated no tenants, if nothing looks referenced,
or if it would delete more than a quarter of the bucket. That shape is not
caution for its own sake — see the note in the limitations below.

## Development

```bash
make gen-fixtures       # synthetic statement PDFs + ground truth
make accuracy           # score the parsers; non-zero exit on a miss
make validate-real      # same harness over your own redacted corpus
make test               # the whole backend suite
make test-parsers       # parser, extraction and harness tests only
make test-privacy       # perimeter, assistant traceability, log leakage
make test-security      # tenant isolation, RLS, schema invariants
make lint               # ruff over backend, workers, parsers, tools, tests
make typecheck          # tsc over the frontend
make metrics            # the platform gauges, as Prometheus sees them
make retention          # apply retention windows now
make reconcile-objects  # report unreferenced stored objects (never deletes)
make up-observability   # add Prometheus, Grafana, Loki
make logs-api           # tail API logs
make logs-worker        # tail worker logs
make shell-db           # psql as the owner role
make down               # stop (data preserved)
make reset              # stop and DESTROY all volumes
```

Backend and frontend source are bind-mounted with hot reload; code changes do not
need a rebuild. Dependency changes do (`make up` rebuilds).

## Build status

| Phase | Scope | Status |
|---|---|---|
| **P0** | Foundation, Docker, redacting logger, design system, app shell, 17 routes | **Done** |
| **P1** | 29 models, migrations, RLS policies, integrity triggers, seeds | **Done** |
| **P2** | Argon2id auth, rotating refresh tokens with reuse detection, CSRF, rate limiting, tenant scoping, login/signup UI | **Done** |
| **P3** | Multi-file upload, PDF structural validation, AES-GCM encrypted storage, Celery job pipeline, live SSE progress, Statement Health | **Done** |
| **P4** | PyMuPDF/pdfplumber/Camelot extraction, OCR fallback, 12 parsers, merchant dictionary, deterministic rules, statement generator, accuracy harness | **Done** |
| P4.5 | Real-statement validation against your own redacted corpus | — |
| **P5** | Exact reconciliation, fingerprint dedup, movement detection, four-dimensional confidence, Transactions ledger, Review Center, Accounts | **Done** |
| **P6** | Privacy gateway, allow-list payload, injection guard, output validator, Gemini provider, cascade with AI tier, Privacy Center | **Done** |
| **P7** | Deterministic analytics, subscription detection, statistical anomalies, budgets, forecasting, timeline, insights, nightly scheduler; Dashboard, Insights, Subscriptions, Budgets, Timeline | **Done** |
| **P8** | Seven read-only tools, server-injected identity, traceability post-check, deterministic fallback, Assistant screen, dashboard query box, monthly narrative from snapshots | **Done** |
| **P9** | Prometheus metrics across the pipeline, provisioned Grafana dashboard, CSV/JSON/PDF export, retention and account erasure, notifications, audit log, categories and rules, settings | **Done** |

## Known limitations

- **A reconciliation job deleted every stored PDF in the development
  environment while this phase was being built, and the guards described above
  exist because of it.** The job built its set of live objects by enumerating
  tenants on an unscoped session. Row Level Security is *enabled* on `tenants`,
  so that query returns zero rows rather than all of them — it did not fail, it
  quietly concluded there were no tenants, decided every object in the bucket
  was unreferenced, and deleted them. The ledger itself was untouched; only the
  source PDFs went, and in a development environment they were regenerable
  fixtures. The query was one line to fix. What needed rethinking was a
  destructive job that treated "I found nothing" as a valid basis for deleting
  everything, so it now reports instead of deleting, refuses on four
  independent signals, and reads tenants through a `SECURITY DEFINER` function
  where an empty answer genuinely means empty. Each guard is tested
  individually, and each would have been sufficient alone.
- **Synthetic accuracy is not real-world accuracy, and 100% does not mean what
  it looks like.** Those fixtures were authored alongside the parsers, so they
  contain only the layout quirks we already thought of. Real statements fail on
  rotated pages, templates changed mid-period, footer noise bleeding into the
  table and per-branch formatting drift. A green synthetic scorecard says the
  framework is correct against layouts we wrote — nothing more. P4.5 closes that
  gap and needs a corpus of your own redacted statements; until one exists
  `make validate-real` reports `no corpus supplied` rather than a false pass.
- **A statement that prints no balances cannot be trusted or accused.** It
  stays `pending` with a health note saying its arithmetic could not be checked,
  and its rows never auto-approve. "Nobody checked" and "the arithmetic holds"
  are different claims and the system never conflates them.
- **OCR is a fallback, not a peer.** Scanned statements are read, but OCR
  misreads digits, and the guarantee is not that it never does — it is that a
  misread statement fails reconciliation and is never silently trusted.
- **Gemini is the only implemented AI provider.** OpenAI, Azure OpenAI, Anthropic
  and Azure AI Foundry are registered classes implementing the same ABC and
  raising `NotImplementedError`; adding one is a single file, because the
  gateway, the payload model and the output validator are provider-independent.
- **Dropping PDFs on the upload page is the only implemented ingestion source.**
  CSV, a direct bank API and RBI Account Aggregator are reserved the same way —
  real classes implementing `IngestionSource`, raising `NotImplementedError`,
  and matching the four values the `ingestion_sources` CHECK constraint allows.
  A source produces bytes and provenance and nothing else, so everything after
  it — validation, storage, extraction, reconciliation, dedup, confidence —
  applies unchanged. **There is no trusted-source flag and there will not be
  one**: data arriving over an authenticated API still has to prove its
  arithmetic. Two consequences are written down in
  `app/ingestion/sources/_future.py` rather than left to be discovered: a CSV
  has already lost the running balance, so it could never reconcile and would
  enter the ledger permanently marked unverifiable; and an Account Aggregator
  delivers structured data rather than a printed statement, so the
  reconciliation guarantee would have to come from the provider's own totals or
  be honestly downgraded.
- **AI is off by default and the product is complete without it.** `AI_ENABLED`
  defaults to false, and enabled-without-a-key is treated as disabled rather
  than as an error, so a misconfigured deployment degrades to deterministic
  categorisation instead of failing uploads.
- **Google OAuth is off by default** — it needs your own client credentials.
- **ClamAV is optional** (~1 GB). The default gate is structural PDF analysis.
- **Anomalies are statistical outliers with stated reasons**, never fraud claims.
- **Projections are arithmetic, and are labelled as such.** A month-end figure is
  a run rate from elapsed days plus known upcoming charges. Before a week has
  passed it is marked an early estimate rather than shown as a confident number.
- **The dashboard defaults to the latest month with data, not the current one.**
  Statements arrive after the period they cover, so defaulting to "this month"
  would show most users an empty dashboard on most days.

## Security

Report security issues privately rather than opening a public issue. Never commit
`.env`, and never place a real bank statement anywhere but `tests/fixtures/real/`,
which is gitignored and never leaves your machine.
