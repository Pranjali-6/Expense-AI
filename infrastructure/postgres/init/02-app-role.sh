#!/bin/bash
# =============================================================================
# Create the least-privilege application role.
#
# WHY THIS EXISTS: PostgreSQL Row Level Security is silently bypassed by the
# table owner and by superusers. If the application connected as the owner,
# every RLS policy in this system would be decorative. So the app gets its own
# non-owner, non-superuser role, and tenant isolation becomes enforceable by
# the database rather than by our own diligence.
#
# Table-level grants are issued by Alembic migrations (P1) as tables appear.
# =============================================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" <<-EOSQL

    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE ${APP_DB_USER} LOGIN PASSWORD '${APP_DB_PASSWORD}';
        ELSE
            ALTER ROLE ${APP_DB_USER} WITH LOGIN PASSWORD '${APP_DB_PASSWORD}';
        END IF;
    END
    \$\$;

    -- Explicitly deny the two attributes that would defeat RLS.
    ALTER ROLE ${APP_DB_USER} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${APP_DB_USER};
    GRANT USAGE ON SCHEMA public TO ${APP_DB_USER};

    -- Future tables created by migrations are granted automatically.
    ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${APP_DB_USER};
    ALTER DEFAULT PRIVILEGES FOR ROLE ${POSTGRES_USER} IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO ${APP_DB_USER};

    -- The tenant scope is carried in a session GUC set per request/task via
    -- SET LOCAL app.current_tenant_id. Declare it so it always resolves.
    ALTER DATABASE ${POSTGRES_DB} SET app.current_tenant_id TO '';

EOSQL

echo "[init] application role '${APP_DB_USER}' ready (NOSUPERUSER, NOBYPASSRLS)"
