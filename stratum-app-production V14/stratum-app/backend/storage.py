"""
storage.py

Postgres persistence (Supabase) for sites and their capture history. This is
what turns the platform from "analyze one file pair" into "remember every
capture for a site over time" -- the prerequisite for any trend analysis.

No ORM, deliberately -- the schema is small enough that raw SQL is easier to
reason about and test than an abstraction layer would be.

Configuration (environment):
  DATABASE_URL   required. Use the Supabase *pooler* connection string
                 (transaction mode, port 6543). Never commit it.
  DB_POOL_MAX    optional, default 5. Keep this small: the free Supabase tier
                 caps total connections, and every gunicorn worker gets its own pool.

Tenant isolation: every function that reads or writes a site takes the caller's
`org_id` and filters on it, so a site id belonging to another organization
behaves exactly like one that doesn't exist (None / 404). Captures and alerts
are only ever reached through a site the caller's org owns. `org_id=None` means
"unscoped" and is only used by tests and maintenance scripts.

Schema lives in migrations/*.sql and is applied by init_db(), tracked in a
schema_migrations table. To change the schema, add a new numbered .sql file --
never edit one that has already been applied.
"""

import math
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_LOCK_ID = 7_355_608  # arbitrary constant; serializes migrations across workers

def _finite(obj):
    """JSONB rejects NaN/Infinity (they aren't valid JSON), whereas the old TEXT column
    happily stored them. Analysis code can legitimately produce them (e.g. a forecast
    with zero velocity), so map them to null instead of failing the whole request."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _jsonb(obj) -> Jsonb:
    return Jsonb(_finite(obj))


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    """Lazily creates the connection pool on first use (so load_dotenv() in
    app.py has already run by the time DATABASE_URL is read)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = os.environ.get("DATABASE_URL")
                if not url:
                    raise RuntimeError(
                        "DATABASE_URL is not set. Copy backend/.env.example to backend/.env and "
                        "paste your Supabase pooler connection string."
                    )
                pool = ConnectionPool(
                    conninfo=url,
                    min_size=1,
                    max_size=int(os.environ.get("DB_POOL_MAX", "5")),
                    kwargs={
                        "row_factory": dict_row,
                        # Supabase's transaction-mode pooler can't handle server-side
                        # prepared statements; this turns them off.
                        "prepare_threshold": None,
                    },
                    # Test each connection before handing it out, so a connection the
                    # pooler silently dropped doesn't surface as a random 500.
                    check=ConnectionPool.check_connection,
                    open=False,
                )
                pool.open(wait=True, timeout=20)
                _pool = pool
    return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def get_conn():
    """One transaction: commits on clean exit, rolls back if an exception escapes."""
    with _get_pool().connection() as conn:
        yield conn


def init_db() -> None:
    """Applies any not-yet-applied migrations/*.sql files, in filename order.
    Safe to call from every worker at startup: an advisory lock makes them queue up
    instead of racing, and everything runs in one transaction (Postgres DDL is
    transactional, so a failed migration leaves nothing half-applied)."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with get_conn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at DOUBLE PRECISION NOT NULL)"
        )
        applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for f in files:
            if f.name in applied:
                continue
            conn.execute(f.read_text())  # no params -> multiple statements allowed
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (%s, %s)",
                (f.name, time.time()),
            )


DEFAULT_REFERENCE_ZONES = {"boundary": None, "buffers": [], "restricted_zones": [], "pond_configs": {}}
DEFAULT_ALERT_RULES = {"pace_threshold_pct": 10, "notify_slack": False, "notify_email": False}


def _site_from_row(row: dict) -> dict:
    site = dict(row)
    # An empty object in the DB falls back to defaults, same as the SQLite version.
    site["reference_zones"] = site["reference_zones"] or dict(DEFAULT_REFERENCE_ZONES)
    site["alert_rules"] = site["alert_rules"] or dict(DEFAULT_ALERT_RULES)
    return site


def _fetch_site(conn, site_id: int, org_id: int | None, for_update: bool = False) -> dict | None:
    """Loads a site on an existing connection, restricted to `org_id` (None = any org).
    for_update=True row-locks it for the rest of the transaction -- used by the
    read-modify-write updaters below so two concurrent edits to the same site can't
    silently overwrite each other."""
    sql = "SELECT * FROM sites WHERE id = %s AND (%s::bigint IS NULL OR org_id = %s::bigint)"
    if for_update:
        sql += " FOR UPDATE"
    row = conn.execute(sql, (site_id, org_id, org_id)).fetchone()
    return _site_from_row(row) if row else None


def create_site(name: str, blueprint: dict, start_date: str | None = None, org_id: int | None = None) -> int:
    """org_id is the caller's organization. None falls back to the oldest organization
    (the 'Default Organization' from 001_init.sql) -- only used when auth is disabled."""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO sites (org_id, name, blueprint, reference_zones, alert_rules, start_date, created_at) "
            "VALUES (COALESCE(%s::bigint, (SELECT id FROM organizations ORDER BY id LIMIT 1)), "
            "        %s, %s, %s, %s, %s, %s) RETURNING id",
            (org_id, name, _jsonb(blueprint), _jsonb(DEFAULT_REFERENCE_ZONES), _jsonb(DEFAULT_ALERT_RULES),
             start_date, time.time()),
        ).fetchone()
        return row["id"]


def update_start_date(site_id: int, start_date: str, org_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE sites SET start_date = %s WHERE id = %s AND (%s::bigint IS NULL OR org_id = %s::bigint) "
            "RETURNING *",
            (start_date, site_id, org_id, org_id),
        ).fetchone()
        return _site_from_row(row) if row else None


def list_sites(org_id: int | None = None) -> list[dict]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT s.id, s.name, s.created_at, COUNT(c.id) AS capture_count
            FROM sites s
            LEFT JOIN captures c ON c.site_id = s.id
            WHERE %s::bigint IS NULL OR s.org_id = %s::bigint
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """, (org_id, org_id)).fetchall()


def get_site(site_id: int, org_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        return _fetch_site(conn, site_id, org_id)


def add_capture(site_id: int, day: int, method: str, result: dict) -> int:
    """
    Upserts by (site_id, day): re-adding a capture for a day that already
    has one REPLACES it, rather than creating a duplicate. Duplicate
    same-day rows break velocity/forecast math (zero time delta between
    points) and make the chart show overlapping points that look like one --
    so this is a correctness fix, not just tidiness. Enforced by a UNIQUE
    constraint, so it's atomic even with concurrent writers.
    """
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO captures (site_id, day, method, result, created_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (site_id, day) DO UPDATE "
            "SET method = EXCLUDED.method, result = EXCLUDED.result, created_at = EXCLUDED.created_at "
            "RETURNING id",
            (site_id, day, method, _jsonb(result), time.time()),
        ).fetchone()
        return row["id"]


def update_target_days(site_id: int, targets: dict, org_id: int | None = None) -> dict | None:
    """
    Updates properties.target_day on matching blueprint features (by id).
    `targets` is {feature_id: target_day_or_None}. Returns the updated site,
    or None if the site doesn't exist.
    """
    with get_conn() as conn:
        site = _fetch_site(conn, site_id, org_id, for_update=True)
        if not site:
            return None

        blueprint = site["blueprint"]
        for feature in blueprint.get("features", []):
            fid = feature["properties"]["id"]
            if fid in targets:
                value = targets[fid]
                if value is None:
                    feature["properties"].pop("target_day", None)
                else:
                    feature["properties"]["target_day"] = value

        conn.execute("UPDATE sites SET blueprint = %s WHERE id = %s", (_jsonb(blueprint), site_id))
        return site


def update_schedules(site_id: int, schedules: dict, org_id: int | None = None) -> dict | None:
    """
    Updates properties.schedule on matching blueprint features (by id).
    `schedules` is {feature_id: [{"pct":10,"day":5}, ...] or None}.
    Returns the updated site, or None if the site doesn't exist.
    """
    with get_conn() as conn:
        site = _fetch_site(conn, site_id, org_id, for_update=True)
        if not site:
            return None

        blueprint = site["blueprint"]
        for feature in blueprint.get("features", []):
            fid = feature["properties"]["id"]
            if fid in schedules:
                value = schedules[fid]
                if not value:
                    feature["properties"].pop("schedule", None)
                else:
                    feature["properties"]["schedule"] = value

        conn.execute("UPDATE sites SET blueprint = %s WHERE id = %s", (_jsonb(blueprint), site_id))
        return site


def update_reference_zones(site_id: int, zones: dict, org_id: int | None = None) -> dict | None:
    """Replaces the site's reference zones (boundary/buffers/restricted_zones/pond_configs).
    Caller sends the full object; missing keys fall back to current values so a partial
    update (e.g. just the boundary) doesn't wipe the others."""
    with get_conn() as conn:
        site = _fetch_site(conn, site_id, org_id, for_update=True)
        if not site:
            return None
        site["reference_zones"] = {**site["reference_zones"], **zones}
        conn.execute(
            "UPDATE sites SET reference_zones = %s WHERE id = %s", (_jsonb(site["reference_zones"]), site_id)
        )
        return site


def update_alert_rules(site_id: int, rules: dict, org_id: int | None = None) -> dict | None:
    with get_conn() as conn:
        site = _fetch_site(conn, site_id, org_id, for_update=True)
        if not site:
            return None
        site["alert_rules"] = {**site["alert_rules"], **rules}
        conn.execute(
            "UPDATE sites SET alert_rules = %s WHERE id = %s", (_jsonb(site["alert_rules"]), site_id)
        )
        return site


def add_alerts(site_id: int, alerts: list[dict]) -> list[int]:
    """Bulk-inserts alerts (each: rule_type, severity, feature_id, message,
    slack_sent, email_sent). Returns the inserted row ids."""
    ids = []
    with get_conn() as conn:
        for a in alerts:
            row = conn.execute(
                "INSERT INTO alerts (site_id, rule_type, severity, feature_id, message, created_at, slack_sent, email_sent) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (site_id, a["rule_type"], a["severity"], a.get("feature_id"), a["message"], time.time(),
                 int(a.get("slack_sent", False)), int(a.get("email_sent", False))),
            ).fetchone()
            ids.append(row["id"])
    return ids


def get_alerts(site_id: int, limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM alerts WHERE site_id = %s ORDER BY created_at DESC LIMIT %s",
            (site_id, limit),
        ).fetchall()


def get_captures(site_id: int) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, site_id, day, method, result, created_at FROM captures "
            "WHERE site_id = %s ORDER BY day ASC",
            (site_id,),
        ).fetchall()


def delete_site(site_id: int, org_id: int | None = None) -> None:
    """Captures and alerts are removed by ON DELETE CASCADE. (The SQLite version only
    deleted captures and left orphaned alerts behind -- Postgres would have rejected
    that, and it was a latent bug either way.)"""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sites WHERE id = %s AND (%s::bigint IS NULL OR org_id = %s::bigint)",
            (site_id, org_id, org_id),
        )


# ============================================================ Organizations / membership =====

def _first_org_id(conn) -> int | None:
    row = conn.execute("SELECT id FROM organizations ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def resolve_membership(user_id: str, email: str | None, bootstrap_email: str | None = None) -> dict:
    """Returns {"org_id", "role"} for an authenticated Supabase user, creating the
    membership on first sign-in. Order of precedence:
      1. an existing membership (oldest one, if the user belongs to several)
      2. a pending invite for the user's email -> join that org
      3. email matches BOOTSTRAP_ADMIN_EMAIL -> become owner of the original
         'Default Organization', so data created before auth existed isn't orphaned
      4. otherwise a brand-new personal organization, owned by this user
    Serialized per user with an advisory lock so two parallel first requests can't
    create two personal orgs."""
    email_norm = (email or "").strip().lower() or None
    with get_conn() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"stratum-user:{user_id}",))
        row = conn.execute(
            "SELECT org_id, role FROM org_members WHERE user_id = %s ORDER BY created_at, org_id LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            if email_norm:
                conn.execute("UPDATE org_members SET email = %s WHERE user_id = %s AND email IS DISTINCT FROM %s",
                             (email_norm, user_id, email_norm))
            return {"org_id": row["org_id"], "role": row["role"]}

        org_id, role = None, "owner"
        if email_norm:
            invite = conn.execute(
                "DELETE FROM org_invites WHERE id = (SELECT id FROM org_invites WHERE lower(email) = %s "
                "ORDER BY created_at LIMIT 1) RETURNING org_id, role",
                (email_norm,),
            ).fetchone()
            if invite:
                org_id, role = invite["org_id"], invite["role"]
        if org_id is None and email_norm and bootstrap_email and email_norm == bootstrap_email.strip().lower():
            org_id = _first_org_id(conn)
        if org_id is None:
            name = f"{email_norm.split('@')[0]}'s organization" if email_norm else "My organization"
            org_id = conn.execute(
                "INSERT INTO organizations (name, created_at) VALUES (%s, %s) RETURNING id", (name, time.time())
            ).fetchone()["id"]

        conn.execute(
            "INSERT INTO org_members (org_id, user_id, email, role, created_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO NOTHING",
            (org_id, user_id, email_norm, role, time.time()),
        )
        return {"org_id": org_id, "role": role}


def get_org(org_id: int) -> dict | None:
    with get_conn() as conn:
        return conn.execute("SELECT id, name, created_at FROM organizations WHERE id = %s", (org_id,)).fetchone()


def list_org_members(org_id: int) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT user_id::text AS user_id, email, role, created_at FROM org_members "
            "WHERE org_id = %s ORDER BY created_at",
            (org_id,),
        ).fetchall()


def list_org_invites(org_id: int) -> list[dict]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, email, role, created_at FROM org_invites WHERE org_id = %s ORDER BY created_at",
            (org_id,),
        ).fetchall()


def add_org_invite(org_id: int, email: str, role: str = "member") -> dict:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO org_invites (org_id, email, role, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, email) DO UPDATE SET role = EXCLUDED.role "
            "RETURNING id, email, role, created_at",
            (org_id, email.strip().lower(), role, time.time()),
        ).fetchone()


def delete_org_invite(org_id: int, invite_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM org_invites WHERE id = %s AND org_id = %s RETURNING id", (invite_id, org_id)
        ).fetchone()
        return row is not None


def org_member_emails(org_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT email FROM org_members WHERE org_id = %s AND email IS NOT NULL ORDER BY created_at", (org_id,)
        ).fetchall()
        return [r["email"] for r in rows]


def default_org_id() -> int | None:
    """The original 'Default Organization' (only used when AUTH_DISABLED is set)."""
    with get_conn() as conn:
        return _first_org_id(conn)
