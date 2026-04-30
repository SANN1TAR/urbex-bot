# Поиск объектов через Tavily + обработка результатов через Groq
# Получает: тип объекта (zabroshka/roof) и город
# Отдаёт: список из 3 объектов с названием, адресом, описанием, источником

import asyncio
import json
import logging
import os

from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

TYPE_NAMES = {
    "zabroshka": "заброшка",
    "roof": "крыша",
}

QUERY_VARIANTS = {
    "zabroshka": [
        "заброшка адрес vk.com telegram",
        "заброшенное здание адрес вконтакте телеграм",
        "urbex заброшка координаты вход site:vk.com",
    ],
    "roof": [
        "крыша руф адрес vk.com telegram",
        "руфинг точка адрес высотка вконтакте телеграм",
        "крышелазание здание адрес site:vk.com",
    ],
}

_query_counters: dict = {}


def _parse_json(text: str):
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _format_results(results: list) -> str:
    return "\n".join(f"- {r['title']}: {r['content']} (URL: {r['url']})" for r in results)


def _tavily_search(query: str) -> dict:
    return tavily.search(query, max_results=10)


def _groq_ask(prompt: str) -> str:
    return groq.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


async def search_objects(obj_type: str, city: str, shown: list | None = None) -> list:
    shown = shown or []

    key = f"{obj_type}_{city}"
    counter = _query_counters.get(key, 0)
    query_base = QUERY_VARIANTS[obj_type][counter % len(QUERY_VARIANTS[obj_type])]
    _query_counters[key] = counter + 1

    response = await asyncio.to_thread(_tavily_search, f"{query_base} {city}")
    results = response.get("results", [])

    if not results:
        return []

    # Сортировка: свежие результаты первыми
    results.sort(key=lambda r: r.get("published_date") or "", reverse=True)

    exclude = f"Уже показанные объекты (не повторяй их): {', '.join(shown)}.\n" if shown else ""

    prompt = f"""Из результатов поиска выдели 3 реальных РАЗНЫХ конкретных объекта типа "{TYPE_NAMES[obj_type]}" в городе {city}.
{exclude}
Для каждого объекта:
- name: название объекта или здания
- address: ТОЧНЫЙ адрес — улица, номер дома. Если нет точного — район и ориентир
- description: что это за место, какой вид/атмосфера, особенности
- security: охрана, замки, камеры, сложность попадания. Если инфы нет — не включай поле
- source: URL откуда взята инфа

Результаты:
{_format_results(results)}

Ответь строго JSON массивом без лишнего текста:
[{{"name":"...","address":"...","description":"...","security":"...","source":"..."}}]

Приоритет источников: сначала посты из ВКонтакте (vk.com) и Telegram (t.me), потом остальные.
Нужны конкретные точки с адресами, не общие статьи. Если объектов меньше 3 — дай сколько есть. Если нет — верни [].
"""

    text = await asyncio.to_thread(_groq_ask, prompt)

    try:
        objects = _parse_json(text)
        for i, obj in enumerate(objects):
            if i < len(results):
                obj["published_date"] = results[i].get("published_date", "")
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    response = await asyncio.to_thread(_tavily_search, f"{name} {city} заброшка крыша")
    results = response.get("results", [])

    if not results:
        return {"not_found": True}

    prompt = f"""Найди информацию об объекте "{name}" в городе {city}.

Результаты:
{_format_results(results)}

Ответь строго JSON без лишнего текста:
{{"name":"...","address":"...","description":"...","source":"..."}}

Если не найдено — верни: {{"not_found":true}}
"""

    text = await asyncio.to_thread(_groq_ask, prompt)

    try:
        return _parse_json(text)
    except Exception:
        return {"not_found": True}
