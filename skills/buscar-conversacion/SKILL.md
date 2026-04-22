---
name: buscar-conversacion
description: Busca en el historial completo de conversaciones de Claude Code (todas las sesiones previas, incluso las que nacieron en OTRA máquina). Backend Supabase con pgvector (semántico) + tsvector (full-text) + Storage bucket con los JSONL para poder reabrir sesiones cross-machine. Devuelve los turnos más relevantes con fecha, snippet, session-id y comando para reabrirlos. Usar cuando el usuario pregunte "¿te acordás cuando hablamos de X?", "buscá aquella conversación sobre Y", "qué dijimos la otra vez de Z", "buscá en el historial", "dónde hablamos de", "en qué sesión", "recordás cuando", o cualquier consulta sobre charlas pasadas conmigo. Triggers literales: "te acordás", "acordate", "recordás", "busca en conversaciones", "busca historial", "qué conversación", "cuál fue la sesión", "buscar en memoria", "hablamos de".
---

# Skill: buscar-conversacion

Busca en el historial completo de charlas con el usuario, incluso entre máquinas.

## Backend actual: Supabase (V2)

- Tablas: `memory_turns`, `memory_sessions`, `memory_files_indexed`
- Storage bucket: `claude-memory-jsonl` (JSONLs de todas las sesiones)
- Proyecto: `wdgdbbcwcrirpnfdmykh` (Estudio Jurídico)
- Scripts: `~/.claude/memory-index/buscar_supabase.py`, `indexar_supabase.py`, `supabase_client.py`
- Credenciales: `SUPABASE_URL` y `SUPABASE_KEY` en `~/.env` (service_role)

El indexador corre en el hook `SessionStart` — cada vez que abrís Claude Code,
sincroniza turnos nuevos y sube los JSONL cambiados al bucket.

## Uso desde Claude Code

Cuando el usuario pregunte por una conversación pasada:

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar_supabase.py "<query>"
```

Flags:
- `-n 10` → más resultados (default 5)
- `--mode fts` → solo full-text (más preciso si hay una palabra única: apellido, nombre de skill, nº expediente)
- `--mode semantic` → solo semántico (si el usuario describe un concepto sin palabras exactas)
- `--json` → para parsear y reformatear
- `--per-session 3` → más de un turno por sesión (para profundizar)
- `--open N` → **reabre el resultado N** en una Terminal nueva (macOS); baja el JSONL de Storage si la sesión nació en otra máquina
- `--include-current` → incluye sesiones activas. Por default, el script excluye cualquier sesión cuyo JSONL fue escrito en los últimos 3 min (evita que la charla actual se cuele como resultado, porque el indexador corre en `SessionStart`).

## Cómo presentar los resultados

1. Mostrá top 3-5 con fecha, snippet, primer prompt, máquina de origen (⬡).
2. Preguntá cuál reabrir por número.
3. Para abrir: volvé a correr `buscar_supabase.py "<misma query>" --open N`.
   NO uses `claude --resume <id>` directamente desde Bash tool porque spawnearía
   una sub-sesión dentro de la actual y porque el comando necesita `cd` al cwd
   original (el script lo hace vía osascript).

## Si la query es ambigua

- Empezá con `--mode hybrid` (default, fusión RRF).
- Palabra específica (apellido, expediente, skill) → `--mode fts`.
- Concepto vago → `--mode semantic` con términos reformulados.

## Reindexación manual

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/indexar_supabase.py --quiet
```

Ya corre solo en `SessionStart`. Forzalo a mano si el usuario acaba de charlar
mucho en otra ventana y quiere buscarlo ya.

## Fallback V1 (SQLite local)

Si Supabase no está disponible o el usuario pidió modo offline:

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar.py "<query>"
```

Misma interfaz, base local `~/.claude/memory-index/conversations.db`.
