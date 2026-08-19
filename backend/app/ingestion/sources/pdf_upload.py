"""The one implemented source: a person dropping files on the upload page.

Thin to the point of looking pointless, and that is the point. The upload
endpoint already receives the bytes, so this class does not fetch anything — it
exists to make the *shape* of a source concrete, and to prove the contract in
``base.py`` was derived from a working case rather than imagined.

``supports_polling`` is False and always will be. There is nothing to poll: the
trigger is a human action, which is exactly what distinguishes this source from
every reserved one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone

from app.ingestion.base import IngestedDocument, IngestionSource


class PdfUploadSource(IngestionSource):
    source_type = "pdf_upload"
    supports_polling = False

    def __init__(self, uploads: Iterable[tuple[str, bytes]] | None = None) -> None:
        self._uploads = list(uploads or [])

    def available(self) -> bool:
        return True

    async def fetch(
        self, *, tenant_id: uuid.UUID, since: datetime | None = None
    ) -> AsyncIterator[IngestedDocument]:
        """Yield what the request already carried.

        ``since`` is ignored, and honestly so: a browser upload has no history
        to filter. A source that silently accepted a parameter it cannot honour
        would be worse than one that documents the omission.
        """
        for filename, content in self._uploads:
            yield IngestedDocument(
                content=content,
                filename=filename,
                provenance={"source": "browser_upload"},
                fetched_at=datetime.now(timezone.utc),
            )
