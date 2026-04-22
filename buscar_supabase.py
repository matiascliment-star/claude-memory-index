#!/usr/bin/env python3
"""V2 search: hybrid FTS + pgvector over Supabase memory_turns.

Output format matches V1 buscar.py. --open N downloads the JSONL from Storage
if it is not already present locally, so sessions from other machines can be
resumed too.
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np

from supabase_client import (
    client,
    rpc,
    select,
    storage_download,
    vector_literal,
)

INDEX_DIR = Path.home() / ".claude" / "memory-index"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
SNIPPET_RADIUS = 160


def fts_search(c, query: str, limit: int) -> list[tuple[int, float, str]]:
    if not query.strip():
        return []
    rows = rpc(c, "memory_search_fts", {"q": query, "lim": limit})
    return [(r["id"], float(r["score"]), r["session_id"]) for r in rows]


def semantic_search(c, query: str, limit: int) -> list[tuple[int, float, str]]:
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(INDEX_DIR / "models"))
    qv = list(model.embed([query]))[0].astype(np.float32)
    qv /= np.linalg.norm(qv) + 1e-9
    rows = rpc(c, "memory_search_vec", {"q_emb": vector_literal(qv.tolist()), "lim": limit})
    return [(r["id"], float(r["score"]), r["session_id"]) for r in rows]


def rrf_merge(lists: list[list[tuple[int, float, str]]], k: int = 60) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranked in lists:
        for rank, (rid, _s, _sid) in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def make_snippet(content: str, query: str) -> str:
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    clean = content.replace("\n", " ")
    idx = -1
    for tok in tokens:
        m = re.search(re.escape(tok), clean, flags=re.IGNORECASE)
        if m:
            idx = m.start()
            break
    if idx < 0:
        return clean[: SNIPPET_RADIUS * 2].strip() + ("..." if len(clean) > SNIPPET_RADIUS * 2 else "")
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(clean), idx + SNIPPET_RADIUS)
    s = clean[start:end].strip()
    if start > 0:
        s = "..." + s
    if end < len(clean):
        s = s + "..."
    return s


def detect_active_sessions(max_age_seconds: int = 180) -> set[str]:
    """JSONLs written very recently are almost certainly live Claude Code
    sessions (including this one). Excluding them avoids self-matching when the
    current conversation already mentions the query terms."""
    active: set[str] = set()
    if not PROJECTS_DIR.exists():
        return active
    now = time.time()
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            age = now - jsonl.stat().st_mtime
        except OSError:
            continue
        if 0 <= age <= max_age_seconds:
            active.add(jsonl.stem)
    return active


def fetch_turns(c, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    ids_filter = "(" + ",".join(str(i) for i in ids) + ")"
    rows = select(
        c,
        "memory_turns",
        id=f"in.{ids_filter}",
        select="id,session_id,project,role,content,timestamp,cwd,git_branch,machine_id",
    )
    return {r["id"]: r for r in rows}


def fetch_session_meta(c, session_ids: list[str]) -> dict[str, dict]:
    if not session_ids:
        return {}
    ids_filter = "(" + ",".join(f'"{s}"' for s in session_ids) + ")"
    rows = select(
        c,
        "memory_sessions",
        session_id=f"in.{ids_filter}",
        select="session_id,first_ts,last_ts,turn_count,first_user_prompt,project,cwd,machine_id,jsonl_storage_path",
    )
    return {r["session_id"]: r for r in rows}


def ensure_jsonl_local(c, session: dict) -> tuple[Path, bool]:
    """Make sure the JSONL for this session exists in ~/.claude/projects/<project>/.
    Returns (path, downloaded). Downloads from Storage if absent."""
    target_dir = PROJECTS_DIR / session["project"]
    target = target_dir / f"{session['session_id']}.jsonl"
    if target.exists():
        return target, False
    if not session.get("jsonl_storage_path"):
        raise RuntimeError(f"No JSONL in Storage for session {session['session_id']}")
    data = storage_download(c, session["jsonl_storage_path"])
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target, True


def open_session(c, session: dict) -> None:
    cwd = session.get("cwd") or str(Path.home())
    sid = session["session_id"]
    cwd_path = Path(cwd)
    ensure_jsonl_local(c, session)
    if not cwd_path.exists():
        print(
            f"AVISO: el directorio original {cwd!r} no existe en esta máquina.\n"
            f"       claude --resume puede fallar. Creá o montá ese path y volvé a intentar.",
            file=sys.stderr,
        )
    cwd_safe = cwd.replace("'", "'\\''")
    cmd = f"cd '{cwd_safe}' && claude --resume {sid}"
    cmd_as = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Terminal"\n    activate\n    do script "{cmd_as}"\nend tell'
    subprocess.run(["osascript", "-e", script], check=True)
    print(f"Abierta sesión {sid} en Terminal (cwd: {cwd}).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="+")
    ap.add_argument("-n", "--limit", type=int, default=5)
    ap.add_argument("--mode", choices=["hybrid", "fts", "semantic"], default="hybrid")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--per-session", type=int, default=1)
    ap.add_argument("--open", type=int, metavar="N")
    ap.add_argument("--include-current", action="store_true",
                    help="Do not filter out sessions currently open (by default, JSONLs modified in the last 3 min are excluded).")
    args = ap.parse_args()

    query = " ".join(args.query)
    c = client()

    per_mode_limit = 50
    lists = []
    if args.mode in ("hybrid", "fts"):
        lists.append(fts_search(c, query, per_mode_limit))
    if args.mode in ("hybrid", "semantic"):
        lists.append(semantic_search(c, query, per_mode_limit))

    if len(lists) > 1:
        merged = rrf_merge(lists)
    elif lists:
        merged = [(rid, s) for rid, s, _ in lists[0]]
    else:
        merged = []
    if not merged:
        print("(no results)")
        return

    top_ids = [rid for rid, _ in merged[: per_mode_limit * 2]]
    turns_map = fetch_turns(c, top_ids)

    exclude_sids: set[str] = set() if args.include_current else detect_active_sessions()

    seen: dict[str, int] = {}
    results = []
    for rid, score in merged:
        t = turns_map.get(rid)
        if not t:
            continue
        sid = t["session_id"]
        if sid in exclude_sids:
            continue
        if seen.get(sid, 0) >= args.per_session:
            continue
        seen[sid] = seen.get(sid, 0) + 1
        t["score"] = score
        t["snippet"] = make_snippet(t["content"], query)
        results.append(t)
        if len({r["session_id"] for r in results}) >= args.limit:
            break

    meta_map = fetch_session_meta(c, [r["session_id"] for r in results])
    for r in results:
        r["session_meta"] = meta_map.get(r["session_id"], {})

    if args.open:
        if args.open < 1 or args.open > len(results):
            print(f"ERROR: --open {args.open} fuera de rango (1..{len(results)})", file=sys.stderr)
            sys.exit(2)
        chosen = results[args.open - 1]
        sess = dict(chosen.get("session_meta") or {})
        sess["session_id"] = chosen["session_id"]
        sess.setdefault("project", chosen["project"])
        sess.setdefault("cwd", chosen.get("cwd"))
        open_session(c, sess)
        return

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print(f'Búsqueda: "{query}"  ({args.mode}, {len(results)} resultados)\n')
    for i, r in enumerate(results, 1):
        meta = r.get("session_meta") or {}
        ts = (r.get("timestamp") or "")[:10]
        first = (meta.get("first_ts") or "")[:10]
        last = (meta.get("last_ts") or "")[:10]
        mid_tag = f"  ⬡ {r.get('machine_id','?')}" if r.get("machine_id") else ""
        print(f"[{i}] {ts}  ({r['role']})  sesión {r['session_id'][:8]}...  ({meta.get('turn_count','?')} turnos, {first}→{last}){mid_tag}")
        if meta.get("first_user_prompt"):
            fp = meta["first_user_prompt"].replace("\n", " ")[:140]
            print(f"    ↳ primer prompt: {fp}...")
        print(f"    {r['snippet']}")
        print(f"    → reabrir:  ./buscar_supabase.py \"{query}\" --open {i}")
        print()


if __name__ == "__main__":
    main()
