"""Celery task registry.

Task modules are imported here explicitly so the live task set is greppable
rather than a product of import-order side effects. Added phase by phase:

    P3  ingest       upload validation, encrypted storage, job orchestration
    P4  extract      text/table extraction, OCR fallback, parser dispatch
    P5  trust        reconciliation, dedup, movement detection, confidence
    P6  categorize   deterministic cascade, privacy gateway, AI enrichment
    P7  intelligence scheduled analytics, subscriptions, anomalies, snapshots
    P8  narrative    monthly prose from stored snapshots (skipped with AI off)
    P9  maintenance  retention sweeps, orphan purge, account erasure
"""

from __future__ import annotations

from workers.tasks import ingest, intelligence, maintenance, narrative

__all__ = ["ingest", "intelligence", "maintenance", "narrative"]
