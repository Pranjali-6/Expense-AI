"""Object storage client (MinIO / S3-compatible).

Statement PDFs live here and nowhere else — never on a worker's local disk
beyond the lifetime of a single task, and never in PostgreSQL. Objects are
encrypted per tenant (SSE-C, key derived from ``STORAGE_MASTER_KEK`` via HKDF,
implemented in P3) and are reachable only through short-lived presigned URLs
issued after an authorization check.
"""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache
from uuid import UUID

from minio import Minio

from app.core.config import settings


@lru_cache(maxsize=1)
def get_storage() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=settings.MINIO_SECURE,
    )


def derive_tenant_key(tenant_id: UUID | str) -> bytes:
    """Derive a per-tenant 256-bit encryption key from the master KEK.

    HKDF-Expand with the tenant id as the info parameter. Per-tenant keys mean
    a leaked object plus a leaked key compromises one tenant, not all of them,
    and they make per-tenant key rotation possible later without re-encrypting
    the whole bucket.
    """
    master = settings.STORAGE_MASTER_KEK.encode("utf-8")
    info = f"expense-ai:statement:{tenant_id}".encode("utf-8")

    # HKDF-Expand (RFC 5869) with a single 32-byte output block.
    return hmac.new(master, info + b"\x01", hashlib.sha256).digest()


def statement_object_key(tenant_id: UUID | str, statement_id: UUID | str) -> str:
    """Object keys carry ids only — never a filename, a bank name or a period.

    An object listing should reveal nothing about its contents.
    """
    return f"tenants/{tenant_id}/statements/{statement_id}.pdf"


def ping_storage() -> bool:
    try:
        get_storage().bucket_exists(settings.MINIO_BUCKET_STATEMENTS)
        return True
    except Exception:
        return False
