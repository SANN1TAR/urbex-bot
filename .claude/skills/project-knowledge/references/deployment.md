# Deployment

## Platform

**Railway** — always-on worker dyno (not web server).

## Process Type

```
worker: python bot.py   # Procfile
```

Long-polling mode (`dp.start_polling(bot)`). No HTTP server, no port binding.

## Runtime

Python 3.11 (`runtime.txt`)

## Environment Variables

Set in Railway dashboard (Settings → Variables):

| Variable | Description | Where used |
|----------|-------------|-----------|
| `TELEGRAM_TOKEN` | Bot token from @BotFather | `bot.py:25` — `Bot(token=...)` |
| `TAVILY_API_KEY` | Tavily search API key | `search.py:21` — `TavilyClient(api_key=...)` |
| `GROQ_API_KEY` | Groq LLM API key | `search.py:22` — `Groq(api_key=...)` |
| `GEMINI_API_KEY` | **UNUSED** — leftover from v0.3 experiment | Can be deleted from Railway |

**No env var validation at startup** — if any key is missing, bot crashes with cryptic error.

## Database

SQLite file `urbex_bot.db` — created automatically by `init_db()` on startup.

**Critical**: Railway uses ephemeral storage. Every new deployment wipes the DB.
All cached objects and user registrations are lost on deploy.

This is a known limitation. Workaround: users re-register, cache refills on first request.

## Deploy Process

Currently manual:
```bash
git push origin master   # push code
# Railway auto-deploys on push to master (if connected via GitHub)
```

No CI/CD pipeline, no tests run before deploy.

## Monitoring

- Railway logs: `railway logs` or Railway dashboard
- Logging level: `logging.INFO` — bot startup, cache updates, scraping results logged
- No error tracking (no Sentry, no alerting)
- No health checks

## Rollback

Railway dashboard → Deployments → select previous → "Redeploy".

## Known Operational Issues

1. First request per city after deploy = 10-30s wait (cache cold start)
2. Tavily rate limits → bot shows "Попробуй позже" to multiple users simultaneously
3. urban3p.ru may block Railway IP (same issue as Overpass API in v0.4)
4. Background cache refresh task fails silently if Tavily is down
