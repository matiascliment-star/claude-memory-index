#!/usr/bin/env python3
"""Index Claude Code conversation history (JSONL) into SQLite with FTS5 + embeddings."""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import struct
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECTS_DIR = Path.home() / ".claude" / "projects"
INDEX_DIR = Path.home() / ".claude" / "memory-index"
DB_PATH = INDEX_DIR / "conversations.db"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
EMBED_MAX_CHARS = 4000  # truncate long turns for embedding; FTS stores full text


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY,
            turn_uuid TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL,
            project TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cwd TEXT,
            git_branch TEXT,
            embedding BLOB
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_turns_project ON turns(project);

        CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
            content,
            content=turns,
            content_rowid=id,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
            INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
            INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TABLE IF NOT EXISTS files_indexed (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            last_line INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def extract_turn(obj: dict) -> dict | None:
    """Return {role, content, ...} for a real user/assistant turn, or None to skip."""
    t = obj.get("type")
    if t == "user":
        msg = obj.get("message", {})
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        role = "user"
        text = content.strip()
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


def embed_texts(model, texts: list[str]) -> list[bytes]:
    vectors = list(model.embed([t[:EMBED_MAX_CHARS] for t in texts]))
    return [struct.pack(f"{EMBED_DIM}f", *v.tolist()) for v in vectors]


def process_file(conn: sqlite3.Connection, model, path: Path, force: bool) -> tuple[int, int]:
    """Returns (turns_added, turns_skipped)."""
    stat = path.stat()
    row = conn.execute(
        "SELECT mtime, size, last_line FROM files_indexed WHERE path = ?", (str(path),)
    ).fetchone()

    start_line = 0
    if row and not force:
        if row[0] == stat.st_mtime and row[1] == stat.st_size:
            return (0, 0)  # unchanged
        if row[1] <= stat.st_size and row[0] <= stat.st_mtime:
            start_line = row[2]  # resume after last indexed line

    project = path.parent.name
    new_turns = []
    last_line = start_line
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
            new_turns.append(turn)

    added = 0
    skipped = 0
    if new_turns:
        # Batch embed
        BATCH = 64
        for i in range(0, len(new_turns), BATCH):
            batch = new_turns[i : i + BATCH]
            embs = embed_texts(model, [t["content"] for t in batch])
            for t, emb in zip(batch, embs):
                try:
                    conn.execute(
                        """INSERT INTO turns
                           (turn_uuid, session_id, project, role, content, timestamp, cwd, git_branch, embedding)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            t["turn_uuid"],
                            t["session_id"],
                            t["project"],
                            t["role"],
                            t["content"],
                            t["timestamp"],
                            t["cwd"],
                            t["git_branch"],
                            emb,
                        ),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1  # already indexed
            conn.commit()

    conn.execute(
        """INSERT INTO files_indexed(path, mtime, size, last_line, indexed_at)
           VALUES(?, ?, ?, ?, datetime('now'))
           ON CONFLICT(path) DO UPDATE SET
             mtime=excluded.mtime, size=excluded.size,
             last_line=excluded.last_line, indexed_at=excluded.indexed_at""",
        (str(path), stat.st_mtime, stat.st_size, last_line),
    )
    conn.commit()
    return (added, skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Reindex even if unchanged")
    ap.add_argument("--project", default=None, help="Limit to one project dir name")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if not args.quiet:
        print(f"[indexar] loading embedding model: {EMBED_MODEL}", file=sys.stderr)
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(INDEX_DIR / "models"))

    pattern = f"{args.project}/*.jsonl" if args.project else "*/*.jsonl"
    files = sorted(PROJECTS_DIR.glob(pattern))
    if not args.quiet:
        print(f"[indexar] found {len(files)} JSONL files", file=sys.stderr)

    t0 = time.time()
    total_added = 0
    total_skipped = 0
    for idx, path in enumerate(files, 1):
        try:
            added, skipped = process_file(conn, model, path, force=args.force)
        except Exception as e:
            print(f"[indexar] ERROR on {path.name}: {e}", file=sys.stderr)
            continue
        total_added += added
        total_skipped += skipped
        if not args.quiet and (added or skipped or idx % 20 == 0):
            elapsed = time.time() - t0
            print(
                f"[indexar] {idx}/{len(files)} {path.parent.name}/{path.name[:12]}... "
                f"+{added} =~{skipped} | total +{total_added} ({elapsed:.1f}s)",
                file=sys.stderr,
            )

    total_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    if not args.quiet:
        print(
            f"[indexar] DONE in {time.time()-t0:.1f}s | added={total_added} skipped={total_skipped} total_turns={total_turns}",
            file=sys.stderr,
        )
    conn.close()


if __name__ == "__main__":
    main()
