"""
sentry_setup.py

Error tracking via Sentry (free tier: 5k errors/month). A no-op unless SENTRY_DSN
is set, so local development and CI never report anything.

  SENTRY_DSN                 from sentry.io -> Project Settings -> Client Keys (DSN)
  SENTRY_ENVIRONMENT         optional, e.g. "production" / "staging" (default: production)
  SENTRY_TRACES_SAMPLE_RATE  optional, 0.0-1.0 performance sampling (default 0 = errors only,
                             which keeps you well inside the free quota)

Request bodies (site blueprints, customer data) and user PII are not sent:
send_default_pii stays off. The Supabase user id and org id are attached as
tags by auth.py so an error can be traced to the tenant that hit it.
"""

import os


def init() -> bool:
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
        max_request_body_size="never",
    )
    return True


def tag_request(user_id, org_id) -> None:
    if not os.environ.get("SENTRY_DSN"):
        return
    import sentry_sdk
    if user_id:
        sentry_sdk.set_user({"id": str(user_id)})
    if org_id is not None:
        sentry_sdk.set_tag("org_id", str(org_id))
