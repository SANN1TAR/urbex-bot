---
created: 2026-05-02
status: approved
branch: master
size: L
---

# Tech Spec — Urbex Bot Complete Rewrite

## Solution

Full rewrite of urbex-bot: replace SQLite with Supabase PostgreSQL, remove all LLM
dependencies, implement archive model, coordinate-based deduplication, strict location
filter, and fix all 28 audit bugs.

## Architecture

### Components

| File | Role | Changes |
|------|------|---------|
| `config.py` | **NEW** — env validation + constants | create from scratch |
| `database.py` | PostgreSQL CRUD via asyncpg | full rewrite |
| `search.py` | Tavily + urban3p.ru scraping | major refactor (remove LLM/Nominatim) |
| `bot.py` | Telegram handlers + FSM | major refactor (remove search feature, fix bugs) |
| `requirements.txt` | Dependencies | remove groq, add asyncpg |

### Data Flow

```
User clicks "Заброшка"
  → check cities table: last_fetched_at for city
  → if NULL or > 7 days ago:
      Tavily search urban3p.ru (2 sources, 10 results each)
      for each result URL (/objectNNNN):
        scrape page: OG image + /onmap coords
        parse coords → lat, lon (REAL)
        parse address from page HTML
        if no lat/lon AND no address → SKIP (don't save)
        check dedup: any object within 100m? → SKIP
        save to objects table
      update cities.last_fetched_at = NOW()
  → get_objects(city, shown_ids, limit=3)
  → show one at a time

User clicks "Следующий"
  → load next from in-memory cache (3 objects pre-fetched)
  → if cache empty: fetch 3 more from DB
```

### Shared Resources

- `asyncpg.Pool` — single connection pool, created at startup, reused across all requests
- `TavilyClient` — module-level singleton (thread-safe for asyncio.to_thread usage)
- `httpx.AsyncClient` — reused across requests via module-level instance

## Key Technical Decisions

### 1. Supabase PostgreSQL via asyncpg
**Why:** Railway ephemeral storage kills SQLite on every deploy. asyncpg is the fastest
async PostgreSQL driver for Python. Supabase free tier: 500MB, no expiry.
**Alternative considered:** aiopg (slower), psycopg3 (less mature ecosystem).
**Connection:** `DATABASE_URL` env var → `asyncpg.create_pool()` at startup.

### 2. Remove Groq/LLM completely
**Why:** LLM was only used in `search_by_name()` which is being removed entirely.
LLM-generated coordinates were the primary source of hallucinated data.
**Impact:** Remove `groq` from requirements.txt, delete `search_by_name()`, delete `Search` FSM state.

### 3. Remove Nominatim geocoding
**Why:** Nominatim poorly resolves industrial/abandoned buildings by name → wrong coordinates.
Only urban3p.ru `/onmap` POST coordinates are reliable (they come from the site's own map data).
**Impact:** Delete `_get_coords_nominatim()`. If `/onmap` returns no coords → use address or skip.

### 4. Archive model (objects never deleted)
**Why:** User wants a growing database of objects. Re-fetching replaces cache in old model.
**New behavior:** `last_fetched_at` per city controls fetch frequency (7 days). Objects accumulate.
`/restart` triggers re-fetch and ADDS new objects, doesn't clear old ones.
**Impact:** Remove `clear_city_cache()`. Add `cities` table. Change `save_objects()` to INSERT OR IGNORE.

### 5. Coordinate deduplication (Haversine ~100m)
**Why:** urban3p.ru has multiple pages for same location → duplicates under different names.
**Implementation:** Before INSERT, query: objects within ±0.001° (≈90-110m at Moscow latitude).
If match found → skip new object.
**Schema:** Store `lat REAL` and `lon REAL` as separate columns (not string "55.1234, 37.5678").
Display string `coords` computed on read: `f"{lat:.4f}, {lon:.4f}"` if lat/lon exist.

### 6. Location filter at scrape time
**Why:** Objects without any location data are useless for urbex purposes.
**Rule:** Skip object if `lat IS NULL AND lon IS NULL AND address = ''`.
**Display rule:** Show object if has lat/lon OR address. Photo optional.

### 7. asyncpg connection pool
**Why:** Old aiosqlite opened new connection per function call → no pooling, lock contention.
asyncpg pool: `min_size=2, max_size=10`. Created once in `main()`, passed to all DB functions.
**Pattern:** All DB functions accept `pool: asyncpg.Pool` parameter.

## Data Models

### Table: cities
```sql
CREATE TABLE IF NOT EXISTS cities (
    name TEXT PRIMARY KEY,
    last_fetched_at TIMESTAMP WITH TIME ZONE
);
```

### Table: users
```sql
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    city        TEXT NOT NULL,
    registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### Table: objects
```sql
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

## Dependencies

### Remove
- `groq==0.13.1` → delete

### Add
- `asyncpg==0.29.0` → PostgreSQL async driver

### Keep
- `aiogram==3.7.0`
- `tavily-python==0.5.0`
- `httpx==0.27.0`
- `python-dotenv==1.0.1`

## Testing Strategy

No automated tests exist. Manual verification:
- Smoke test: bot starts, connects to Supabase, responds to /start
- Object test: click "Заброшка" → object appears with lat/lon or address
- Dedup test: run fetch twice for same city → no new duplicates in DB
- Fallback test: set DATABASE_URL to wrong host → startup fails with clear error

## Agent Verification Plan

```bash
# 1. Bot starts without crash
python bot.py &
sleep 3
kill %1  # should not have crashed

# 2. DB schema exists in Supabase
python -c "import asyncio, asyncpg, os; asyncio.run(asyncpg.connect(os.getenv('DATABASE_URL')))"

# 3. No groq import
python -c "import bot" 2>&1 | grep -v groq  # should not import groq

# 4. search_by_name removed
python -c "from search import search_by_name" 2>&1 | grep ImportError
```

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| urban3p.ru blocks Railway IP | Medium | High | Fallback to archived objects in DB |
| Supabase free tier row limit (50k) | Low | Medium | 50k objects = years of accumulation |
| Tavily 1000 req/month limit | Medium | Medium | Cache 7 days, minimal re-fetches |
| asyncpg connection pool exhausted | Low | High | pool max_size=10, connections returned after each query |

## User-Spec Deviations

None. All requirements from user-spec are covered.

## Acceptance Criteria

- [ ] Bot starts and connects to Supabase without errors
- [ ] Missing env var at startup → clear error message, bot exits
- [ ] "Заброшка" returns object with lat/lon OR address (never empty location)
- [ ] No hallucinated coordinates (Nominatim removed, only /onmap data used)
- [ ] No duplicate objects within 100m radius in same city
- [ ] "Следующий" works without crash on rapid double-tap
- [ ] User registration persists after Railway redeploy
- [ ] Object archive persists after Railway redeploy
- [ ] "🔍 Поиск по названию" button absent from keyboard
- [ ] /help text updated, no mention of name search
- [ ] /restart adds new objects without clearing archive

## Implementation Tasks

### Wave 1 — Foundation (config + database)
**Task 1.1** — Create config.py with env validation
- Skills: code-writing
- Reviewers: code-reviewer, security-auditor
- Verify-smoke: `python -c "from config import Config; print(Config)"`
- Files: config.py (new)

**Task 1.2** — Rewrite database.py for asyncpg/PostgreSQL
- Skills: code-writing
- Reviewers: code-reviewer, security-auditor
- Depends on: 1.1
- Verify-smoke: `python -c "import asyncio; from database import init_db; asyncio.run(init_db())"`
- Files: database.py, requirements.txt

### Wave 2 — Search Quality (search.py refactor)
**Task 2.1** — Refactor search.py: remove LLM/Nominatim, add location filter + dedup
- Skills: code-writing
- Reviewers: code-reviewer, security-auditor
- Depends on: 1.2
- Verify-smoke: `python -c "import search"  # no ImportError`
- Files: search.py

### Wave 3 — Bot Refactor (bot.py + UX)
**Task 3.1** — Refactor bot.py: remove search feature, fix all bugs, update keyboard
- Skills: code-writing
- Reviewers: code-reviewer
- Depends on: 1.2, 2.1
- Verify-smoke: `python bot.py &` → no crash in 5 seconds
- Files: bot.py

### Audit Wave
- code-reviewer: all changed files
- security-auditor: config.py, database.py (credentials handling)

### Final Wave
- Update requirements.txt
- Update DOCS.txt, VERSIONS.txt
- Commit + push
