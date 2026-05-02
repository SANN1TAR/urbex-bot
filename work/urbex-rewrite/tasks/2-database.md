---
status: completed
wave: 1
depends_on: [1-config]
skills: [code-writing]
reviewers: [code-reviewer, security-auditor]
---

# Task 2 — Rewrite database.py for asyncpg/PostgreSQL

## Description

Replace the SQLite/aiosqlite implementation with asyncpg (PostgreSQL). Implement the
archive model: objects are never deleted, only added. Add `cities` table for fetch
tracking. Store lat/lon as separate REAL columns for coordinate deduplication.

## What to do

1. Rewrite all functions in `database.py` to use asyncpg
2. Use a single `asyncpg.Pool` passed as parameter to all functions (created in `main()`)
3. Implement `init_db(pool)` — creates all 3 tables if not exist (idempotent)
4. Implement `get_city_last_fetched(pool, city)` → `datetime | None`
5. Implement `update_city_fetched(pool, city)` — upsert into cities table
6. Implement `save_objects(pool, city, objects)` — INSERT OR IGNORE (archive, never delete)
7. Implement `get_objects(pool, city, shown_ids: set[int], limit=3)` — random, excluding shown
8. Implement `get_user(pool, telegram_id)` → `dict | None`
9. Implement `save_user(pool, telegram_id, city)` — upsert
10. Remove: `clear_city_cache()`, `get_cache_age_days()`, `get_city_count()`, `get_all_cached_cities()`
11. Add to `requirements.txt`: `asyncpg==0.29.0`
12. Remove from `requirements.txt`: `aiosqlite==0.20.0`

## Schema

```sql
CREATE TABLE IF NOT EXISTS cities (
    name TEXT PRIMARY KEY,
    last_fetched_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    city        TEXT NOT NULL,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS objects (
    id          SERIAL PRIMARY KEY,
    city        TEXT NOT NULL,
    osm_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    address     TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT 'Urban3P',
    image       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(city, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_objects_city ON objects(city);
CREATE INDEX IF NOT EXISTS idx_objects_latlon ON objects(lat, lon);
```

## Object dict format (input to save_objects)

```python
{
    "name": str,
    "lat": float | None,     # NEW — separate lat
    "lon": float | None,     # NEW — separate lon
    "address": str,
    "source_name": str,
    "image": str,
    "osm_id": str,           # unique key: osm_id or name
}
```

## TDD Anchor

Manual smoke:
```bash
python -c "
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
async def test():
    pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'))
    from database import init_db
    await init_db(pool)
    print('DB init OK')
    await pool.close()
asyncio.run(test())
"
```

## Acceptance Criteria

- [ ] `database.py` imports cleanly (no aiosqlite, no sqlite3)
- [ ] `init_db(pool)` creates all 3 tables without error on Supabase
- [ ] `save_objects()` with same objects twice → no error (INSERT OR IGNORE)
- [ ] `get_objects()` returns objects sorted randomly, excluding shown_ids
- [ ] `get_user()` returns None for unknown user
- [ ] `save_user()` is idempotent (upsert)
- [ ] `asyncpg==0.29.0` in requirements.txt
- [ ] `aiosqlite` removed from requirements.txt

## Context Files

- `database.py` — current SQLite implementation to rewrite
- `bot.py` — shows how database functions are called
- `config.py` — Config.database_url for connection string
- `work/urbex-rewrite/tech-spec.md` — Data Models section

## Verify-smoke

```bash
python -c "import database; print('import OK')"
```

## Post-completion

Update status to `completed`.
