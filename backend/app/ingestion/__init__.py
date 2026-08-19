"""Ingestion sources: where statements come from.

One implemented (``pdf_upload``), three reserved. See ``base.py`` for the
contract and why the reserved ones are classes rather than a to-do list.
"""

from __future__ import annotations

from app.ingestion.base import IngestedDocument, IngestionSource, SourceUnavailable
from app.ingestion.sources._future import RESERVED
from app.ingestion.sources.pdf_upload import PdfUploadSource

__all__ = [
    "IngestedDocument",
    "IngestionSource",
    "SourceUnavailable",
    "PdfUploadSource",
    "implemented_sources",
    "known_sources",
    "get_source",
]


def _registry() -> dict[str, type[IngestionSource]]:
    return {
        PdfUploadSource.source_type: PdfUploadSource,
        **{source.source_type: source for source in RESERVED},
    }


def implemented_sources() -> list[str]:
    return [PdfUploadSource.source_type]


def known_sources() -> list[str]:
    return sorted(_registry())


def get_source(source_type: str) -> IngestionSource:
    """Instantiate a source by name.

    A reserved source constructs fine and fails on ``fetch``, deliberately: the
    Statements screen can list what exists without every reserved name being an
    exception waiting to happen.
    """
    source = _registry().get(source_type)
    if source is None:
        raise SourceUnavailable(f"No ingestion source registered under {source_type!r}.")
    return source()
