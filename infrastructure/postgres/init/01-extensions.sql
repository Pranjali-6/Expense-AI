-- =============================================================================
-- Extensions required by the platform.
--   pgcrypto   gen_random_uuid(), digest() for fingerprints
--   citext     case-insensitive email addresses
--   pg_trgm    trigram similarity for merchant fuzzy matching
--   btree_gin  composite indexes mixing scalar + jsonb predicates
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- Note: the `money` type is deliberately never used. Every monetary column is
-- NUMERIC(18, 2) and every application-side value is a Python Decimal.
