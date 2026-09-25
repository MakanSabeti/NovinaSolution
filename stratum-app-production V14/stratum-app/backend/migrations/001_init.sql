-- 001_init.sql
-- Initial Postgres schema for Stratum. Replaces the SQLite schema in the old storage.py.
--
-- Conventions:
--   * created_at stays DOUBLE PRECISION unix-epoch seconds (not timestamptz) so the
--     JSON the API returns is identical to what the SQLite version returned --
--     reporting.py compares it numerically. Revisit if/when the API is versioned.
--   * JSON payloads are JSONB, not TEXT: no more json.dumps/loads round-trips, and
--     they can be queried/indexed later.
--   * Tenant model: every site belongs to an organization. Captures and alerts reach
--     their org through site_id (no denormalized org_id to drift out of sync).

CREATE TABLE IF NOT EXISTS organizations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);

-- Until real auth lands (next step), every site belongs to this one org.
INSERT INTO organizations (name, created_at)
SELECT 'Default Organization', EXTRACT(EPOCH FROM now())
WHERE NOT EXISTS (SELECT 1 FROM organizations);

CREATE TABLE IF NOT EXISTS sites (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    name                TEXT NOT NULL,
    blueprint           JSONB NOT NULL,
    reference_zones     JSONB NOT NULL DEFAULT '{}'::jsonb,
    alert_rules         JSONB NOT NULL DEFAULT '{}'::jsonb,
    start_date          TEXT,
    created_at          DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    day         INTEGER NOT NULL,
    method      TEXT NOT NULL,
    result      JSONB NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    -- One capture per (site, day). The old code enforced this with delete-then-insert,
    -- which is racy under concurrent writers; the constraint makes it atomic.
    CONSTRAINT captures_site_day_key UNIQUE (site_id, day)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    rule_type   TEXT NOT NULL,
    severity    TEXT NOT NULL,
    feature_id  TEXT,
    message     TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    slack_sent  INTEGER NOT NULL DEFAULT 0,
    email_sent  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sites_org ON sites(org_id);
CREATE INDEX IF NOT EXISTS idx_alerts_site ON alerts(site_id, created_at);
-- (captures already has an index via the UNIQUE (site_id, day) constraint)

-- SECURITY: Supabase automatically exposes tables in the public schema through its
-- REST API to anyone holding the project's (public) anon key. Enabling RLS with no
-- policies means that API can read/write NOTHING here. Stratum's Flask backend
-- connects directly as the database owner role, which bypasses RLS, so it is
-- unaffected. Do not add permissive policies to these tables.
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sites         ENABLE ROW LEVEL SECURITY;
ALTER TABLE captures      ENABLE ROW LEVEL SECURITY;
ALTER TABLE alerts        ENABLE ROW LEVEL SECURITY;
