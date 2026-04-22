#!/usr/bin/env python3
"""V2 indexer: reads Claude Code JSONL history and upserts to Supabase
(memory_turns, memory_sessions) + uploads changed JSONL files to Storage
for cross-machine resume."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

from supabase_client import (
    BUCKET,
    client,
    machine_id,
    rpc,
    select,
    storage_upload,
    upsert_rows,
    vector_literal,
)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
INDEX_DIR = Path.home() / ".claude" / "memory-index"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
EMBED_MAX_CHARS = 4000


def extract_turn(obj: dict) -> dict | None:
    t = obj.get("type")
    if t == "user":
        msg = obj.get("message", {})
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        role, text = "user", content.strip()
    elif t == "assistant":
        msg = obj.get("message", {})
        blocks = msg.get("content", [])
        if not isinstance(blocks, list):
            return None
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(x for x in texts if x).strip()
        if not text:
            return None
        role = "assistant"
    else:
        return None

    uuid = obj.get("uuid")
    if not uuid:
        return None
    return {
        "turn_uuid": uuid,
        "session_id": obj.get("sessionId", ""),
        "role": role,
        "content": text,
        "timestamp": obj.get("timestamp", ""),
        "cwd": obj.get("cwd"),
        "git_branch": obj.get("gitBranch"),
    }


def hash_path(p: str) -> str:
    return hashlib.sha1(p.encode()).hexdigest()[:16]


def process_file(c, model, path: Path, mid: str, force: bool, log) -> tuple[int, int]:
    stat = path.stat()
    existing = select(
        c,
        "memory_files_indexed",
        path=f"eq.{path}",
        machine_id=f"eq.{mid}",
        select="mtime,size,last_line",
        limit=1,
    )
    start_line = 0
    row = existing[0] if existing else None
    if row and not force:
        if row["mtime"] == stat.st_mtime and row["size"] == stat.st_size:
            return (0, 0)
        if row["size"] <= stat.st_size and row["mtime"] <= stat.st_mtime:
            start_line = row["last_line"]

    project = path.parent.name
    new_turns = []
    last_line = start_line
    session_ids: set[str] = set()
    session_cwd: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            last_line = i + 1
            if i < start_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = extract_turn(obj)
            if turn is None:
                continue
            turn["project"] = project
            turn["machine_id"] = mid
            new_turns.append(turn)
            if turn["session_id"]:
                session_ids.add(turn["session_id"])
                if turn.get("cwd"):
                    session_cwd[turn["session_id"]] = turn["cwd"]

    added = 0
    if new_turns:
        BATCH = 64
        for i in range(0, len(new_turns), BATCH):
            batch = new_turns[i : i + BATCH]
            texts = [t["content"][:EMBED_MAX_CHARS] for t in batch]
            embs = list(model.embed(texts))
            rows = []
            for t, emb in zip(batch, embs):
                rows.append({
                    "turn_uuid": t["turn_uuid"],
                    "session_id": t["session_id"],
                    "project": t["project"],
                    "machine_id": t["machine_id"],
                    "role": t["role"],
                    "content": t["content"],
                    "timestamp": t["timestamp"],
                    "cwd": t["cwd"],
                    "git_branch": t["git_branch"],
                    "embedding": vector_literal(emb.tolist()),
                })
            upsert_rows(c, "memory_turns", rows, on_conflict="turn_uuid")
            added += len(rows)

    # Upload JSONL to Storage for cross-machine resume
    if (new_turns or force or not row) and not getattr(process_file, "_skip_upload", False):
        sid_from_name = path.stem
        storage_path = f"{mid}/{project}/{path.name}"
        try:
            storage_upload(c, storage_path, path.read_bytes())
            rpc(c, "memory_set_jsonl_path", {
                "sid": sid_from_name,
                "path": storage_path,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            })
        except Exception as e:
            log(f"    ! storage upload failed for {path.name}: {e}")

    upsert_rows(
        c,
        "memory_files_indexed",
        [{
            "path": str(path),
            "machine_id": mid,
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "last_line": last_line,
        }],
        on_conflict="path,machine_id",
    )
    return (added, len(session_ids))


def refresh_all_sessions(c, mid: str, log) -> None:
    """Recompute memory_sessions metadata from memory_turns for this machine."""
    r = rpc(c, "memory_refresh_all_sessions", {"mid": mid})
    log(f"[refresh] sessions updated: {r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--project", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skip-upload", action="store_true", help="Skip JSONL upload to Storage")
    args = ap.parse_args()

    def log(msg):
        if not args.quiet:
            print(msg, file=sys.stderr)

    log(f"[indexar-v2] loading embedding model: {EMBED_MODEL}")
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(INDEX_DIR / "models"))

    mid = machine_id()
    log(f"[indexar-v2] machine_id={mid}")

    pattern = f"{args.project}/*.jsonl" if args.project else "*/*.jsonl"
    files = sorted(PROJECTS_DIR.glob(pattern))
    log(f"[indexar-v2] {len(files)} JSONL files")

    process_file._skip_upload = args.skip_upload
    c = client()
    t0 = time.time()
    total_added = 0
    touched_files = 0
    for idx, path in enumerate(files, 1):
        try:
            added, n_sessions = process_file(c, model, path, mid, args.force, log if not args.quiet else (lambda *_: None))
        except Exception as e:
            log(f"[indexar-v2] ERROR {path.name}: {e}")
            continue
        total_added += added
        if added or n_sessions:
            touched_files += 1
        if not args.quiet and (added or idx % 25 == 0):
            log(f"[indexar-v2] {idx}/{len(files)} {path.name[:12]}... +{added} | total +{total_added} ({time.time()-t0:.1f}s)")

    if touched_files:
        log(f"[indexar-v2] refreshing session metadata...")
        refresh_all_sessions(c, mid, log)

    log(f"[indexar-v2] DONE in {time.time()-t0:.1f}s | added={total_added} touched_files={touched_files}")
    c.close()


if __name__ == "__main__":
    main()
