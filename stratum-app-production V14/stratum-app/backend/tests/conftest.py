"""
Pytest setup. Needs a real (throwaway) Postgres: set TEST_DATABASE_URL, or it
defaults to the local one CI starts (see .github/workflows/ci.yml). Never point
this at production -- the tests create and delete organizations, sites and members.

Auth is exercised for real: tokens are minted with a test HS256 secret, exactly
the way Supabase signs them with a project's legacy JWT secret.
"""

import os
import sys
import time
import uuid

import pytest

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/stratum_test"
)
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_JWT_SECRET"] = "test-secret-for-ci-only-at-least-32-bytes-long"
os.environ["SUPABASE_ANON_KEY"] = "test-anon-key"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "founder@example.com"
for var in ("AUTH_DISABLED", "SUPABASE_SERVICE_ROLE_KEY", "SENTRY_DSN", "RESEND_API_KEY", "SMTP_HOST",
            "SLACK_WEBHOOK_URL", "RENDER", "RAILWAY_ENVIRONMENT", "APP_ENV"):
    os.environ.pop(var, None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import jwt  # noqa: E402

import app as stratum_app  # noqa: E402  (applies migrations)


def make_token(user_id=None, email=None, *, role="authenticated", exp_offset=3600, secret=None, **extra):
    now = int(time.time())
    claims = {
        "sub": user_id or str(uuid.uuid4()),
        "email": email,
        "role": role,
        "aud": "authenticated",
        "iss": "https://test-project.supabase.co/auth/v1",
        "iat": now,
        "exp": now + exp_offset,
        **extra,
    }
    return jwt.encode(claims, secret or os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")


class User:
    def __init__(self, client, email):
        self.id = str(uuid.uuid4())
        self.email = email
        self.client = client
        self.headers = {"Authorization": f"Bearer {make_token(self.id, email)}"}

    def get(self, url, **kw):
        return self.client.get(url, headers=self.headers, **kw)

    def post(self, url, **kw):
        return self.client.post(url, headers=self.headers, **kw)

    def patch(self, url, **kw):
        return self.client.patch(url, headers=self.headers, **kw)

    def delete(self, url, **kw):
        return self.client.delete(url, headers=self.headers, **kw)


@pytest.fixture
def client():
    return stratum_app.app.test_client()


@pytest.fixture
def new_user(client):
    def factory(email=None):
        return User(client, email or f"user-{uuid.uuid4().hex[:8]}@example.com")
    return factory
