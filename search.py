# Поиск объектов через DuckDuckGo + обработка результатов через Gemini
# Получает: тип объекта (заброшка/крыша/бомбарь) и город
# Отдаёт: список из 3 объектов с названием, адресом, описанием, источником

import asyncio
import json
import os

import google.generativeai as genai
from duckduckgo_search import DDGS
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

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


def _ddg_text(query: str, max_results: int) -> list:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results))


def _ddg_images(query: str, max_results: int) -> list:
    with DDGS() as ddgs:
        return list(ddgs.images(query, max_results=max_results))


async def search_objects(obj_type: str, city: str) -> list:
    query = f"{SEARCH_QUERIES[obj_type]} {city}"
    results = await asyncio.to_thread(_ddg_text, query, 10)

    if not results:
        return []

    results_text = "\n".join(
        [f"- {r['title']}: {r['body']} (URL: {r['href']})" for r in results]
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

    response = model.generate_content(prompt)
    text = response.text.strip()

    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]

    try:
        return json.loads(text.strip())
    except Exception:
        return []


async def search_images(name: str, city: str) -> list:
    query = f"{name} {city} заброшка фото"
    results = await asyncio.to_thread(_ddg_images, query, 4)

    photos = []
    for r in results[:2]:
        url = r.get("image", "")
        source = r.get("url", "")
        if url:
            photos.append({"url": url, "source": source})
    return photos


async def search_by_name(name: str, city: str) -> dict:
    query = f"{name} {city} заброшка крыша бомбарь"
    results = await asyncio.to_thread(_ddg_text, query, 8)

    if not results:
        return {"not_found": True}

    results_text = "\n".join(
        [f"- {r['title']}: {r['body']} (URL: {r['href']})" for r in results]
    )

    prompt = f"""Найди информацию об объекте с названием "{name}" в городе {city}.

Результаты поиска:
{results_text}

Ответь строго в формате JSON без лишнего текста:
{{"name": "...", "address": "...", "description": "...", "source": "..."}}

Если объект не найден или информации нет, ответь:
{{"not_found": true}}
"""

    response = model.generate_content(prompt)
    text = response.text.strip()

    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]

    try:
        return json.loads(text.strip())
    except Exception:
        return {"not_found": True}
