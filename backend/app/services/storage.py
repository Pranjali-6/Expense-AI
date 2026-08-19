"""Encrypted statement storage.

Statement PDFs are encrypted **by the application** with AES-256-GCM before
they are handed to MinIO, using a key derived per tenant from the master KEK.

This is deliberately not SSE-C. Under server-side encryption the object store
receives the key on every request and does the work; here it receives ciphertext
and never sees a key at all. A compromised or misconfigured bucket, a stray
public policy, a backup copied to the wrong place — none of them yield a
readable statement. It also happens to work over plain HTTP in development,
where the SDK refuses SSE-C outright.

The cost is that a presigned URL would hand a browser ciphertext, so downloads
are streamed back through the API, which decrypts after an authorization check.
That is a fair trade: it means every download is authorised and auditable
rather than being a bearer URL that works for anyone who obtains it.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from minio.error import S3Error

from app.core.config import settings
from app.core.errors import NotFoundError, ServiceUnavailableError
from app.core.logging import get_logger
from app.core.storage import derive_tenant_key, get_storage, statement_object_key

logger = get_logger(__name__)

#: AES-GCM nonce. 96 bits is the size the mode is designed around; anything
#: else forces an internal rehash and buys nothing.
NONCE_BYTES = 12

#: Stored layout: nonce ‖ ciphertext ‖ tag (the tag is appended by AESGCM).
#: A version byte would let the scheme change later without a migration, so
#: one is written even though there is only one scheme today.
FORMAT_VERSION = b"\x01"


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


def _encrypt(plaintext: bytes, tenant_id: uuid.UUID | str) -> bytes:
    key = derive_tenant_key(tenant_id)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return FORMAT_VERSION + nonce + ciphertext


def _decrypt(blob: bytes, tenant_id: uuid.UUID | str) -> bytes:
    if not blob or blob[0:1] != FORMAT_VERSION:
        raise ServiceUnavailableError(
            "Stored file is unreadable.", error_code="storage_format"
        )
    key = derive_tenant_key(tenant_id)
    nonce = blob[1 : 1 + NONCE_BYTES]
    ciphertext = blob[1 + NONCE_BYTES :]
    # GCM authenticates as it decrypts: a tampered object raises rather than
    # returning plausible-looking bytes.
    return AESGCM(key).decrypt(nonce, ciphertext, None)


def store_statement(
    *, tenant_id: uuid.UUID, statement_id: uuid.UUID, data: bytes
) -> StoredObject:
    """Encrypt and upload. Returns the key and a hash of the *plaintext*.

    The hash is of the original bytes, not the ciphertext: it is what detects
    the same statement being uploaded twice, and encryption is randomised so
    identical plaintext yields different ciphertext every time.
    """
    digest = hashlib.sha256(data).hexdigest()
    key = statement_object_key(tenant_id, statement_id)
    blob = _encrypt(data, tenant_id)

    try:
        get_storage().put_object(
            settings.MINIO_BUCKET_STATEMENTS,
            key,
            io.BytesIO(blob),
            length=len(blob),
            content_type="application/octet-stream",
            metadata={
                # Ids only. A filename, a bank name or a period in object
                # metadata would leak from a bucket listing alone.
                "x-amz-meta-tenant": str(tenant_id),
                "x-amz-meta-statement": str(statement_id),
            },
        )
    except S3Error:
        logger.error("storage_write_failed", statement_id=str(statement_id),
                     error_code="s3_error")
        raise ServiceUnavailableError(
            "Could not store the statement. Please try again."
        ) from None

    logger.info(
        "statement_stored",
        tenant_id=str(tenant_id),
        statement_id=str(statement_id),
        count=len(blob),
    )
    return StoredObject(key=key, size_bytes=len(data), sha256=digest)


def load_statement(*, tenant_id: uuid.UUID, storage_key: str) -> bytes:
    """Download and decrypt. Raises NotFoundError if the object is gone."""
    try:
        response = get_storage().get_object(settings.MINIO_BUCKET_STATEMENTS, storage_key)
        try:
            blob = response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchBucket"}:
            raise NotFoundError("That statement file is no longer available.") from None
        logger.error("storage_read_failed", error_code="s3_error")
        raise ServiceUnavailableError("Could not read the statement.") from None

    return _decrypt(blob, tenant_id)


def delete_statement(*, storage_key: str) -> None:
    try:
        get_storage().remove_object(settings.MINIO_BUCKET_STATEMENTS, storage_key)
    except S3Error as exc:
        # Already gone is the desired end state.
        if exc.code not in {"NoSuchKey", "NoSuchBucket"}:
            logger.warning("storage_delete_failed", error_code="s3_error")
