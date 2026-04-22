---
name: buscar-conversacion
description: Busca en el historial completo de conversaciones de Claude Code (todas las sesiones previas en ~/.claude/projects/). Usa búsqueda híbrida (full-text BM25 + embeddings semánticos multilingües) sobre una base SQLite local. Devuelve los turnos más relevantes con fecha, snippet, session-id y el comando claude --resume para reabrir esa conversación. Usar cuando el usuario pregunte "¿te acordás cuando hablamos de X?", "buscá aquella conversación sobre Y", "qué dijimos la otra vez de Z", "buscá en el historial", "dónde hablamos de", "en qué sesión", "recordás cuando", o cualquier consulta sobre charlas pasadas conmigo. Triggers literales: "te acordás", "acordate", "recordás", "busca en conversaciones", "busca historial", "qué conversación", "cuál fue la sesión", "buscar en memoria", "hablamos de".
---

# Skill: buscar-conversacion

Busca en el historial completo de charlas con el usuario (todas las sesiones de Claude Code).

## Cómo funciona

La base SQLite vive en `~/.claude/memory-index/conversations.db`. Se indexa leyendo todos los JSONL de `~/.claude/projects/*/*.jsonl`. Cada turno (user + assistant) se guarda con:

- Texto completo en FTS5 (búsqueda literal con BM25 y stemming)
- Embedding multilingüe de 384 dim (búsqueda semántica por concepto)

`buscar.py` hace **búsqueda híbrida** fusionando ambos resultados con Reciprocal Rank Fusion.

## Uso desde Claude Code

Cuando el usuario pregunte por una conversación pasada, ejecutá:

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar.py "<query del usuario>"
```

Flags útiles:
- `-n 10` → más resultados (default 5)
- `--mode fts` → solo full-text (más rápido si hay una palabra única como un apellido)
- `--mode semantic` → solo semántico (si el usuario describe un concepto sin palabras exactas)
- `--json` → para parsear la salida y reformatearla
- `--per-session 3` → traer más de un turno por sesión (útil si vas a profundizar en una conversación específica)

## Cómo presentar los resultados al usuario

1. Mostrale los top 3-5 con fecha, snippet y primer prompt de la sesión.
2. Preguntá cuál quiere reabrir (por número).
3. Para reabrir, corré `buscar.py` de nuevo con `--open N` (N = número del resultado). Esto abre `claude --resume <id>` en una Terminal.app nueva vía osascript. No uses el comando `claude --resume` dentro de Bash tool — spawnearía una sub-sesión dentro de la actual.

## Si la query es ambigua

- Empezá con `--mode hybrid` (default).
- Si no hay hits obvios y el query parece conceptual, probá `--mode semantic` con términos reformulados.
- Si el usuario mencionó una palabra muy específica (apellido, número de expediente, nombre de skill), probá `--mode fts` para ordenar por BM25 puro.

## Reindexación

Para reindexar sesiones nuevas (incremental, rápido):

```bash
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/indexar.py --quiet
```

Esto se corre solo en el scheduled trigger diario (`reindex-memory`), pero podés forzarlo manualmente si el usuario acaba de tener una conversación importante en otra ventana y quiere buscarla ya.

## Estructura de la base

```sql
turns(id, turn_uuid, session_id, project, role, content, timestamp, cwd, git_branch, embedding)
turns_fts (FTS5 virtual table sobre content)
files_indexed(path, mtime, size, last_line, indexed_at)
```
