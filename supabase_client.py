"""Thin Supabase REST/Storage client for the memory index."""
from __future__ import annotations
import gzip
import os
import socket
from pathlib import Path
from typing import Any

import httpx


def _load_env() -> dict[str, str]:
    env_path = Path.home() / ".env"
    out: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k in ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
            out[k] = v
    return out


_ENV = _load_env()
SUPABASE_URL = _ENV.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = _ENV.get("SUPABASE_SERVICE_ROLE_KEY") or _ENV.get("SUPABASE_KEY", "")
BUCKET = "claude-memory-jsonl"


def machine_id() -> str:
    return socket.gethostname()


def client() -> httpx.Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "Missing SUPABASE_URL or SUPABASE_KEY/SUPABASE_SERVICE_ROLE_KEY in ~/.env"
        )
    return httpx.Client(
        base_url=SUPABASE_URL,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
        timeout=60.0,
    )


def upsert_rows(c: httpx.Client, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
    if not rows:
        return
    r = c.post(
        f"/rest/v1/{table}",
        params={"on_conflict": on_conflict},
        headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        json=rows,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{table} upsert failed: {r.status_code} {r.text[:400]}")


def rpc(c: httpx.Client, fn: str, args: dict[str, Any]) -> Any:
    r = c.post(f"/rest/v1/rpc/{fn}", json=args)
    if r.status_code >= 400:
        raise RuntimeError(f"RPC {fn} failed: {r.status_code} {r.text[:400]}")
    if r.status_code == 204 or not r.content:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def select(c: httpx.Client, table: str, **params: Any) -> list[dict[str, Any]]:
    r = c.get(f"/rest/v1/{table}", params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"select {table} failed: {r.status_code} {r.text[:400]}")
    return r.json()


STANDARD_UPLOAD_MAX = 50 * 1024 * 1024  # 50 MB; above this use resumable


def storage_upload(c: httpx.Client, path: str, data: bytes, *, compress: bool = True) -> str:
    """Upload to Storage. Gzips by default (JSONL compresses 70-90%) and appends
    .gz to the path. Auto-switches to resumable (TUS) for payloads >50 MB.

    Returns the final storage path (with .gz suffix if compressed)."""
    if compress and not path.endswith(".gz"):
        data = gzip.compress(data, compresslevel=6)
        path = path + ".gz"
    if len(data) > STANDARD_UPLOAD_MAX:
        _upload_resumable(c, path, data)
        return path
    r = c.post(
        f"/storage/v1/object/{BUCKET}/{path}",
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "x-upsert": "true",
        },
    )
    if r.status_code == 413:
        _upload_resumable(c, path, data)
        return path
    if r.status_code >= 400:
        raise RuntimeError(f"storage upload failed: {r.status_code} {r.text[:400]}")
    return path


def _upload_resumable(c: httpx.Client, path: str, data: bytes) -> None:
    """Upload via Supabase's TUS-compatible resumable endpoint. For files >50 MB."""
    import base64
    meta_pairs = {
        "bucketName": BUCKET,
        "objectName": path,
        "contentType": "application/octet-stream",
        "cacheControl": "3600",
    }
    meta = ",".join(
        f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in meta_pairs.items()
    )
    r = c.post(
        "/storage/v1/upload/resumable",
        headers={
            "Tus-Resumable": "1.0.0",
            "Upload-Length": str(len(data)),
            "Upload-Metadata": meta,
            "x-upsert": "true",
            "Content-Length": "0",
            "Content-Type": "application/offset+octet-stream",
        },
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"resumable init failed: {r.status_code} {r.text[:400]}")
    upload_url = r.headers.get("location")
    if not upload_url:
        raise RuntimeError(f"resumable init missing Location header: {dict(r.headers)}")
    # The Location may be a full URL or relative — normalize to a usable target.
    if upload_url.startswith("/"):
        upload_url = f"{SUPABASE_URL}{upload_url}"

    CHUNK = 6 * 1024 * 1024  # 6 MB per PATCH
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + CHUNK]
        r = c.patch(
            upload_url,
            content=chunk,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            },
        )
        if r.status_code not in (200, 204):
            raise RuntimeError(f"resumable patch failed at offset {offset}: {r.status_code} {r.text[:200]}")
        offset = int(r.headers.get("upload-offset", offset + len(chunk)))
    if offset != len(data):
        raise RuntimeError(f"resumable upload ended at offset {offset} != {len(data)}")


def storage_download(c: httpx.Client, path: str) -> bytes:
    """Download from Storage. Auto-decompresses .gz paths."""
    r = c.get(f"/storage/v1/object/{BUCKET}/{path}")
    if r.status_code >= 400:
        raise RuntimeError(f"storage download failed: {r.status_code} {r.text[:400]}")
    if path.endswith(".gz"):
        return gzip.decompress(r.content)
    return r.content


def vector_literal(vec) -> str:
    """Convert numeric vector to pgvector textual literal."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
