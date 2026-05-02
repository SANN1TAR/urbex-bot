# Patterns & Conventions

## Code Conventions

### Error handling
- External API calls (Tavily, Groq, httpx) wrapped in `try/except Exception: pass` — bare catch, silent failures
- This is a **known antipattern** being refactored — new code should catch specific exceptions and log them
- DB calls use aiosqlite context manager — exceptions propagate up to handler

### Async patterns
- All DB calls: `async with aiosqlite.connect(DB_PATH) as db:` — new connection per call (no pooling)
- Sync API calls (Tavily, Groq): run via `asyncio.to_thread(lambda: client.call(...))`
- HTTP calls: `async with httpx.AsyncClient(timeout=N) as client:` — new client per call

### User guard pattern
```python
# Always call _require_user() before accessing user data in handlers:
user = await _require_user(message)
if not user:
    return
# Then safe to use user["city"]
```
**Exception**: `handle_search_query` currently does NOT use `_require_user()` — known bug.

### FSM state management
- Clear state on error: `await state.clear()` before showing error message
- Clear state before starting new browsing: `await state.clear()` in handle_zabroshka
- MemoryStorage used — all FSM state lost on restart

### Object display
- Show 3 objects at a time from cache, one-by-one via inline "Next" button
- `_shown_global[uid]` tracks shown object names (in-memory, resets on restart)
- After 30 shown objects: reset to only current batch (user can see repeats)

## Filtering Rules (Business Logic)

### BANNED_WORDS
Objects are filtered if name OR content contains any banned word:
- City landmarks: москва-сити, кремль, мгу, вднх, останкино, большой театр
- Commercial: торговый центр, молл, ресторан, кафе, отель, гостиница
- Government: администрация, мэрия, правительство, министерство
- No longer abandoned: снесён, снесено, снесена
- Rural: деревня, садовое товарищество

### JUNK_RE
Articles/lists filtered via regex: "топ-10", "часть N", "лучшие заброшки", "youtube", "дзен", etc.

### URL filter (urban3p.ru)
Only `/objectNNNN` pages accepted — category pages and articles skipped.

### City relevance check
Object accepted only if city name appears in content or title.

## Git Workflow

- Single branch: `master`
- Commit messages: Russian, descriptive, no prefix conventions enforced
- No CI/CD configured — push to master = nothing happens automatically
- Deploy: manual push to Railway via git

## Testing

- No tests exist (0 test files)
- No test runner configured
- Manual testing only: run bot locally with .env, test via Telegram

## Commit Style (from history)

```
Фикс UnboundLocalError: coords инициализирован
Nominatim: чистим название от 'заброшенный' и города перед поиском
Добавлена команда /help с объяснением кнопок и проблем
```

Russian, imperative present tense, describes what changed.
