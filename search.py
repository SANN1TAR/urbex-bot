# Поиск объектов через Tavily + обработка результатов через Groq
# Получает: тип объекта (заброшка/крыша/бомбарь) и город
# Отдаёт: список из 3 объектов с названием, адресом, описанием, источником

import asyncio
import json
import os

from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()

groq = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

SEARCH_QUERIES = {
    "zabroshka": "заброшенное здание урбекс",
    "roof": "руфинг крышелазание высотка",
    "digger": "бомбоубежище дигеры подземелье",
}

TYPE_NAMES = {
    "zabroshka": "заброшка",
    "roof": "крыша",
    "digger": "бомбарь",
}


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
    return tavily.search(query, max_results=6, include_images=True)


def _groq_ask(prompt: str) -> str:
    completion = groq.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return completion.choices[0].message.content


async def search_objects(obj_type: str, city: str) -> list:
    query = f"{SEARCH_QUERIES[obj_type]} {city}"
    response = await asyncio.to_thread(_tavily_search, query)

    results = response.get("results", [])
    images = response.get("images", [])

    if not results:
        return []

    prompt = f"""Из результатов поиска выдели 3 реальных объекта типа "{TYPE_NAMES[obj_type]}" в городе {city}.
Для каждого дай: name, address (адрес или район), description (2-3 предложения), source (URL).

Результаты:
{_format_results(results)}

Ответь строго JSON массивом без лишнего текста:
[{{"name":"...","address":"...","description":"...","source":"..."}}]

Если объектов меньше 3 — дай сколько есть. Если нет совсем — верни [].
"""

    text = await asyncio.to_thread(_groq_ask, prompt)

    try:
        objects = _parse_json(text)
        for i, obj in enumerate(objects):
            if i < len(images):
                obj["image"] = images[i]
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    query = f"{name} {city} заброшка бомбарь крыша"
    response = await asyncio.to_thread(_tavily_search, query)

    results = response.get("results", [])
    images = response.get("images", [])

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
        result = _parse_json(text)
        if images and not result.get("not_found"):
            result["images"] = images[:2]
        return result
    except Exception:
        return {"not_found": True}
