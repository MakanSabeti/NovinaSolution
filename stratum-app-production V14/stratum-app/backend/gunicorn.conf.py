"""Production WSGI server settings (used by render.yaml, Procfile and railway.json).

Kept deliberately small for a hobby-tier instance (512 MB RAM): 2 worker processes
x 4 threads. Each worker holds its own DB pool (DB_POOL_MAX, default 5), so this
opens at most ~10 connections to Supabase's transaction pooler.

No preload_app: storage.init_db() runs at import, and a connection pool opened in
the master process must not be inherited across fork().
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5001')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"
# Photo-mode requests wait on the vision API; give them room before gunicorn kills the worker.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Render/Railway terminate TLS at their load balancer and forward the original scheme.
forwarded_allow_ips = "*"
