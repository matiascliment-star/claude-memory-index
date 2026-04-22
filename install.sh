#!/usr/bin/env bash
# Installer for claude-memory-index (macOS / Linux).
# Copies scripts to ~/.claude/memory-index, sets up venv, installs skill,
# registers SessionStart hook in ~/.claude/settings.json, runs initial index.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${HOME}/.claude/memory-index"
SKILLS_DIR="${HOME}/.claude/skills"
SETTINGS="${HOME}/.claude/settings.json"

echo "==> Instalando claude-memory-index en ${TARGET}"
mkdir -p "${TARGET}" "${SKILLS_DIR}"

# 1) Copiar scripts
cp "${REPO_DIR}/indexar.py" "${REPO_DIR}/buscar.py" "${TARGET}/"

# 2) Venv + deps
if [[ ! -d "${TARGET}/venv" ]]; then
    echo "==> Creando venv (puede tardar)"
    python3 -m venv "${TARGET}/venv"
fi
"${TARGET}/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"${TARGET}/venv/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

# 3) Skill
mkdir -p "${SKILLS_DIR}/buscar-conversacion"
cp "${REPO_DIR}/skills/buscar-conversacion/SKILL.md" "${SKILLS_DIR}/buscar-conversacion/"

# 4) Registrar hook en settings.json (merge con python para no romper JSON)
HOOK_CMD="nohup ${TARGET}/venv/bin/python ${TARGET}/indexar.py --quiet >> ${TARGET}/reindex.log 2>&1 &"
python3 - <<EOF
import json, os, pathlib
p = pathlib.Path("${SETTINGS}")
data = {}
if p.exists():
    try:
        data = json.loads(p.read_text())
    except Exception:
        pass
hooks = data.setdefault("hooks", {})
starts = hooks.setdefault("SessionStart", [])
cmd = """${HOOK_CMD}"""
already = any(
    any(h.get("command") == cmd for h in (entry.get("hooks") or []) if isinstance(h, dict))
    for entry in starts if isinstance(entry, dict)
)
if not already:
    starts.append({"hooks": [{"type": "command", "command": cmd}]})
    p.write_text(json.dumps(data, indent=2))
    print("    → hook SessionStart agregado")
else:
    print("    → hook SessionStart ya presente")
EOF

# 5) Indexación inicial (silenciosa, en primer plano para que veas el progreso)
echo "==> Indexación inicial (5–10 min en la primera corrida, descarga el modelo)"
"${TARGET}/venv/bin/python" "${TARGET}/indexar.py"

echo ""
echo "==> Listo."
echo "    Probá desde Claude Code: 'te acordás cuando hablamos de X'"
echo "    O corré a mano: ${TARGET}/venv/bin/python ${TARGET}/buscar.py 'query'"
