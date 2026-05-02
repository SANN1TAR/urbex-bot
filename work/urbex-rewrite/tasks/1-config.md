---
status: completed
wave: 1
depends_on: []
skills: [code-writing]
reviewers: [code-reviewer, security-auditor]
---

# Task 1 — Create config.py with env validation

## Description

Create a new `config.py` module that validates all required environment variables
at startup and exposes a typed `Config` dataclass. Bot must fail fast with a clear
human-readable error if any required env var is missing.

## What to do

1. Create `config.py` in project root
2. Load env vars with `python-dotenv`
3. Validate all required vars exist and are non-empty
4. If any missing → print clear error with var name → `sys.exit(1)`
5. Expose a `Config` dataclass with all settings as typed fields
6. Replace all `os.getenv(...)` calls in bot.py and search.py with `config.*`

## TDD Anchor

No automated test framework. Manual smoke test:
```bash
# With all vars present — should not raise
python -c "from config import get_config; c = get_config(); print('OK', c.telegram_token[:5])"

# With missing var — should print error and exit 1
TELEGRAM_TOKEN="" python -c "from config import get_config; get_config()" ; echo "exit: $?"
```

## Acceptance Criteria

- [ ] `config.py` exists and is importable
- [ ] `get_config()` raises `SystemExit` with message if any required var missing
- [ ] Required vars: `TELEGRAM_TOKEN`, `TAVILY_API_KEY`, `DATABASE_URL`
- [ ] `GROQ_API_KEY` is NOT required (Groq removed)
- [ ] `Config` dataclass has: `telegram_token`, `tavily_api_key`, `database_url`
- [ ] No hardcoded secrets anywhere

## Context Files

- `bot.py` — currently uses `os.getenv("TELEGRAM_TOKEN")` at line 25
- `search.py` — currently uses `os.getenv("TAVILY_API_KEY")` at line 21
- `.env` — shows current env var names (TELEGRAM_TOKEN, GROQ_API_KEY, TAVILY_API_KEY)

## Verify-smoke

```bash
python -c "from config import get_config; print('config OK')"
```

## Verify-user

Run bot.py locally without .env — should print clear error about missing DATABASE_URL.

## Post-completion

Update status to `completed`. Note in decisions.md that config.py was created.
