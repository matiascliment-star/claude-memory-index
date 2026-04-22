# claude-memory-index

Búsqueda híbrida (full-text BM25 + embeddings multilingües) sobre **todo el historial
de sesiones de Claude Code** (`~/.claude/projects/**/*.jsonl`).

Cuando le preguntás *"¿te acordás cuando hablamos de X?"*, el skill ejecuta una
consulta sobre una base SQLite local y devuelve los turnos más relevantes con
fecha, snippet, `session-id` y el comando para reabrir esa conversación.

- **100% local** — nada viaja a la nube, sin APIs externas.
- **Híbrido** — FTS5 (keywords exactas, ranking BM25) + embeddings (semántica, multilingüe).
- **Incremental** — reindex diario vía hook `SessionStart` de Claude Code.
- **Persistente entre sesiones** — abrís una sesión nueva y seguís teniendo acceso a todo lo anterior.

## Instalación

### macOS / Linux

```bash
git clone https://github.com/matiascliment-star/claude-memory-index.git
cd claude-memory-index
bash install.sh
```

Qué hace el installer:
1. Copia `indexar.py` y `buscar.py` a `~/.claude/memory-index/`
2. Crea un venv local e instala `fastembed` + `numpy`
3. Instala el skill `buscar-conversacion` en `~/.claude/skills/`
4. Agrega un hook `SessionStart` a `~/.claude/settings.json` que reindexa al arrancar Claude Code (merge no destructivo)
5. Corre la indexación inicial (tarda ~5–10 min la primera vez porque baja el modelo de embeddings ~220 MB)

### Windows

Todavía no hay un installer automático. Los pasos manuales:

```powershell
# 1. Clonar
git clone https://github.com/matiascliment-star/claude-memory-index.git
cd claude-memory-index

# 2. Copiar scripts
mkdir $HOME\.claude\memory-index
copy indexar.py, buscar.py $HOME\.claude\memory-index\

# 3. Venv + deps
python -m venv $HOME\.claude\memory-index\venv
$HOME\.claude\memory-index\venv\Scripts\pip install -r requirements.txt

# 4. Copiar skill
mkdir $HOME\.claude\skills\buscar-conversacion
copy skills\buscar-conversacion\SKILL.md $HOME\.claude\skills\buscar-conversacion\

# 5. Indexar
$HOME\.claude\memory-index\venv\Scripts\python $HOME\.claude\memory-index\indexar.py
```

Para el hook `SessionStart` en Windows, editar `%USERPROFILE%\.claude\settings.json` a mano
y agregar:

```json
"hooks": {
  "SessionStart": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "start /B %USERPROFILE%\\.claude\\memory-index\\venv\\Scripts\\python.exe %USERPROFILE%\\.claude\\memory-index\\indexar.py --quiet"
        }
      ]
    }
  ]
}
```

El flag `--open N` para reabrir sesiones usa `osascript` (macOS). En Windows/Linux
podés correr manualmente `claude --resume <session-id>` desde el directorio que
te indica la salida de `buscar.py --json`.

## Uso

Desde Claude Code, cualquier frase del tipo:

- *"¿te acordás cuando hablamos de X?"*
- *"buscá la conversación donde definimos Y"*
- *"dónde discutimos Z"*

dispara el skill `buscar-conversacion`.

A mano:

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar.py "tu query"

# Solo full-text (más rápido para keywords únicos):
... buscar.py "BAREIRO" --mode fts

# Solo semántico (para búsquedas conceptuales):
... buscar.py "cuando rompí el MCP" --mode semantic

# Abrir resultado N en una Terminal.app nueva (macOS):
... buscar.py "Montero" --open 1

# JSON para procesar:
... buscar.py "query" --json
```

## Arquitectura

```
~/.claude/
├── memory-index/
│   ├── indexar.py              # lee JSONL, extrae turnos, embed, upsert
│   ├── buscar.py               # híbrido FTS5 + cosine con RRF merge
│   ├── conversations.db        # SQLite (no se comitea)
│   ├── models/                 # cache del modelo de embeddings (no se comitea)
│   ├── venv/                   # virtualenv (no se comitea)
│   └── reindex.log             # log del hook SessionStart
├── skills/
│   └── buscar-conversacion/
│       └── SKILL.md            # descriptor del skill para Claude Code
└── settings.json               # hook SessionStart apunta a indexar.py --quiet
```

### Base de datos

```sql
turns(id, turn_uuid, session_id, project, role, content, timestamp,
      cwd, git_branch, embedding)      -- embedding es un BLOB de 384 floats
turns_fts (FTS5 virtual table sobre content, tokenize unicode61 sin diacríticos)
files_indexed(path, mtime, size, last_line, indexed_at)
```

### Modelo de embeddings

`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dim, ~220 MB
en disco, ONNX via `fastembed`). Soporta español nativamente. Si alguna vez
querés upgradearlo, cambiá `EMBED_MODEL` y `EMBED_DIM` en los dos scripts y
corré `indexar.py --force`.

## Privacidad

- La base (`conversations.db`) nunca sale de tu máquina.
- El modelo corre 100% local.
- El repo en GitHub solo contiene los scripts, nunca tus conversaciones.

## Licencia

MIT.
