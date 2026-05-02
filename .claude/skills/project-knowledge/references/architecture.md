# Architecture

## Tech Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| Language | Python | 3.11 |
| Bot framework | aiogram | 3.7.0 |
| Database | aiosqlite (SQLite) | 0.20.0 |
| HTTP client | httpx | 0.27.0 |
| Search API | Tavily | tavily-python 0.5.0 |
| LLM (name search only) | Groq — llama-3.1-8b-instant | groq 0.13.1 |
| Env vars | python-dotenv | 1.0.1 |

## File Structure

```
urbex-bot/
├── bot.py          # Entry point: bot setup, FSM handlers, UI logic
├── search.py       # Search logic: Tavily + urban3p.ru scraping + Nominatim
├── database.py     # SQLite CRUD: users table + objects cache table
├── requirements.txt
├── Procfile        # Railway: "worker: python bot.py"
├── runtime.txt     # Python 3.11
└── .env            # TELEGRAM_TOKEN, GROQ_API_KEY, TAVILY_API_KEY (not in git)
```

## External Integrations

### Tavily API
- **Role**: primary search engine
- **Used in**: `search.py` — `_fetch_from_web()` and `_tavily_photo()` and `search_by_name()`
- **Queries**: `"заброс заброшенная {город} site:urban3p.ru"` + `"site:urban3p.com"`
- **Returns**: search results with titles, URLs, content snippets, images
- **Limit**: paid API, rate limits apply

### urban3p.ru (scraping via httpx)
- **Role**: primary data source for abandoned objects in Russia/CIS
- **Used in**: `search.py` — `_scrape_object_page()`
- **What we scrape**: OG image (`og:image` meta tag), address (regex), coordinates via `/onmap` POST
- **Fragile**: depends on site HTML structure; `/onmap` POST form may change

### Nominatim (OpenStreetMap geocoding)
- **Role**: fallback geocoding when urban3p.ru `/onmap` doesn't return coords
- **Used in**: `search.py` — `_get_coords_nominatim()`
- **Query**: `"{cleaned_name}, {city}, Россия"`
- **Limitation**: poor at industrial/abandoned buildings by name

### Groq (LLM)
- **Role**: parse search results for name-based search only
- **Model**: `llama-3.1-8b-instant` (500k tokens/day free)
- **Used in**: `search.py` — `search_by_name()`
- **NOT used** for browsing — only `/поиск по названию`

## FSM States

```
Reg.city        — waiting for user to type their city
Search.query    — waiting for user to type search query
Browsing.active — user is browsing objects (Next/Restart callbacks active)
```

## Data Model

### Table: users
```sql
CREATE TABLE users (
    telegram_id INTEGER PRIMARY KEY,
    city        TEXT NOT NULL,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

### Table: objects
```sql
CREATE TABLE objects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    city        TEXT NOT NULL,
    osm_id      TEXT,           -- unique key per city: osm_id or object name
    name        TEXT NOT NULL,
    coords      TEXT,           -- "lat, lon" string or empty
    address     TEXT,
    description TEXT,           -- currently always empty (removed from card)
    security    TEXT,           -- guard info, currently always empty
    source_name TEXT,           -- always "Urban3P"
    image       TEXT,           -- URL to image
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(city, osm_id)
)
```

Cache TTL: `CACHE_TTL_DAYS = 7` days. Age checked via `julianday('now') - julianday(MAX(created_at))`.

## Key In-Memory State

```python
_shown_global: dict[int, set] = {}   # uid → set of shown object names
                                      # LOST on restart, never cleaned up (memory leak)
```

## Search Flow

```
User clicks "Заброшка"
  → check SQLite cache (count + age)
  → if empty or stale:
      Tavily search (urban3p.ru) → 10 results × 2 sources
      for each result:
        _scrape_object_page(url)   → OG image + /onmap coords
        if no coords: Nominatim fallback
        if no image: Tavily image search fallback
      save_objects() → SQLite
  → get_objects(city, shown, limit=3) → random 3 not-yet-shown
  → show one at a time via _send_one()
```

## Background Tasks

`_refresh_cache_loop()` — runs every 24h, refreshes stale city caches.
Created via `asyncio.create_task()` in `main()`. Failure is silent (no exception handler).
