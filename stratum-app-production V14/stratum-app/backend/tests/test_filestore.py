"""Supabase Storage client, with the HTTP layer faked (CI has no Supabase project)."""

import pytest

import filestore


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.text, self._content = status, body or {}, str(body), content

    def json(self):
        return self._body

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://proj.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(filestore, "_bucket_ready", True)


def test_signed_upload_is_scoped_to_org(configured, monkeypatch):
    calls = []

    def fake_post(url, headers=None, timeout=None, **kw):
        calls.append((url, headers))
        path = url.split("/object/upload/sign/", 1)[1]
        return Resp(200, {"url": f"/object/upload/sign/{path}?token=tok123"})

    monkeypatch.setattr(filestore.requests, "post", fake_post)
    out = filestore.create_signed_upload(42, "dem", "../../etc/My Survey (v2).json")
    assert out["storage_path"].startswith("org_42/dem/")
    assert out["storage_path"].endswith("-My_Survey_v2_.json")
    assert ".." not in out["storage_path"]
    assert out["upload_url"] == f"https://proj.supabase.co/storage/v1/object/upload/sign/stratum-files/{out['storage_path']}?token=tok123"
    assert out["token"] == "tok123"
    assert calls[0][1]["Authorization"] == "Bearer service-key"


def test_rejects_unknown_kind(configured):
    with pytest.raises(filestore.FileStoreError):
        filestore.create_signed_upload(1, "executable", "x.sh")


def test_download_json_and_size_cap(configured, monkeypatch):
    monkeypatch.setattr(filestore.requests, "get", lambda url, **kw: Resp(200, content=b'{"values": [[1]]}'))
    assert filestore.resolve({"storage_path": "org_3/dem/a.json"}, 3) == {"values": [[1]]}
    assert filestore.resolve({"values": [[2]]}, 3) == {"values": [[2]]}  # inline passes through

    monkeypatch.setattr(filestore.requests, "get", lambda url, **kw: Resp(200, content=b"x" * 2048))
    with pytest.raises(filestore.FileStoreError) as e:
        filestore.download("org_3/photo/big.jpg", 3, max_bytes=1024)
    assert e.value.status == 413


def test_missing_file_is_404(configured, monkeypatch):
    monkeypatch.setattr(filestore.requests, "get", lambda url, **kw: Resp(400, {"error": "not_found"}))
    with pytest.raises(filestore.FileStoreError) as e:
        filestore.download("org_3/dem/nope.json", 3)
    assert e.value.status == 404
