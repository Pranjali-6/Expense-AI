"""Domain enumerations.

Every one of these is persisted as ``VARCHAR`` with a ``CHECK`` constraint
rather than a native PostgreSQL ``ENUM`` type.

Native enums are more compact and give nicer error messages, but they make
schema evolution genuinely painful: ``ALTER TYPE ... ADD VALUE`` has transaction
restrictions, values cannot be removed, and reordering is impossible. This
system expects to grow — new banks, new payment rails, new movement types — so
a CHECK constraint that any migration can simply redefine is the better trade.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------- identity --

class UserRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING_DELETION = "pending_deletion"


class AuthProvider(StrEnum):
    PASSWORD = "password"
    GOOGLE = "google"


# --------------------------------------------------------------- accounts --

class AccountType(StrEnum):
    SAVINGS = "savings"
    CURRENT = "current"
    CREDIT_CARD = "credit_card"
    WALLET = "wallet"
    LOAN = "loan"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    CLOSED = "closed"


# ------------------------------------------------------------- ingestion --

class IngestionSourceType(StrEnum):
    """How transactions entered the system.

    Only ``PDF_UPLOAD`` is implemented. The rest are reserved so the pluggable
    ingestion model is real rather than aspirational — adding a source is a new
    ``IngestionSource`` implementation, not a schema change.
    """

    PDF_UPLOAD = "pdf_upload"
    CSV = "csv"
    API = "api"
    ACCOUNT_AGGREGATOR = "account_aggregator"


class DocumentType(StrEnum):
    BANK_STATEMENT = "bank_statement"
    CREDIT_CARD_STATEMENT = "credit_card_statement"
    UNKNOWN = "unknown"


class StatementStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"

    #: Stored, but encrypted with a password we do not have. Not a failure —
    #: the file is intact and one correct password away from processing, so it
    #: waits for the user rather than being discarded. See
    #: `services.statements.unlock_statement`.
    PASSWORD_REQUIRED = "password_required"


class TrustStatus(StrEnum):
    """Whether a statement's arithmetic holds.

    ``TRUSTED`` requires an exact ₹0.00 reconciliation. There is no tolerance
    band: a statement reconciles or it does not.
    """

    PENDING = "pending"
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ExtractionMethod(StrEnum):
    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    HYBRID = "hybrid"


# ------------------------------------------------------------------ jobs --

class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    CATEGORIZING = "categorizing"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------- transactions --

class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class PaymentMethod(StrEnum):
    UPI = "upi"
    NEFT = "neft"
    IMPS = "imps"
    RTGS = "rtgs"
    CARD = "card"
    ATM = "atm"
    CHEQUE = "cheque"
    ACH = "ach"
    NACH = "nach"
    CASH = "cash"
    NETBANKING = "netbanking"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class MovementType(StrEnum):
    """What kind of money movement this is.

    Everything other than ``EXPENSE`` and ``INCOME`` is excluded from spending
    totals via ``Transaction.is_expense``. Counting a credit-card payment as an
    expense alongside the card purchases it settles is the classic way personal
    finance tools silently double-count.
    """

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    CREDIT_CARD_PAYMENT = "credit_card_payment"
    REFUND = "refund"
    SALARY = "salary"
    INVESTMENT = "investment"
    EMI = "emi"
    CASH_WITHDRAWAL = "cash_withdrawal"
    BANK_CHARGE = "bank_charge"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    AUTO_APPROVED = "auto_approved"
    FLAGGED = "flagged"
    REVIEW_REQUIRED = "review_required"
    RESOLVED = "resolved"


class CategorySource(StrEnum):
    """Which cascade tier decided the category.

    Stored on every transaction so the UI can always answer "why was this
    categorised this way?" without re-deriving anything.
    """

    USER_RULE = "user_rule"
    VERIFIED_MERCHANT_RULE = "verified_merchant_rule"
    DETERMINISTIC_RULE = "deterministic_rule"
    HISTORICAL_PATTERN = "historical_pattern"
    AI_MODEL = "ai_model"
    FALLBACK_OTHER = "fallback_other"


class ActorKind(StrEnum):
    """Who performed a write.

    Set as the ``app.actor_kind`` session GUC. The verified-correction trigger
    reads it to reject AI writes to rows a human has confirmed.
    """

    USER = "user"
    SYSTEM = "system"
    AI = "ai"


# --------------------------------------------------------- intelligence --

class SubscriptionCadence(StrEnum):
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    ANNUAL = "annual"


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    LAPSED = "lapsed"
    CANCELLED = "cancelled"


class BudgetPeriod(StrEnum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class AnomalyKind(StrEnum):
    """Statistical outlier types.

    Deliberately never "fraud": the system has no ground truth for that claim,
    and every one of these has innocent explanations.
    """

    AMOUNT_OUTLIER = "amount_outlier"
    CATEGORY_SPIKE = "category_spike"
    MERCHANT_FIRST_LARGE = "merchant_first_large"
    DUPLICATE_PROXIMITY = "duplicate_proximity"
    UNUSUAL_FREQUENCY = "unusual_frequency"


class TimelineEventKind(StrEnum):
    TRANSACTION = "transaction"
    LARGE_TRANSACTION = "large_transaction"
    STATEMENT_IMPORT = "statement_import"
    BUDGET_BREACH = "budget_breach"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"
    ANOMALY = "anomaly"


# ---------------------------------------------------------------- system --

class NotificationKind(StrEnum):
    STATEMENT_PROCESSED = "statement_processed"
    STATEMENT_FAILED = "statement_failed"
    RECONCILIATION_FAILED = "reconciliation_failed"
    REVIEW_REQUIRED = "review_required"
    BUDGET_BREACH = "budget_breach"
    ANOMALY_DETECTED = "anomaly_detected"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"


class AuditAction(StrEnum):
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    REGISTER = "register"
    PASSWORD_CHANGE = "password_change"
    STATEMENT_UPLOAD = "statement_upload"
    STATEMENT_DELETE = "statement_delete"
    STATEMENT_REPROCESS = "statement_reprocess"
    TRANSACTION_EDIT = "transaction_edit"
    TRANSACTION_APPROVE = "transaction_approve"
    RULE_CREATE = "rule_create"
    RULE_DELETE = "rule_delete"
    ACCOUNT_DELETE = "account_delete"
    BUDGET_CHANGE = "budget_change"
    EXPORT = "export"
    AI_TOGGLE = "ai_toggle"
    DATA_DELETE = "data_delete"


class PrivacyIncidentKind(StrEnum):
    """Why a privacy control fired.

    Every one of these aborts the AI call — the system fails closed and routes
    the transaction to human review rather than retrying with a cleaner payload.
    """

    PII_IN_PAYLOAD = "pii_in_payload"
    INJECTION_QUARANTINED = "injection_quarantined"
    OUTPUT_PII_ECHO = "output_pii_echo"
    OUTPUT_SCHEMA_VIOLATION = "output_schema_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    #: An assistant answer quoted a figure that appears in no tool result.
    #: Recorded, and the prose discarded — see app/assistant/traceability.py.
    OUTPUT_UNTRACEABLE_FIGURE = "output_untraceable_figure"


class AccuracyCorpus(StrEnum):
    SYNTHETIC = "synthetic"
    REAL = "real"
