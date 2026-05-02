# UX Guidelines — Bot Messages

## Tone of Voice

Casual, direct, street-smart. The bot speaks like a knowledgeable local guide —
not formal, not corporate. Slang is OK. Short sentences.

Examples from current codebase:
- "О, вернулся. Город {city} — поехали, чё надо?"
- "Здорово, ёпта. Я тут из рода экскурсоводов — знаю почти все дыры в городе."
- "Перезагрузил. Всё с нуля — поехали."
- "Ща пробью заброшки в {city}... 🔍"

## Emoji Usage

Functional emojis only — one per context, not decorative:
- 🏚️ — abandoned objects (button label)
- 🔍 — search
- 🏙️ — city
- ➡️ — next
- 🔄 — restart/reset
- 🗺 — map coordinates
- 📍 — address (when no coords)
- 🔒 — security/guard info
- ⚠️ — warning/disclaimer

## Object Card Format

```
<b>Название объекта</b>
🗺 55.1234, 37.5678       ← if coords available
📍 ул. Примерная, 1       ← if only address (no coords)
🔒 Охрана есть           ← only if security info is non-empty and not "неизвестно"
```

No description text in card — removed in v0.5 (too noisy, often wrong).
Followed by `STALE_NOTE`: "⚡ Не серчай, инфа может быть устаревшей. Перед вылазкой перепроверяй."

## Error Messages

Current (generic):
- "Попробуй позже или смени город." — shown for any failure

Should be specific per failure type (known improvement needed):
- API down → "Сервис временно недоступен, попробуй через пару минут"
- City has no objects → "В {city} пока пусто. Попробуй другой город."
- Rate limit → "Слишком много запросов, подожди минуту"

## Disclaimer (shown once at /start for new users)

Legal disclaimer about responsibility — user takes all risks. Shown via `DISCLAIMER` constant.
Not shown again after registration.

## VPN Note

`VPN_NOTE` — shown after city registration. Warns that Instagram/YouTube links may need VPN.
One-time per registration.

## Message Parse Mode

All bot messages use `parse_mode="HTML"` — bold via `<b>`, no Markdown.
