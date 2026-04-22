# claude-memory-index

Búsqueda híbrida (full-text BM25 + embeddings multilingües) sobre **todo el historial
de sesiones de Claude Code** (`~/.claude/projects/**/*.jsonl`).

Cuando le preguntás *"¿te acordás cuando hablamos de X?"*, el skill ejecuta una
consulta y devuelve los turnos más relevantes con fecha, snippet, `session-id` y
el comando para reabrir esa conversación.

## Dos modos

### V2 — Supabase + Storage (**recomendado**)

- Guarda turnos + embeddings en Postgres (pgvector + tsvector).
- Sube los JSONL a un bucket de Storage.
- **Cross-machine:** instalás en otra Mac, corrés el installer y tenés acceso a todo
  tu historial. El skill baja el JSONL on-demand al reabrir sesiones que nacieron
  en otra máquina.
- Requiere Supabase (plan gratis alcanza).

### V1 — SQLite local (offline)

- Todo vive en `~/.claude/memory-index/conversations.db`.
- Cero dependencias de red, cero cuenta de nada.
- No se sincroniza entre máquinas.

## Instalación

### V2 (Supabase) — por defecto

1. Conseguí credenciales de un proyecto Supabase con pgvector disponible:
   - `SUPABASE_URL=https://<ref>.supabase.co`
   - `SUPABASE_KEY=<service_role_jwt>`  (o `SUPABASE_SERVICE_ROLE_KEY`)

2. Agregalas a `~/.env`:
   ```bash
   echo 'SUPABASE_URL=https://<ref>.supabase.co' >> ~/.env
   echo 'SUPABASE_KEY=eyJhbGciOi...' >> ~/.env
   ```

3. Aplicá el schema. Si usás el MCP de Supabase desde Claude Code se lo podés
   pegar entero al Claude para que lo corra, o usar la UI:

   ```sql
   -- Ver supabase_schema.sql en este repo
   ```

4. Cloná e instalá:
   ```bash
   git clone https://github.com/matiascliment-star/claude-memory-index.git
   cd claude-memory-index
   bash install.sh
   ```

5. (Opcional) Si tenías V1 corriendo, migrá los turnos ya indexados:
   ```bash
   ~/.claude/memory-index/venv/bin/python \
       ~/.claude/memory-index/migrate_sqlite_to_supabase.py
   ```

### V1 (SQLite local)

```bash
git clone https://github.com/matiascliment-star/claude-memory-index.git
cd claude-memory-index
MODE=local bash install.sh
```

## Uso

Desde Claude Code, cualquier frase del tipo:

- *"¿te acordás cuando hablamos de X?"*
- *"buscá la conversación donde definimos Y"*
- *"dónde hablamos de Z"*

dispara el skill `buscar-conversacion`.

A mano:

```bash
# V2
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar_supabase.py "tu query"

# V1
~/.claude/memory-index/venv/bin/python ~/.claude/memory-index/buscar.py "tu query"
```

Flags:
- `-n 10` más resultados (default 5)
- `--mode fts` solo full-text (mejor para apellidos, nombres de skill)
- `--mode semantic` solo semántico (para conceptos sin palabras exactas)
- `--json` salida JSON
- `--per-session N` más de un turno por sesión
- `--open N` reabrir el resultado N en Terminal nueva (macOS). **V2 baja el
  JSONL de Storage si la sesión nació en otra máquina**.

## Arquitectura V2

```
┌─ Mac #1 ──────────────────────────────────┐        ┌─ Mac #2 ─────────────────┐
│  ~/.claude/projects/*/*.jsonl             │        │  ~/.claude/projects/...  │
│          │                                │        │          │               │
│  indexar_supabase.py  (hook SessionStart) │        │  indexar_supabase.py     │
│          │  │                             │        │          │               │
└──────────┼──┼─────────────────────────────┘        └──────────┼───────────────┘
           │  │                                                 │
           ▼  ▼                                                 ▼
   ┌──────────────────────────── Supabase ─────────────────────────────┐
   │  memory_turns (embedding vector(384), content_tsv, machine_id)    │
   │  memory_sessions (metadata + jsonl_storage_path)                  │
   │  memory_files_indexed (incremental bookkeeping, por máquina)      │
   │  Storage bucket: claude-memory-jsonl/<machine>/<project>/*.jsonl  │
   └────────────────────────────────▲──────────────────────────────────┘
                                    │
                     buscar_supabase.py (desde cualquier Mac)
                                    │
                     skill `buscar-conversacion` en Claude Code
```

### Base de datos

```sql
memory_turns(
  id bigserial pk,
  turn_uuid text unique,       -- idempotent upserts
  session_id text,
  project text,
  machine_id text,              -- origen
  role text check (role in ('user','assistant')),
  content text,
  "timestamp" timestamptz,
  cwd text,
  git_branch text,
  embedding vector(384),        -- fastembed multilingüe
  content_tsv tsvector,         -- generated, indexado con GIN
  created_at timestamptz default now()
);

memory_sessions(
  session_id text pk,
  project text,
  machine_id text,
  cwd text,
  first_ts timestamptz,
  last_ts timestamptz,
  turn_count int,
  first_user_prompt text,
  jsonl_storage_path text,      -- path en Storage
  jsonl_mtime, jsonl_size,
  updated_at timestamptz
);

memory_files_indexed(
  path text,
  machine_id text,
  mtime double precision,
  size bigint,
  last_line int,
  primary key (path, machine_id)
);
```

### Funciones RPC

- `memory_search_fts(q text, lim int)` — FTS con ts_rank
- `memory_search_vec(q_emb vector(384), lim int)` — cosine similarity
- `memory_refresh_all_sessions(mid text)` — recalcula metadata por máquina
- `memory_set_jsonl_path(sid, path, mtime, size)` — actualiza puntero a Storage

### Modelo de embeddings

`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384 dim, ~220 MB,
ONNX via `fastembed`). Español nativo. Los embeddings se calculan **local** en
cada máquina y viajan como vectores a Supabase — el texto nunca pasa por una API
de terceros.

## Resume cross-machine

Flujo cuando pedís `--open N` de una sesión que nació en Mac #1 y estás en Mac #2:

1. `buscar_supabase.py` consulta `memory_sessions.jsonl_storage_path`.
2. Baja el JSONL del bucket y lo escribe en
   `~/.claude/projects/<project>/<session_id>.jsonl` local.
3. `osascript` abre una Terminal en el `cwd` original con
   `claude --resume <session_id>`.
4. **Gotcha:** Claude Code matchea el path por CWD. Si en Mac #2 el directorio
   original no existe (ej. otra ruta de OneDrive), `claude --resume` va a
   mostrar un error. Te imprime un aviso. Workaround: creá el path o montá el
   OneDrive en la misma ruta.

## Privacidad

- Los embeddings + texto **sí** van a Supabase (V2). Si tus charlas son
  sensibles, usá V1 (SQLite).
- El bucket es **privado** (no public).
- Las credenciales `SUPABASE_KEY` viven solo en `~/.env`, nunca se comitean.
- El modelo de embeddings corre 100% local.

## Licencia

MIT.
