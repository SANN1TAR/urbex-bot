# Поиск объектов через Tavily + обработка результатов через Gemini
# Получает: тип объекта (заброшка/крыша/бомбарь) и город
# Отдаёт: список из 3 объектов с названием, адресом, описанием, источником

import asyncio
import json
import os

import google.generativeai as genai
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-2.0-flash")
tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

SEARCH_QUERIES = {
    "zabroshka": "заброшенное здание заброшка урбекс",
    "roof": "руфинг крыша крышелазание высотка",
    "digger": "бомбоубежище бомбарь подземелье дигеры",
}

TYPE_NAMES = {
    "zabroshka": "заброшка",
    "roof": "крыша",
    "digger": "бомбарь",
}


def _tavily_search(query: str) -> dict:
    return tavily.search(query, max_results=8, include_images=True)


async def search_objects(obj_type: str, city: str) -> list:
    query = f"{SEARCH_QUERIES[obj_type]} {city}"
    response = await asyncio.to_thread(_tavily_search, query)

    results = response.get("results", [])
    images = response.get("images", [])

    if not results:
        return []

    results_text = "\n".join(
        [f"- {r['title']}: {r['content']} (URL: {r['url']})" for r in results]
    )
    type_name = TYPE_NAMES[obj_type]

    prompt = f"""Ты помощник по поиску заброшенных мест, крыш и подземелий.

Из этих результатов поиска выдели 3 реальных объекта типа "{type_name}" в городе {city}.
Для каждого объекта дай:
- name: название объекта
- address: адрес или район/описание местонахождения
- description: 2-3 предложения что это, как выглядит, особенности
- source: URL откуда взята информация

Результаты поиска:
{results_text}

Ответь строго в формате JSON массива, без лишнего текста:
[
  {{"name": "...", "address": "...", "description": "...", "source": "..."}}
]

Если реальных объектов меньше 3 — дай сколько есть. Если совсем ничего нет — верни пустой массив [].
"""

    response_ai = model.generate_content(prompt)
    text = response_ai.text.strip()

    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]

    try:
        objects = json.loads(text.strip())
        for i, obj in enumerate(objects):
            if i < len(images):
                obj["image"] = images[i]
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    query = f"{name} {city} заброшка крыша бомбарь"
    response = await asyncio.to_thread(_tavily_search, query)

    results = response.get("results", [])
    images = response.get("images", [])

    if not results:
        return {"not_found": True}

    results_text = "\n".join(
        [f"- {r['title']}: {r['content']} (URL: {r['url']})" for r in results]
    )

    prompt = f"""Найди информацию об объекте с названием "{name}" в городе {city}.

Результаты поиска:
{results_text}

Ответь строго в формате JSON без лишнего текста:
{{"name": "...", "address": "...", "description": "...", "source": "..."}}

Если объект не найден или информации нет, ответь:
{{"not_found": true}}
"""

    response_ai = model.generate_content(prompt)
    text = response_ai.text.strip()

    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]

    try:
        result = json.loads(text.strip())
        if images and not result.get("not_found"):
            result["images"] = images[:2]
        return result
    except Exception:
        return {"not_found": True}
