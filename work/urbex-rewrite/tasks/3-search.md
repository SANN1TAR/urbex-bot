---
status: completed
wave: 2
depends_on: [2-database]
skills: [code-writing]
reviewers: [code-reviewer, security-auditor]
---

# Task 3 — Refactor search.py: remove LLM/Nominatim, add location filter + dedup

## Description

Major refactor of `search.py`. Remove all LLM (Groq) and Nominatim code. Add strict
location filter (skip objects with no lat/lon AND no address). Add coordinate-based
deduplication (100m radius). Update `search_objects()` to use asyncpg pool.

## What to do

### Remove entirely:
- `groq` import and `groq_client`
- `_get_coords_nominatim()` function
- `_tavily_photo()` function
- `search_by_name()` function
- `_fetch_and_cache()` function (replace with new logic)
- `_counters` dict (unused)

### Update `_scrape_object_page(url)`:
- Keep: OG image scraping, /onmap coords scraping, address regex
- Return: `{"image": str, "lat": float|None, "lon": float|None, "address": str}`
- Parse coords string into `(lat, lon)` floats inside this function
- If /onmap returns no coords → lat=None, lon=None (do NOT fall back to Nominatim)

### Add `_parse_coords(coords_str: str) -> tuple[float, float] | None`:
- Parse "55.1234, 37.5678" → (55.1234, 37.5678)
- Return None if format invalid or empty

### Update `_fetch_from_web(city: str, pool) -> list[dict]`:
- Accept pool parameter for dedup queries
- After scraping each object: check location filter (lat/lon OR address non-empty)
- If no location → skip, log reason
- Check dedup: query DB for objects within 0.001° of lat/lon (if lat/lon available)
- If duplicate found → skip, log reason
- Return list of objects in new format (lat/lon as floats, not string)

### Add `_is_duplicate(pool, city, lat, lon) -> bool`:
```python
# Check if any object exists within ~100m (0.001° ≈ 90-110m at Moscow latitude)
async def _is_duplicate(pool, city: str, lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False  # can't check without coords
    row = await pool.fetchrow(
        """SELECT id FROM objects WHERE city = $1
           AND lat BETWEEN $2 - 0.001 AND $2 + 0.001
           AND lon BETWEEN $3 - 0.001 AND $3 + 0.001
           LIMIT 1""",
        city, lat, lon
    )
    return row is not None
```

### Update `search_objects(pool, obj_type, city, shown_ids)`:
- Accept pool as first parameter
- Check `get_city_last_fetched(pool, city)` — if None or > 7 days → fetch
- If fetch returns 0 objects AND DB is also empty → return []
- If urban3p.ru fails → log warning, fall back to DB (return whatever is cached)
- After successful fetch: `update_city_fetched(pool, city)`
- Call `get_objects(pool, city, shown_ids, limit=3)`

### Error handling (new standard — NOT bare except):
```python
# Replace all `except Exception: pass` with:
except httpx.TimeoutException as e:
    logger.warning(f"Timeout scraping {url}: {e}")
except httpx.ConnectError as e:
    logger.warning(f"Connection error: {e}")
except Exception as e:
    logger.error(f"Unexpected error in _scrape_object_page: {e}")
```

## TDD Anchor

```bash
# No groq import
python -c "import search; print('OK')" 2>&1 | grep -v groq

# search_by_name no longer exists
python -c "from search import search_by_name" 2>&1
# Expected: ImportError

# _get_coords_nominatim removed
python -c "from search import _get_coords_nominatim" 2>&1
# Expected: ImportError
```

## Acceptance Criteria

- [ ] `import search` succeeds without groq import
- [ ] `search_by_name` does not exist in search.py
- [ ] `_get_coords_nominatim` does not exist in search.py
- [ ] `_tavily_photo` does not exist in search.py
- [ ] `search_objects(pool, ...)` accepts pool as first arg
- [ ] Objects without lat/lon AND address are not saved to DB
- [ ] No `except Exception: pass` — all exceptions are logged
- [ ] Fallback to DB if urban3p.ru is unreachable

## Context Files

- `search.py` — current implementation to refactor
- `database.py` (new, from Task 2) — `get_objects`, `save_objects`, `get_city_last_fetched`, `update_city_fetched`, `_is_duplicate` (add if not there)
- `work/urbex-rewrite/tech-spec.md` — Data Flow section

## Verify-smoke

```bash
python -c "from search import search_objects; print('OK')"
```

## Post-completion

Update status to `completed`.
