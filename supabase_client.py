"""Thin Supabase REST/Storage client for the memory index."""
from __future__ import annotations
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


def storage_upload(c: httpx.Client, path: str, data: bytes) -> None:
    r = c.post(
        f"/storage/v1/object/{BUCKET}/{path}",
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "x-upsert": "true",
        },
    )
    if r.status_code >= 400:
        raise RuntimeError(f"storage upload failed: {r.status_code} {r.text[:400]}")


def storage_download(c: httpx.Client, path: str) -> bytes:
    r = c.get(f"/storage/v1/object/{BUCKET}/{path}")
    if r.status_code >= 400:
        raise RuntimeError(f"storage download failed: {r.status_code} {r.text[:400]}")
    return r.content


def vector_literal(vec) -> str:
    """Convert numeric vector to pgvector textual literal."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
