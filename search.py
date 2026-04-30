# Поиск объектов через Tavily + Groq
# Получает: тип объекта (zabroshka/roof), город, список уже показанных
# Отдаёт: список объектов с названием, координатами/адресом, описанием, фото

import asyncio
import json
import logging
import os
import re

import httpx
from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

TYPE_NAMES = {"zabroshka": "заброшка", "roof": "крыша для руфинга"}

QUERIES = {
    "zabroshka": [
        "заброшка адрес координаты охрана wikimapia",
        "urbex заброшенное здание вход координаты",
        "заброшка урбекс описание место",
    ],
    "roof": [
        "руф многоэтажка крыша залаз координаты",
        "руфинг точка высотка адрес как попасть",
        "крышелазание жилой дом открытая крыша",
    ],
}

_counters: dict = {}


def _parse_json(text: str):
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _clean(text: str) -> str:
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\b\w+\.(ru|com|org|net|io)\b', '', text)
    return re.sub(r'\s{2,}', ' ', text).strip()


def _sort_by_date(results: list) -> list:
    def key(r):
        priority = 0 if "wikimapia" in r.get("url", "") else 1
        return (priority, r.get("published_date") or "")
    return sorted(results, key=key)


def _tavily(query: str, images: bool = False) -> dict:
    return tavily_client.search(query, max_results=10, include_images=images)


def _groq(prompt: str) -> str:
    return groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


async def _get_photo(name: str, city: str) -> str:
    # 1. Tavily с картинками
    try:
        r = await asyncio.to_thread(_tavily, f"{name} {city} фото", True)
        imgs = r.get("images", [])
        if imgs:
            return imgs[0]
    except Exception:
        pass

    # 2. Wikimedia Commons
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "list": "search",
                "srsearch": f"{name} abandoned", "srnamespace": "6",
                "format": "json", "srlimit": 1,
            })
            hits = r.json().get("query", {}).get("search", [])
            if hits:
                r2 = await client.get("https://commons.wikimedia.org/w/api.php", params={
                    "action": "query", "titles": hits[0]["title"],
                    "prop": "imageinfo", "iiprop": "url", "format": "json",
                })
                for page in r2.json().get("query", {}).get("pages", {}).values():
                    info = page.get("imageinfo", [])
                    if info:
                        return info[0].get("url", "")
    except Exception:
        pass

    return ""


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    key = f"{obj_type}_{city}"
    counter = _counters.get(key, 0)
    query_base = QUERIES[obj_type][counter % len(QUERIES[obj_type])]
    _counters[key] = counter + 1

    response = await asyncio.to_thread(_tavily, f"{query_base} {city}")
    results = _sort_by_date(response.get("results", []))

    if not results:
        return []

    exclude = f"Уже показанные объекты — НЕ включай их: {', '.join(shown)}.\n" if shown else ""

    if obj_type == "roof":
        task = f"""Найди 3 реальных точки для РУФИНГА (залезть на крышу многоэтажки) в городе {city}.
{exclude}НЕ нужны: аренда крыш, рестораны, банкеты. НУЖНЫ: конкретные высотки куда залезают."""
    else:
        task = f"""Найди 3 реальных ЗАБРОШЕННЫХ объекта в городе {city}.
{exclude}Нужны реально заброшенные здания — не работающие объекты."""

    results_text = "\n".join(f"- {r['title']}: {r['content']} (URL: {r['url']})" for r in results)

    prompt = f"""{task}

Для каждого объекта:
- name: название
- coords: координаты "55.7558, 37.6173" — ищи в тексте, если нет — попробуй вспомнить по названию места. Если совсем никак — не включай
- address: улица и дом или район — только если нет coords. Если знаешь только город — не включай
- description: состояние, атмосфера, особенности (без ссылок и названий сайтов)
- security: охрана/залаз — если есть инфа. Если нет — не включай

Результаты поиска:
{results_text}

Ответь строго JSON массивом:
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]

Если объектов меньше 3 — дай сколько есть. Если нет — верни [].
"""

    text = await asyncio.to_thread(_groq, prompt)

    try:
        objects = _parse_json(text)
        for i, obj in enumerate(objects):
            if i < len(results):
                obj["published_date"] = results[i].get("published_date", "")
            obj["description"] = _clean(obj.get("description", ""))
            obj["security"] = _clean(obj.get("security", ""))
            obj["image"] = await _get_photo(obj.get("name", ""), city)
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    response = await asyncio.to_thread(_tavily, f"{name} {city} заброшка крыша wikimapia", True)
    results = _sort_by_date(response.get("results", []))
    images = response.get("images", [])

    if not results:
        return {"not_found": True}

    results_text = "\n".join(f"- {r['title']}: {r['content']} (URL: {r['url']})" for r in results)

    prompt = f"""Найди информацию об объекте "{name}" в городе {city}.

Результаты:
{results_text}

Ответь строго JSON:
{{"name":"...","coords":"...","address":"...","description":"..."}}

Если не найдено — верни: {{"not_found":true}}
"""

    text = await asyncio.to_thread(_groq, prompt)

    try:
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["image"] = images[0] if images else await _get_photo(name, city)
        return result
    except Exception:
        return {"not_found": True}
