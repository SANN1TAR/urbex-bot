---
status: completed
wave: 3
depends_on: [2-database, 3-search]
skills: [code-writing]
reviewers: [code-reviewer]
---

# Task 4 — Refactor bot.py: remove search feature, fix all bugs, update keyboard

## Description

Major refactor of `bot.py`. Remove "🔍 Поиск по названию" feature entirely.
Fix all critical bugs from audit. Update keyboard. Wire up asyncpg pool from Config.
Fix _shown_global memory leak. Fix FSM state not cleared on error. Fix race condition
on rapid "Next" button. Fix asyncio.create_task exception handling.

## What to do

### Remove entirely:
- `Search` FSM state class
- `handle_search_prompt()` handler
- `handle_search_query()` handler
- "🔍 Поиск по названию" button from `MAIN_KB`
- `MENU_BUTTONS` entry for search (or remove `MENU_BUTTONS` set entirely if unused)
- `from search import search_by_name, _fetch_and_cache, _counters` — remove these imports

### Update imports:
```python
from config import get_config
from database import get_user, init_db, save_user, get_objects
from search import search_objects
```

### Update `main()`:
```python
async def main():
    cfg = get_config()
    pool = await asyncpg.create_pool(cfg.database_url, min_size=2, max_size=10)
    await init_db(pool)
    bot = Bot(token=cfg.telegram_token)
    dp = Dispatcher(storage=MemoryStorage())
    # store pool in bot data for access in handlers
    dp["pool"] = pool
    task = asyncio.create_task(_refresh_cache_loop(pool))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await pool.close()
```

### Pass pool to handlers via dp["pool"]:
All handlers that need DB access: `pool = dp["pool"]` — actually in aiogram 3, use
`data: dict` or store in `FSMContext`. Best approach: store pool as bot attribute or
use `bot.get_current()`. Use aiogram 3 middleware pattern or pass via `dp["pool"]`.

Actually simplest in aiogram 3: access via `message.bot` doesn't work for pool.
Use module-level pool variable (set in `main()`):
```python
_pool: asyncpg.Pool | None = None  # set in main()
```

### Fix: FSM state not cleared on error (bug audit 7.4)
```python
# In start_browsing() and handle_next():
if not objects:
    await state.clear()  # ADD THIS
    await message.answer("Попробуй позже или смени город.", reply_markup=MAIN_KB)
    return
```

### Fix: race condition in _shown_global (bug audit 1.4)
Replace in-memory set tracking with DB-based tracking using object IDs (integers):
```python
_shown_global: dict[int, set[int]] = {}  # uid → set of object IDs (not names)
```
Use object `id` (INTEGER from DB) instead of `name` (string) as shown key.
This is deduplication-safe and avoids the name-collision issue.

### Fix: _shown_global memory leak (bug audit 3.1)
Add TTL cleanup — remove entries older than 24 hours:
```python
_shown_timestamps: dict[int, float] = {}  # uid → last_active timestamp

def _cleanup_shown():
    cutoff = time.time() - 86400  # 24 hours
    stale = [uid for uid, ts in _shown_timestamps.items() if ts < cutoff]
    for uid in stale:
        _shown_global.pop(uid, None)
        _shown_timestamps.pop(uid, None)
```
Call `_cleanup_shown()` at start of each "Заброшка" handler.

### Fix: asyncio.create_task exception handling (bug audit 3.2, 3.3)
```python
async def _refresh_cache_loop(pool):
    while True:
        try:
            await asyncio.sleep(24 * 3600)
            # ... refresh logic
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache refresh error: {e}")
            # don't re-raise, keep loop running
```

### Update MAIN_KB (remove search button):
```python
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏚️ Заброшка")],
        [KeyboardButton(text="🏙️ Сменить город")],
    ],
    resize_keyboard=True,
)
```

### Update _format_obj() — use lat/lon from DB:
```python
def _format_obj(obj: dict) -> str:
    if obj.get("lat") and obj.get("lon"):
        location = f"\n🗺 {obj['lat']:.4f}, {obj['lon']:.4f}"
    elif obj.get("address"):
        location = f"\n📍 {obj['address']}"
    else:
        location = ""
    return f"<b>{obj.get('name', 'Без названия')}</b>{location}"
```

### Update /help text:
Remove mention of "🔍 Поиск по названию" and "Поиск по названию" command.

### Update _counters import:
Remove `_counters` from search imports and from `cmd_restart`.

## TDD Anchor

```bash
# Bot starts without crash
timeout 5 python bot.py ; echo "exit: $?"
# Expected: exit 0 or 1 (polling stopped), NOT exception traceback
```

## Acceptance Criteria

- [ ] `MAIN_KB` has only 2 rows: "🏚️ Заброшка" and "🏙️ Сменить город"
- [ ] No `Search` class in bot.py
- [ ] No `search_by_name` import
- [ ] Pool initialized in `main()` and accessible in handlers
- [ ] `state.clear()` called before every error message in browsing flow
- [ ] `_shown_global` uses object IDs (int) not names (str)
- [ ] `_refresh_cache_loop` catches exceptions and logs them (doesn't die silently)
- [ ] `/help` text doesn't mention search by name
- [ ] `asyncio` imported at top of file (not in `__main__`)
- [ ] `import time` added for TTL cleanup

## Context Files

- `bot.py` — current implementation to refactor
- `database.py` (new, Task 2) — get_user, save_user, get_objects signatures
- `search.py` (new, Task 3) — search_objects(pool, ...) signature
- `config.py` (new, Task 1) — get_config()

## Verify-smoke

```bash
python -c "import bot; print('import OK')"
```

## Post-completion

Update status to `completed`.
