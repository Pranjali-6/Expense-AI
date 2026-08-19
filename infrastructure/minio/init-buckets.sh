#!/bin/sh
# =============================================================================
# Bootstrap object storage.
#
# Buckets are private, versioned, and never anonymously readable. Statement
# objects are additionally encrypted per tenant (SSE-C, key derived from
# STORAGE_MASTER_KEK via HKDF) and reachable only through short-lived
# presigned URLs issued by the API after an authorization check.
# =============================================================================
set -eu

echo "[minio-init] waiting for MinIO..."
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
    sleep 2
done
echo "[minio-init] connected"

create_bucket() {
    bucket="$1"
    if mc ls "local/${bucket}" >/dev/null 2>&1; then
        echo "[minio-init] bucket '${bucket}' already exists"
    else
        mc mb "local/${bucket}"
        echo "[minio-init] created bucket '${bucket}'"
    fi

    # Private by default — no anonymous access, ever.
    mc anonymous set none "local/${bucket}"

    # Versioning gives us a recovery path for an accidental delete.
    mc version enable "local/${bucket}" >/dev/null 2>&1 || true
}

create_bucket "${MINIO_BUCKET_STATEMENTS}"
create_bucket "${MINIO_BUCKET_EXPORTS}"

# Exports are transient artefacts; expire them rather than accumulating
# derived copies of financial data indefinitely.
mc ilm rule add --expire-days 7 "local/${MINIO_BUCKET_EXPORTS}" >/dev/null 2>&1 \
    || echo "[minio-init] export expiry rule already present"

echo "[minio-init] done"
