"""
auth.py

Supabase Auth for the Flask API.

The frontend signs users in with supabase-js and sends the session's access
token as `Authorization: Bearer <jwt>` on every API call. This module verifies
that token locally (no round-trip to Supabase per request) and resolves the
caller's organization, which every storage query is then scoped by.

Two verification modes, picked automatically:
  * SUPABASE_JWT_SECRET set  -> HS256 with the project's legacy JWT secret
                                (Supabase dashboard -> Project Settings -> JWT Keys ->
                                "Legacy JWT Secret").
  * otherwise                 -> asymmetric signing keys (RS256/ES256), fetched and
                                cached from SUPABASE_URL/auth/v1/.well-known/jwks.json.
                                This is the default for new Supabase projects.

AUTH_DISABLED=true skips all of this and treats every request as a member of the
original 'Default Organization'. It exists for local development only, and the
app refuses to start with it when running under a production host (see app.py).
"""

import functools
import os

import jwt
from flask import g, jsonify, request

import sentry_setup
import storage

_jwks_client = None


def auth_disabled() -> bool:
    return os.environ.get("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes")


def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "").rstrip("/")


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        if not _supabase_url():
            raise AuthError("Server misconfigured: set SUPABASE_URL (or SUPABASE_JWT_SECRET).", 500)
        _jwks_client = jwt.PyJWKClient(
            f"{_supabase_url()}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600, timeout=10
        )
    return _jwks_client


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def verify_token(token: str) -> dict:
    """Returns the verified JWT claims, or raises AuthError."""
    issuer = f"{_supabase_url()}/auth/v1" if _supabase_url() else None
    options = {"require": ["exp", "sub"], "verify_iss": issuer is not None}
    try:
        secret = os.environ.get("SUPABASE_JWT_SECRET")
        if secret:
            key, algorithms = secret, ["HS256"]
        else:
            key = _get_jwks_client().get_signing_key_from_jwt(token).key
            algorithms = ["RS256", "ES256"]
        claims = jwt.decode(
            token, key, algorithms=algorithms, audience="authenticated", issuer=issuer,
            options=options, leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("Session expired -- sign in again.")
    except jwt.PyJWKClientError as e:
        raise AuthError(f"Could not verify session token: {e}")
    except jwt.InvalidTokenError as e:
        raise AuthError(f"Invalid session token: {e}")
    if claims.get("role") != "authenticated":
        raise AuthError("Token is not a signed-in user session.")
    return claims


def _authenticate() -> None:
    """Populates g.user_id / g.email / g.org_id / g.role for this request."""
    if auth_disabled():
        g.user_id, g.email, g.role = None, None, "owner"
        g.org_id = storage.default_org_id()
        return

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        raise AuthError("Sign in required.")
    claims = verify_token(header[7:].strip())

    g.user_id = claims["sub"]
    g.email = claims.get("email")
    membership = storage.resolve_membership(g.user_id, g.email, os.environ.get("BOOTSTRAP_ADMIN_EMAIL"))
    g.org_id = membership["org_id"]
    g.role = membership["role"]
    sentry_setup.tag_request(g.user_id, g.org_id)


def require_auth(fn):
    """Route decorator. CORS preflight (OPTIONS) is let through untouched, since
    browsers never attach credentials to it."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == "OPTIONS":
            return "", 204
        try:
            _authenticate()
        except AuthError as e:
            return jsonify({"error": e.message}), e.status
        return fn(*args, **kwargs)
    return wrapper


def require_role(*roles):
    """Use *under* @require_auth."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if g.get("role") not in roles:
                return jsonify({"error": f"This action needs one of these roles: {', '.join(roles)}."}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
