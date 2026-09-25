-- 002_auth_orgs.sql
-- Supabase Auth -> organization membership.
--
-- Every authenticated Supabase user (auth.users.id, a UUID taken from the JWT's
-- `sub` claim) belongs to one or more organizations through org_members. The
-- Flask backend resolves the caller's org on every request and scopes every
-- site query by it -- that is the tenant isolation boundary.
--
-- user_id deliberately has no foreign key to auth.users: that schema only
-- exists on Supabase, and keeping it out lets CI run this against plain Postgres.

CREATE TABLE IF NOT EXISTS org_members (
    org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID   NOT NULL,
    email       TEXT,
    role        TEXT   NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    created_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id);

-- Pending invitations: an owner/admin invites an email address; the first time
-- a user with that (verified) email signs in, they join that org instead of
-- getting a fresh personal one.
CREATE TABLE IF NOT EXISTS org_invites (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id      BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email       TEXT   NOT NULL,
    role        TEXT   NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    created_at  DOUBLE PRECISION NOT NULL,
    CONSTRAINT org_invites_org_email_key UNIQUE (org_id, email)
);

CREATE INDEX IF NOT EXISTS idx_org_invites_email ON org_invites(lower(email));

-- Same reasoning as 001: nothing here should be reachable through Supabase's
-- auto-generated REST API. The backend connects as the owner role and bypasses RLS.
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_invites ENABLE ROW LEVEL SECURITY;
