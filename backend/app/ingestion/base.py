"""How statements get into the system.

Today there is exactly one way: a person drops PDFs onto the upload page. The
schema has always allowed for three more — ``ingestion_sources.source_type``
permits ``csv``, ``api`` and ``account_aggregator`` — and this is the interface
that makes that allowance real rather than aspirational.

The distinction matters. A reserved *column value* with no code behind it is a
guess about a future shape, and guesses in a schema age badly. A reserved
*interface*, with one real implementation behind it, has already been tested
against a working case: the contract below is not what a PDF upload might need,
it is what a PDF upload actually needed.

What the contract says, and why each part is there:

**A source produces bytes and provenance, nothing else.** It does not parse, it
does not reconcile, it does not write a transaction. Everything downstream of
``fetch`` is identical for every source, which is the property that makes adding
one cheap — an Account Aggregator connection would produce the same
``IngestedDocument`` a file upload does, and the entire trust chain would apply
to it unchanged.

**Every source is subject to the same validation.** There is no "trusted
source" flag and there will not be one. Data arriving over an authenticated API
still gets structural PDF validation, still gets reconciled, still has to prove
its arithmetic before its rows are trusted. A source that vouched for itself
would be a source whose bugs become the ledger's bugs.

**Nothing here is a partial implementation.** The reserved sources raise
``NotImplementedError`` from their one required method. They exist so that the
abstraction is demonstrably source-shaped rather than PDF-shaped with an
interface drawn around it afterwards — the difference shows up the day a second
source is added, and it is the difference between one file and a refactor.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class IngestedDocument:
    """One document, as any source hands it over.

    Bytes plus provenance. Deliberately not a parsed statement: parsing is the
    pipeline's job and doing it here would give each source its own opportunity
    to be subtly wrong about what a transaction is.
    """

    #: The raw file. Validated by the pipeline, never trusted because of where
    #: it came from.
    content: bytes
    #: What the source calls it. Used for nothing but display, and never logged
    #: — a filename routinely carries a name, an account number or both.
    filename: str
    content_type: str = "application/pdf"
    #: Free-form provenance for the audit trail: a connection id, a mailbox
    #: message id, an API cursor. Must contain no credentials and no financial
    #: values, for the same reason audit details are allow-listed.
    provenance: dict[str, Any] = field(default_factory=dict)
    fetched_at: datetime | None = None


class IngestionSource(ABC):
    """Somewhere statements come from."""

    #: Matches ``ingestion_sources.source_type``.
    source_type: str = "unknown"

    #: Whether this source can run without a person present. A PDF upload
    #: cannot — someone has to drop the file — while an Account Aggregator
    #: connection can, which is what would make it worth scheduling.
    supports_polling: bool = False

    @abstractmethod
    async def fetch(
        self, *, tenant_id: uuid.UUID, since: datetime | None = None
    ) -> AsyncIterator[IngestedDocument]:
        """Yield documents to ingest.

        An async iterator rather than a list: a mailbox or an aggregator may
        have a year of statements, and materialising all of them in memory to
        hand back one list is a design that works until the first heavy user.
        """
        raise NotImplementedError

    def available(self) -> bool:
        """Whether this source is configured well enough to be used."""
        return False


class SourceUnavailable(RuntimeError):
    """Raised when a named source exists but is not implemented or configured."""
