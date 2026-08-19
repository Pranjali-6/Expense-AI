"""Sources that are reserved, not implemented.

Each is a real class implementing :class:`~app.ingestion.base.IngestionSource`
and raising ``NotImplementedError`` from ``fetch``. They are registered so the
Statements screen can say what exists and what is implemented, and so selecting
an unimplemented one fails with a clear message rather than an ImportError.

Building one means implementing ``fetch`` and ``available`` and registering it
in :func:`app.ingestion.get_source`. Nothing else changes: validation, storage,
extraction, reconciliation, deduplication and confidence scoring are all
source-independent by construction, and a document arriving from an aggregator
goes through every one of them exactly as an uploaded PDF does.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from app.ingestion.base import IngestedDocument, IngestionSource


class _ReservedSource(IngestionSource):
    """Shared body for the reserved sources."""

    def available(self) -> bool:
        return False

    async def fetch(
        self, *, tenant_id: uuid.UUID, since: datetime | None = None
    ) -> AsyncIterator[IngestedDocument]:
        raise NotImplementedError(
            f"The {self.source_type} source is reserved but not implemented. "
            "Implement fetch() and register it in app.ingestion.get_source."
        )
        yield  # pragma: no cover — makes this an async generator


class CsvUploadSource(_ReservedSource):
    """A CSV export from a bank's net-banking portal.

    The hard part is not reading CSV, it is that a CSV has already lost the
    running balance and the printed opening and closing figures — so a CSV
    import cannot reconcile, and would have to enter the ledger permanently
    marked as unverifiable rather than pending.
    """

    source_type = "csv"


class BankApiSource(_ReservedSource):
    """A direct bank API, where one exists.

    Rare in India outside corporate banking, and the reason the Account
    Aggregator framework exists at all.
    """

    source_type = "api"


class AccountAggregatorSource(_ReservedSource):
    """RBI Account Aggregator — consent-based, user-authorised data sharing.

    The one reserved source that would materially change the product: it
    removes the upload step entirely. It also changes the trust story, because
    an aggregator delivers structured data rather than a printed statement —
    there is no running balance column to check a parse against, so the
    reconciliation guarantee would have to come from the FIP's own totals or be
    honestly downgraded. That is a design decision, not an implementation
    detail, which is why it is written down here rather than discovered later.
    """

    source_type = "account_aggregator"
    supports_polling = True


RESERVED = (CsvUploadSource, BankApiSource, AccountAggregatorSource)
