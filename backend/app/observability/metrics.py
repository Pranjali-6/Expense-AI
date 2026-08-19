"""Prometheus metrics.

Defined centrally in P0 so later phases only increment. Every metric here is a
count, a duration or a ratio — no label carries a merchant, a description or an
amount, which would turn the metrics endpoint into a data leak with a scrape
interval.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --------------------------------------------------------------------- HTTP --

http_requests_total = Counter(
    "expense_http_requests_total",
    "HTTP requests handled.",
    labelnames=("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "expense_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "path"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# --------------------------------------------------------------- extraction --

extraction_duration_seconds = Histogram(
    "expense_extraction_duration_seconds",
    "End-to-end statement extraction time.",
    labelnames=("bank_code", "document_type"),
    buckets=(1, 2.5, 5, 10, 20, 40, 80, 160, 320),
)

extraction_failures_total = Counter(
    "expense_extraction_failures_total",
    "Statement extractions that failed.",
    labelnames=("bank_code", "stage", "error_code"),
)

ocr_pages_total = Counter(
    "expense_ocr_pages_total",
    "Pages that required the OCR fallback (text layer was insufficient).",
    labelnames=("bank_code",),
)

pages_processed_total = Counter(
    "expense_pages_processed_total",
    "Statement pages processed, whether by text layer or OCR.",
    labelnames=("bank_code", "method"),
)

# ------------------------------------------------------------- trust layer --

validation_failures_total = Counter(
    "expense_validation_failures_total",
    "Statements that failed reconciliation and did not become trusted.",
    labelnames=("bank_code", "check"),
)

reconciliation_delta_paise = Histogram(
    "expense_reconciliation_delta_paise",
    "Absolute reconciliation discrepancy, in paise. Zero is the only pass.",
    buckets=(0, 1, 100, 10_000, 100_000, 1_000_000),
)

duplicates_detected_total = Counter(
    "expense_duplicates_detected_total",
    "Transactions rejected as duplicates of an existing ledger row.",
    labelnames=("method",),  # fingerprint | fuzzy
)

transactions_ingested_total = Counter(
    "expense_transactions_ingested_total",
    "Transactions committed to the ledger.",
    labelnames=("bank_code", "review_status"),
)

# Gauges declare a multiprocess mode so they still mean something when the
# process is forked. Without one, prometheus_client refuses to export a gauge
# in multiprocess mode at all — the worker containers run that way.
review_queue_depth = Gauge(
    "expense_review_queue_depth",
    "Transactions currently awaiting human review, across all tenants.",
    multiprocess_mode="livemax",
)

untrusted_statements = Gauge(
    "expense_untrusted_statements",
    "Statements that did not reconcile and are excluded from AI narrative input.",
    multiprocess_mode="livemax",
)

ledger_transactions = Gauge(
    "expense_ledger_transactions",
    "Transactions in the ledger. Read from PostgreSQL, so a restart does not "
    "reset it the way a process-lifetime counter would.",
    multiprocess_mode="livemax",
)

# ----------------------------------------------------------- categorization --

categorization_total = Counter(
    "expense_categorization_total",
    "Category assignments by cascade tier.",
    labelnames=("category_source",),
)

user_corrections_total = Counter(
    "expense_user_corrections_total",
    "Category corrections made by users — the accuracy signal that matters.",
    labelnames=("previous_source",),
)

# ------------------------------------------------------------------- ai/llm --

ai_calls_total = Counter(
    "expense_ai_calls_total",
    "LLM calls attempted.",
    labelnames=("provider", "model_name", "purpose", "outcome"),
)

ai_call_duration_seconds = Histogram(
    "expense_ai_call_duration_seconds",
    "LLM call latency.",
    labelnames=("provider", "model_name"),
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
)

ai_cost_inr_total = Counter(
    "expense_ai_cost_inr_total",
    "Cumulative estimated LLM spend, in rupees.",
    labelnames=("provider", "model_name"),
)

ai_tokens_total = Counter(
    "expense_ai_tokens_total",
    "Tokens consumed.",
    labelnames=("provider", "model_name", "direction"),
)

# ------------------------------------------------------------------ privacy --

privacy_payloads_blocked_total = Counter(
    "expense_privacy_payloads_blocked_total",
    "Payloads that failed the post-build re-scan and were never sent.",
    labelnames=("detector",),
)

prompt_injection_quarantined_total = Counter(
    "expense_prompt_injection_quarantined_total",
    "Inputs quarantined by the injection heuristic; AI was skipped entirely.",
)

# ------------------------------------------------------------------- queues --

celery_queue_depth = Gauge(
    "expense_celery_queue_depth",
    "Tasks waiting in a Celery queue.",
    labelnames=("queue",),
    multiprocess_mode="livemax",
)

job_stage_duration_seconds = Histogram(
    "expense_job_stage_duration_seconds",
    "Time spent in each pipeline stage.",
    labelnames=("stage",),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 180),
)

# -------------------------------------------------------------- dependencies --

dependency_up = Gauge(
    "expense_dependency_up",
    "1 when a dependency responded to its health probe, 0 otherwise.",
    labelnames=("dependency",),
    multiprocess_mode="livemin",
)


# ------------------------------------------------------------------ exports --

exports_total = Counter(
    "expense_exports_total",
    "Exports generated, by format.",
    labelnames=("format",),
)

export_rows_total = Counter(
    "expense_export_rows_total",
    "Transaction rows written to exports.",
)

# ---------------------------------------------------------------- retention --

retention_deleted_total = Counter(
    "expense_retention_deleted_total",
    "Rows removed by the retention sweep.",
    labelnames=("table",),
)

account_deletions_total = Counter(
    "expense_account_deletions_total",
    "Accounts erased on request, and whether the erasure completed.",
    labelnames=("status",),
)
