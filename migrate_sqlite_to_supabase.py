#!/usr/bin/env python3
"""One-shot: migrate the local SQLite index (conversations.db) into Supabase.

Reads rows from the V1 database and upserts into memory_turns + memory_sessions.
Also uploads every JSONL file found in ~/.claude/projects to the Storage bucket
so that cross-machine resume works.

Idempotent: safe to re-run. Uses turn_uuid as the upsert key.
"""
from __future__ import annotations
import argparse
import sqlite3
import struct
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from supabase_client import (
    client,
    machine_id,
    rpc,
    storage_upload,
    upsert_rows,
    vector_literal,
)

LOCAL_DB = Path.home() / ".claude" / "memory-index" / "conversations.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
EMBED_DIM = 384
BATCH = 100


def decode_embedding(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{EMBED_DIM}f", blob))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-turns", action="store_true", help="Skip turn migration (only JSONL upload)")
    ap.add_argument("--skip-uploads", action="store_true", help="Skip JSONL Storage uploads")
    ap.add_argument("--limit", type=int, default=None, help="Limit turns migrated (for testing)")
    args = ap.parse_args()

    if not LOCAL_DB.exists():
        print(f"ERROR: {LOCAL_DB} not found. Run V1 indexar.py first.", file=sys.stderr)
        sys.exit(1)

    mid = machine_id()
    print(f"[migrate] machine_id={mid}")
    conn = sqlite3.connect(LOCAL_DB)
    c = client()

    # 1) Turns
    if not args.skip_turns:
        total = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        if args.limit:
            total = min(total, args.limit)
        print(f"[migrate] turns to send: {total}")

        t0 = time.time()
        sent = 0
        query = "SELECT turn_uuid, session_id, project, role, content, timestamp, cwd, git_branch, embedding FROM turns"
        if args.limit:
            query += f" LIMIT {int(args.limit)}"

        batch: list[dict] = []
        for row in conn.execute(query):
            uuid, sid, project, role, content, ts, cwd, gb, emb_blob = row
            if emb_blob is None:
                continue
            emb = decode_embedding(emb_blob)
            batch.append({
                "turn_uuid": uuid,
                "session_id": sid,
                "project": project,
                "machine_id": mid,
                "role": role,
                "content": content,
                "timestamp": ts,
                "cwd": cwd,
                "git_branch": gb,
                "embedding": vector_literal(emb),
            })
            if len(batch) >= BATCH:
                upsert_rows(c, "memory_turns", batch, on_conflict="turn_uuid")
                sent += len(batch)
                batch.clear()
                rate = sent / max(1, time.time() - t0)
                print(f"[migrate] {sent}/{total} ({rate:.0f}/s)", file=sys.stderr)
        if batch:
            upsert_rows(c, "memory_turns", batch, on_conflict="turn_uuid")
            sent += len(batch)
        print(f"[migrate] turns done: {sent} in {time.time()-t0:.1f}s")

    # 2) Refresh session metadata from turns
    print("[migrate] refreshing session metadata...")
    rows = rpc(c, "memory_refresh_all_sessions", {"mid": mid})
    print(f"[migrate] sessions refreshed: {rows}")

    # 3) Upload JSONLs
    if not args.skip_uploads:
        jsonls = sorted(PROJECTS_DIR.glob("*/*.jsonl"))
        print(f"[migrate] uploading {len(jsonls)} JSONL files to Storage...")
        t0 = time.time()
        up = 0
        for i, p in enumerate(jsonls, 1):
            project = p.parent.name
            storage_path = f"{mid}/{project}/{p.name}"
            sid = p.stem
            try:
                final_path = storage_upload(c, storage_path, p.read_bytes())
                rpc(c, "memory_set_jsonl_path", {
                    "sid": sid,
                    "path": final_path,
                    "mtime": p.stat().st_mtime,
                    "size": p.stat().st_size,
                })
                up += 1
            except Exception as e:
                print(f"[migrate] upload failed {p.name}: {e}", file=sys.stderr)
            if i % 20 == 0:
                print(f"[migrate] uploaded {i}/{len(jsonls)} ({time.time()-t0:.1f}s)", file=sys.stderr)
        print(f"[migrate] uploads done: {up}/{len(jsonls)} in {time.time()-t0:.1f}s")

    # 4) Mirror files_indexed so incremental indexar_supabase.py knows about them
    print("[migrate] seeding memory_files_indexed...")
    rows_fi = []
    for p in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        st = p.stat()
        rows_fi.append({
            "path": str(p),
            "machine_id": mid,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "last_line": 0,
        })
    for i in range(0, len(rows_fi), 100):
        upsert_rows(c, "memory_files_indexed", rows_fi[i:i+100], on_conflict="path,machine_id")
    print(f"[migrate] files_indexed rows: {len(rows_fi)}")

    c.close()
    conn.close()
    print("[migrate] DONE")


if __name__ == "__main__":
    main()
