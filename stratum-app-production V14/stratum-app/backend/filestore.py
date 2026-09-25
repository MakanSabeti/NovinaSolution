"""
filestore.py

Large-file storage on Supabase Storage (same project as the database and auth).

Why: DEMs, point clouds and drone photos from a real flight are far too big to
ship inside a JSON request body. Instead the browser uploads the file straight
to Supabase Storage with a short-lived signed upload URL that this backend
issues, and then passes the API a small reference -- {"storage_path": "..."} --
in place of the inline data. The backend downloads it server-side.

Tenant isolation: every object lives under `org_<org_id>/`. The backend only
ever issues upload URLs inside the caller's own prefix and refuses to read any
path outside it, so one organization can never reference another's files. The
bucket is private; the browser never gets the service-role key.

Configuration (environment):
  SUPABASE_URL               https://<project-ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  Project Settings -> API Keys -> service_role / secret key.
                             Server-side only -- never put this in the frontend.
  STORAGE_BUCKET             optional, default "stratum-files". Created automatically.

Uses the Storage REST API directly with `requests` (already a dependency)
rather than pulling in the full supabase-py client.
"""

import json
import os
import re
import threading
import uuid

import requests

DEFAULT_BUCKET = "stratum-files"
# Supabase free tier caps a single upload at 50 MB. Raise this (and the project's
# global limit in the dashboard) on a paid plan.
BUCKET_FILE_SIZE_LIMIT = 50 * 1024 * 1024
ALLOWED_KINDS = {"dem", "capture", "blueprint", "photo", "pointcloud", "other"}

_bucket_ready = False
_bucket_lock = threading.Lock()


class FileStoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def bucket() -> str:
    return os.environ.get("STORAGE_BUCKET", DEFAULT_BUCKET)


def _base() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1"


def _headers(extra: dict | None = None) -> dict:
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if extra:
        h.update(extra)
    return h


def _require_enabled():
    if not enabled():
        raise FileStoreError(
            "File storage is not configured: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on the backend.", 503
        )


def org_prefix(org_id) -> str:
    return f"org_{int(org_id)}/"


def check_path(path: str, org_id) -> str:
    """Rejects anything outside the caller's org prefix or containing traversal tricks."""
    if not isinstance(path, str) or not path:
        raise FileStoreError("storage_path must be a non-empty string.")
    if ".." in path.split("/") or path.startswith("/") or "\\" in path:
        raise FileStoreError("Invalid storage_path.")
    if not path.startswith(org_prefix(org_id)):
        # Deliberately indistinguishable from "doesn't exist".
        raise FileStoreError("File not found.", 404)
    return path


def ensure_bucket() -> None:
    """Creates the private bucket on first use. Safe to call repeatedly / concurrently."""
    global _bucket_ready
    if _bucket_ready:
        return
    with _bucket_lock:
        if _bucket_ready:
            return
        r = requests.get(f"{_base()}/bucket/{bucket()}", headers=_headers(), timeout=10)
        if r.status_code != 200:
            r = requests.post(
                f"{_base()}/bucket",
                headers=_headers({"Content-Type": "application/json"}),
                json={"id": bucket(), "name": bucket(), "public": False, "file_size_limit": BUCKET_FILE_SIZE_LIMIT},
                timeout=10,
            )
            # 409 / "already exists" just means another worker won the race.
            if r.status_code not in (200, 201) and "already exists" not in r.text.lower() and r.status_code != 409:
                raise FileStoreError(f"Could not create storage bucket '{bucket()}': {r.status_code} {r.text[:200]}", 502)
        _bucket_ready = True


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "file")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return name[-100:]


def create_signed_upload(org_id, kind: str, filename: str) -> dict:
    """Returns {"storage_path", "upload_url", "token"}. The browser PUTs the raw
    file body to upload_url (valid for 2 hours) and then sends storage_path to the API."""
    _require_enabled()
    if kind not in ALLOWED_KINDS:
        raise FileStoreError(f"'kind' must be one of {sorted(ALLOWED_KINDS)}.")
    ensure_bucket()
    path = f"{org_prefix(org_id)}{kind}/{uuid.uuid4().hex}-{_safe_filename(filename)}"
    r = requests.post(f"{_base()}/object/upload/sign/{bucket()}/{path}", headers=_headers(), timeout=10)
    if r.status_code != 200:
        raise FileStoreError(f"Could not create upload URL: {r.status_code} {r.text[:200]}", 502)
    signed = r.json()["url"]  # "/object/upload/sign/<bucket>/<path>?token=..."
    token = signed.split("token=", 1)[1] if "token=" in signed else None
    return {"storage_path": path, "upload_url": _base() + signed, "token": token}


def upload_bytes(org_id, kind: str, filename: str, data: bytes, content_type: str) -> str:
    """Server-side upload (e.g. keeping a copy of a vision-capture photo). Returns the path."""
    _require_enabled()
    ensure_bucket()
    path = f"{org_prefix(org_id)}{kind}/{uuid.uuid4().hex}-{_safe_filename(filename)}"
    r = requests.post(
        f"{_base()}/object/{bucket()}/{path}",
        headers=_headers({"Content-Type": content_type or "application/octet-stream"}),
        data=data, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise FileStoreError(f"Upload failed: {r.status_code} {r.text[:200]}", 502)
    return path


def download(path: str, org_id, max_bytes: int = BUCKET_FILE_SIZE_LIMIT) -> bytes:
    _require_enabled()
    check_path(path, org_id)
    with requests.get(f"{_base()}/object/authenticated/{bucket()}/{path}", headers=_headers(),
                      timeout=60, stream=True) as r:
        if r.status_code in (400, 404):
            raise FileStoreError("File not found.", 404)
        if r.status_code != 200:
            raise FileStoreError(f"Could not fetch file: {r.status_code}", 502)
        chunks, total = [], 0
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise FileStoreError(f"File is larger than the {max_bytes // (1024 * 1024)} MB limit for this operation.", 413)
            chunks.append(chunk)
        return b"".join(chunks)


def download_json(path: str, org_id):
    try:
        return json.loads(download(path, org_id))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FileStoreError(f"'{path.rsplit('/', 1)[-1]}' is not valid JSON.")


def signed_download_url(path: str, org_id, expires_in: int = 3600) -> str:
    _require_enabled()
    check_path(path, org_id)
    r = requests.post(
        f"{_base()}/object/sign/{bucket()}/{path}",
        headers=_headers({"Content-Type": "application/json"}), json={"expiresIn": int(expires_in)}, timeout=10,
    )
    if r.status_code != 200:
        raise FileStoreError("File not found.", 404)
    return _base() + r.json()["signedURL"]


def is_reference(value) -> bool:
    return isinstance(value, dict) and set(value.keys()) == {"storage_path"}


def resolve(value, org_id):
    """If `value` is {"storage_path": ...}, downloads and parses that JSON file;
    otherwise returns it unchanged (inline data keeps working exactly as before)."""
    if is_reference(value):
        return download_json(value["storage_path"], org_id)
    return value
