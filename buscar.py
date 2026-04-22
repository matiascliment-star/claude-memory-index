#!/usr/bin/env python3
"""Hybrid search over Claude Code conversation history: FTS5 BM25 + semantic embeddings."""
from __future__ import annotations
import argparse
import json
import re
import sqlite3
import struct
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np

INDEX_DIR = Path.home() / ".claude" / "memory-index"
DB_PATH = INDEX_DIR / "conversations.db"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
SNIPPET_RADIUS = 160  # chars around match


def escape_fts(q: str) -> str:
    # Extract word-like tokens, drop tiny ones, OR them together for recall.
    # FTS5 defaults to AND which kills recall on multi-word queries; BM25
    # will still rank multi-match turns higher.
    tokens = [t for t in re.findall(r"\w+", q, flags=re.UNICODE) if len(t) >= 2]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def fts_search(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, float]]:
    fts_q = escape_fts(query)
    if not fts_q:
        return []
    rows = conn.execute(
        """SELECT rowid, bm25(turns_fts) AS score
           FROM turns_fts WHERE turns_fts MATCH ?
           ORDER BY score LIMIT ?""",
        (fts_q, limit),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def semantic_search(conn: sqlite3.Connection, query: str, limit: int) -> list[tuple[int, float]]:
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(INDEX_DIR / "models"))
    qv = list(model.embed([query]))[0].astype(np.float32)
    qv /= np.linalg.norm(qv) + 1e-9

    ids = []
    vecs = []
    for rowid, blob in conn.execute("SELECT id, embedding FROM turns WHERE embedding IS NOT NULL"):
        ids.append(rowid)
        vecs.append(struct.unpack(f"{EMBED_DIM}f", blob))
    if not ids:
        return []
    M = np.asarray(vecs, dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-9
    sims = M @ qv
    top = np.argsort(-sims)[:limit]
    return [(ids[i], float(sims[i])) for i in top]


def rrf_merge(lists: list[list[tuple[int, float]]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion: combines result lists without needing comparable scores."""
    scores: dict[int, float] = {}
    for ranked in lists:
        for rank, (rid, _s) in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def make_snippet(content: str, query: str) -> str:
    """Find first query-word match in content and return surrounding window."""
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    content_clean = content.replace("\n", " ")
    idx = -1
    for tok in tokens:
        m = re.search(re.escape(tok), content_clean, flags=re.IGNORECASE)
        if m:
            idx = m.start()
            break
    if idx < 0:
        return content_clean[:SNIPPET_RADIUS * 2].strip() + ("..." if len(content_clean) > SNIPPET_RADIUS * 2 else "")
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(content_clean), idx + SNIPPET_RADIUS)
    s = content_clean[start:end].strip()
    if start > 0:
        s = "..." + s
    if end < len(content_clean):
        s = s + "..."
    return s


def fetch_turn(conn: sqlite3.Connection, rowid: int) -> dict:
    row = conn.execute(
        """SELECT id, session_id, project, role, content, timestamp, cwd, git_branch
           FROM turns WHERE id = ?""",
        (rowid,),
    ).fetchone()
    if not row:
        return {}
    return {
        "id": row[0],
        "session_id": row[1],
        "project": row[2],
        "role": row[3],
        "content": row[4],
        "timestamp": row[5],
        "cwd": row[6],
        "git_branch": row[7],
    }


def session_context(conn: sqlite3.Connection, session_id: str) -> dict:
    """Metadata about a session: first user prompt, first timestamp, last timestamp, turn count."""
    row = conn.execute(
        """SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
           FROM turns WHERE session_id = ?""",
        (session_id,),
    ).fetchone()
    first_prompt = conn.execute(
        """SELECT content FROM turns
           WHERE session_id = ? AND role = 'user'
           ORDER BY timestamp ASC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return {
        "first_ts": row[0] if row else None,
        "last_ts": row[1] if row else None,
        "turn_count": row[2] if row else 0,
        "first_prompt": (first_prompt[0][:200] if first_prompt else ""),
    }


def main():
    ap = argparse.ArgumentParser(description="Search your Claude Code conversation history.")
    ap.add_argument("query", nargs="+", help="Query text")
    ap.add_argument("-n", "--limit", type=int, default=5, help="Top N sessions to return")
    ap.add_argument("--mode", choices=["hybrid", "fts", "semantic"], default="hybrid")
    ap.add_argument("--json", action="store_true", help="Output JSON instead of pretty text")
    ap.add_argument("--per-session", type=int, default=1, help="Max turns per session in output")
    ap.add_argument("--open", type=int, metavar="N", help="Open result N in a new Terminal window (macOS)")
    args = ap.parse_args()

    query = " ".join(args.query)
    if not DB_PATH.exists():
        print("ERROR: index not built yet. Run indexar.py first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    per_mode_limit = 50
    lists = []
    if args.mode in ("hybrid", "fts"):
        lists.append(fts_search(conn, query, per_mode_limit))
    if args.mode in ("hybrid", "semantic"):
        lists.append(semantic_search(conn, query, per_mode_limit))

    merged = rrf_merge(lists) if len(lists) > 1 else (lists[0] if lists else [])
    if not merged:
        print("(no results)")
        return

    # Group by session, keep best-ranked turns per session
    seen_per_session: dict[str, int] = {}
    results = []
    for rowid, score in merged:
        turn = fetch_turn(conn, rowid)
        if not turn:
            continue
        sid = turn["session_id"]
        if seen_per_session.get(sid, 0) >= args.per_session:
            continue
        seen_per_session[sid] = seen_per_session.get(sid, 0) + 1
        turn["score"] = score
        turn["snippet"] = make_snippet(turn["content"], query)
        turn["session_meta"] = session_context(conn, sid)
        results.append(turn)
        if len({r["session_id"] for r in results}) >= args.limit:
            break

    if args.open:
        if args.open < 1 or args.open > len(results):
            print(f"ERROR: --open {args.open} fuera de rango (1..{len(results)})", file=sys.stderr)
            sys.exit(2)
        target = results[args.open - 1]
        sid = target["session_id"]
        cwd = target.get("cwd") or str(Path.home())
        # claude --resume only finds sessions scoped to the original project dir,
        # so cd there first. Escape single quotes for AppleScript do script.
        cwd_safe = cwd.replace("'", "'\\''")
        cmd = f"cd '{cwd_safe}' && claude --resume {sid}"
        cmd_as = cmd.replace("\\", "\\\\").replace('"', '\\"')
        import subprocess
        script = f'tell application "Terminal"\n    activate\n    do script "{cmd_as}"\nend tell'
        subprocess.run(["osascript", "-e", script], check=True)
        print(f"Abierta sesión {sid} en Terminal (cwd: {cwd}).")
        return

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print(f'Búsqueda: "{query}"  ({args.mode}, {len(results)} resultados)\n')
    for i, r in enumerate(results, 1):
        meta = r["session_meta"]
        ts = r["timestamp"][:10] if r["timestamp"] else "?"
        sm_first = (meta.get("first_ts") or "")[:10]
        sm_last = (meta.get("last_ts") or "")[:10]
        print(f"[{i}] {ts}  ({r['role']})  sesión {r['session_id'][:8]}...  ({meta['turn_count']} turnos, {sm_first}→{sm_last})")
        if meta.get("first_prompt"):
            fp = meta["first_prompt"].replace("\n", " ")[:140]
            print(f"    ↳ primer prompt: {fp}...")
        print(f"    {r['snippet']}")
        print(f"    → claude --resume {r['session_id']}")
        print()


if __name__ == "__main__":
    main()
