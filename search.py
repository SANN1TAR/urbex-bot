# Поиск объектов через Tavily + обработка результатов через Groq
# Получает: тип объекта (zabroshka/roof) и город
# Отдаёт: список из 3 объектов с названием, адресом, описанием, фото

import asyncio
import json
import logging
import os

import httpx
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
        "заброшка урбекс адрес охрана залаз сохранность wikimapia",
        "заброшенное здание urbex как попасть охрана координаты",
        "заброшка вход охрана описание место urbex",
    ],
    "roof": [
        "руф точка многоэтажка открытая крыша залаз вид",
        "руфинг многоэтажка адрес как залезть на крышу",
        "крышелазание точка жилой дом открытая крыша вид город",
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


async def _wikimedia_image(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "list": "search", "srsearch": query,
                "srnamespace": "6", "format": "json", "srlimit": 3,
            })
            hits = r.json().get("query", {}).get("search", [])
            if not hits:
                return ""
            title = hits[0]["title"]
            r2 = await client.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "titles": title, "prop": "imageinfo",
                "iiprop": "url", "format": "json",
            })
            pages = r2.json().get("query", {}).get("pages", {})
            for page in pages.values():
                info = page.get("imageinfo", [])
                if info:
                    return info[0].get("url", "")
    except Exception:
        pass
    return ""


async def _flickr_image(query: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://www.flickr.com/search/", params={
                "text": query, "license": "2,3,4,5,6,9", "media": "photos",
                "safe_search": "1", "view_all": "1",
            }, headers={"User-Agent": "Mozilla/5.0"})
            import re
            urls = re.findall(r'https://live\.staticflickr\.com/[^"\']+\.jpg', r.text)
            return urls[0] if urls else ""
    except Exception:
        return ""


async def _get_image(name: str, obj_type: str, city: str) -> str:
    fallback = "rooftop city view" if obj_type == "roof" else "abandoned building urbex"

    # 1. Wikimedia с точным названием
    url = await _wikimedia_image(f"{name} {city}")
    if url:
        return url

    # 2. Wikimedia с общим запросом
    url = await _wikimedia_image(fallback)
    if url:
        return url

    # 3. Flickr
    url = await _flickr_image(f"{name} {fallback}")
    return url


def _groq_ask(prompt: str) -> str:
    return groq.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


def _sort_results(results: list) -> list:
    def key(r):
        url = r.get("url", "")
        priority = 0 if "wikimapia" in url else 1
        date = r.get("published_date") or ""
        return (priority, date)
    return sorted(results, key=key)


async def search_objects(obj_type: str, city: str, shown: list | None = None) -> list:
    shown = shown or []

    key = f"{obj_type}_{city}"
    counter = _query_counters.get(key, 0)
    query_base = QUERY_VARIANTS[obj_type][counter % len(QUERY_VARIANTS[obj_type])]
    _query_counters[key] = counter + 1

    response = await asyncio.to_thread(_tavily_search, f"{query_base} {city}")
    results = _sort_results(response.get("results", []))
    images = response.get("images", [])

    if not results:
        return []

    exclude = f"Уже показанные объекты (не повторяй их): {', '.join(shown)}.\n" if shown else ""

    if obj_type == "roof":
        task = f"""Найди 3 реальных конкретных точки для РУФИНГА (залезть на крышу многоэтажки) в городе {city}.
{exclude}
Руфинг — это когда залезают на крышу жилого или нежилого высотного здания, фотографируются, смотрят вид.
НЕ нужны: аренда крыш, банкеты, рестораны на крыше, ремонт кровли.
НУЖНЫ: конкретные многоэтажки или высотки, куда можно залезть на крышу.

Для каждой точки:
- name: название здания или адрес как называют в тусовке
- address: ТОЧНЫЙ адрес — улица, номер дома, район
- coords: координаты в формате "55.7558, 37.6173" если есть в тексте, иначе не включай поле
- description: вид с крыши, как выглядит, высота, особенности
- security: есть ли охрана, замок на чердаке, консьерж, сложность залаза. Если нет инфы — не включай поле"""
    else:
        task = f"""Найди 3 реальных конкретных ЗАБРОШЕННЫХ объекта в городе {city}.
{exclude}
Нужны реально заброшенные здания/территории — не работающие объекты.

Для каждого объекта:
- name: название заброшки
- address: ТОЧНЫЙ адрес — улица, номер дома, район, ориентир
- coords: координаты в формате "55.7558, 37.6173" если есть в тексте, иначе не включай поле
- description: что это за место, в каком состоянии, что внутри, атмосфера
- security: охрана, камеры, сложность попадания, как залезть. Если нет инфы — не включай поле"""

    prompt = f"""{task}

Результаты:
{_format_results(results)}

Ответь строго JSON массивом без лишнего текста:
[{{"name":"...","address":"...","coords":"...","description":"...","security":"..."}}]

ВАЖНО: в description и security — никаких ссылок, названий сайтов, упоминаний YouTube, ВКонтакте или других ресурсов. Только сухая инфа об объекте.
Если объектов меньше 3 — дай сколько есть. Если нет — верни [].
"""

    text = await asyncio.to_thread(_groq_ask, prompt)

    try:
        objects = _parse_json(text)
        for i, obj in enumerate(objects):
            if i < len(results):
                obj["published_date"] = results[i].get("published_date", "")
            name = obj.get("name", "")
            obj["image"] = await _get_image(name, obj_type, city)
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    response = await asyncio.to_thread(_tavily_search, f"{name} {city} заброшка крыша wikimapia")
    results = _sort_results(response.get("results", []))
    images = response.get("images", [])

    if not results:
        return {"not_found": True}

    prompt = f"""Найди информацию об объекте "{name}" в городе {city}.

Результаты:
{_format_results(results)}

Ответь строго JSON без лишнего текста:
{{"name":"...","address":"...","description":"..."}}

Если не найдено — верни: {{"not_found":true}}
"""

    text = await asyncio.to_thread(_groq_ask, prompt)

    try:
        result = _parse_json(text)
        if images and not result.get("not_found"):
            result["image"] = images[0]
        return result
    except Exception:
        return {"not_found": True}
