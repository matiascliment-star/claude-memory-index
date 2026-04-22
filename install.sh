#!/usr/bin/env bash
# Installer for claude-memory-index (macOS / Linux).
#
# By default installs V2 (Supabase backend). Requires SUPABASE_URL and
# SUPABASE_KEY (service_role) in ~/.env or environment. V2 enables
# cross-machine memory and JSONL resume from Storage.
#
# To install V1 (SQLite only, fully offline) run with: MODE=local bash install.sh

set -euo pipefail

MODE="${MODE:-supabase}"   # supabase | local
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${HOME}/.claude/memory-index"
SKILLS_DIR="${HOME}/.claude/skills"
SETTINGS="${HOME}/.claude/settings.json"

echo "==> Instalando claude-memory-index (${MODE}) en ${TARGET}"
mkdir -p "${TARGET}" "${SKILLS_DIR}"

# 1) Copiar scripts
cp "${REPO_DIR}/indexar.py" "${REPO_DIR}/buscar.py" "${TARGET}/"
if [[ "${MODE}" == "supabase" ]]; then
    cp "${REPO_DIR}/supabase_client.py" \
       "${REPO_DIR}/indexar_supabase.py" \
       "${REPO_DIR}/buscar_supabase.py" \
       "${REPO_DIR}/migrate_sqlite_to_supabase.py" \
       "${TARGET}/"
fi

# 2) Venv + deps
if [[ ! -d "${TARGET}/venv" ]]; then
    echo "==> Creando venv"
    python3 -m venv "${TARGET}/venv"
fi
"${TARGET}/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"${TARGET}/venv/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

# 3) Skill
mkdir -p "${SKILLS_DIR}/buscar-conversacion"
cp "${REPO_DIR}/skills/buscar-conversacion/SKILL.md" "${SKILLS_DIR}/buscar-conversacion/"

# 4) Credenciales (solo modo supabase)
if [[ "${MODE}" == "supabase" ]]; then
    if ! grep -qE '^SUPABASE_URL\s*=' "${HOME}/.env" 2>/dev/null; then
        echo ""
        echo "!! Falta SUPABASE_URL en ~/.env"
        echo "   Agregalo antes de indexar:"
        echo "       echo 'SUPABASE_URL=https://<ref>.supabase.co' >> ~/.env"
        echo "       echo 'SUPABASE_KEY=<service_role_jwt>' >> ~/.env"
        echo ""
    fi
fi

# 5) Registrar hook SessionStart
if [[ "${MODE}" == "supabase" ]]; then
    INDEXER="indexar_supabase.py"
else
    INDEXER="indexar.py"
fi
HOOK_CMD="nohup ${TARGET}/venv/bin/python ${TARGET}/${INDEXER} --quiet >> ${TARGET}/reindex.log 2>&1 &"
python3 - <<EOF
import json, pathlib
p = pathlib.Path("${SETTINGS}")
data = {}
if p.exists():
    try: data = json.loads(p.read_text())
    except Exception: pass
hooks = data.setdefault("hooks", {})
starts = hooks.setdefault("SessionStart", [])
cmd = """${HOOK_CMD}"""
# Remove old indexer hooks to avoid duplicates
starts[:] = [e for e in starts if not any(
    isinstance(h, dict) and "memory-index" in (h.get("command") or "")
    for h in (e.get("hooks") or [])
)]
starts.append({"hooks": [{"type": "command", "command": cmd}]})
p.write_text(json.dumps(data, indent=2))
print("    → hook SessionStart actualizado")
EOF

# 6) Indexación inicial
echo "==> Indexación inicial (5–10 min en la primera corrida, baja el modelo de embeddings)"
"${TARGET}/venv/bin/python" "${TARGET}/${INDEXER}"

echo ""
echo "==> Listo."
echo "    Probá desde Claude Code: 'te acordás cuando hablamos de X'"
if [[ "${MODE}" == "supabase" ]]; then
    echo "    Si querés migrar el historial local (SQLite) a Supabase:"
    echo "        ${TARGET}/venv/bin/python ${TARGET}/migrate_sqlite_to_supabase.py"
fi
