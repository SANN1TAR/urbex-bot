# Поиск объектов по иерархии источников: Викимапия → Урбантрип → Telegram → VK → общий
# Получает: тип объекта (zabroshka/roof), город, список уже показанных
# Отдаёт: список объектов с координатами/адресом, описанием, фото

import asyncio
import json
import logging
import os
import re

from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

TYPE_NAMES = {"zabroshka": "заброшка", "roof": "крыша для руфинга"}
NO_LIST = "Нельзя: МГУ, Кремль, телебашни, Москва-Сити, ВДНХ, Останкино, госучреждения."

# Иерархия источников — от приоритетного к общему
SOURCES = [
    "site:wikimapia.org",
    "site:urbantrip.ru",
    "site:t.me OR site:telegram.me",
    "site:vk.com",
    "site:instagram.com OR site:youtube.com",
    "",  # общий поиск как последний резерв
]

BASE_QUERIES = {
    "zabroshka": "заброшка здание адрес координаты урбекс",
    "roof": "руф крыша высотка залаз адрес",
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


def _tavily(query: str, images: bool = False) -> dict:
    return tavily_client.search(query, max_results=8, include_images=images)


def _groq(prompt: str) -> str:
    return groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


def _fmt(results: list) -> str:
    return "\n".join(f"- {r['title']}: {r['content']}" for r in results)


async def _get_photo(name: str, city: str, obj_type: str) -> str:
    suffix = "здание фасад заброшка" if obj_type == "zabroshka" else "высотка здание крыша"
    try:
        r = await asyncio.to_thread(_tavily, f"{name} {city} {suffix}", True)
        imgs = r.get("images", [])
        if imgs:
            return imgs[0]
    except Exception:
        pass
    return ""


async def _search_source(query: str, source: str) -> list:
    full_query = f"{query} {source}".strip()
    try:
        r = await asyncio.to_thread(_tavily, full_query)
        results = r.get("results", [])
        logger.info(f"Источник '{source or 'общий'}': {len(results)} результатов")
        return results
    except Exception:
        return []


def _build_prompt(obj_type: str, city: str, results: list, shown: set) -> str:
    if obj_type == "roof":
        task = f"Найди 3 точки для руфинга (крыша жилой/нежилой высотки) в {city}. {NO_LIST} Не аренда, не рестораны."
    else:
        task = f"Найди 3 заброшенных здания или объекта в {city}. {NO_LIST} Малоизвестные реальные места."

    exclude_block = ""
    if shown:
        names = "\n".join(f"- {n}" for n in shown)
        exclude_block = f"\n\nСТРОГО НЕ ПОВТОРЯТЬ:\n{names}"

    return f"""{task}{exclude_block}

Для каждого:
- name: название здания/объекта
- coords: координаты "55.7558, 37.6173" — ТОЛЬКО если есть в тексте. Не придумывай.
- address: конкретная улица с номером дома или название района. НЕ писать "центр города", "у метро X", "у Москвы-Сити" и подобное. Только если нет coords и адрес конкретный.
- description: состояние, атмосфера, особенности. Без ссылок и сайтов.
- security: охрана, залаз — только если есть инфа

Данные:
{_fmt(results)}

JSON массив:
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]

Меньше 3 — дай сколько есть. Нет ничего — верни [].
"""


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    base_query = f"{BASE_QUERIES[obj_type]} {city}"

    # Идём по иерархии источников
    for source in SOURCES:
        results = await _search_source(base_query, source)
        if not results:
            continue

        prompt = _build_prompt(obj_type, city, results, shown)
        try:
            text = await asyncio.to_thread(_groq, prompt)
            logger.info(f"Groq ({source or 'общий'}): {text[:100]}")
            objects = _parse_json(text)
        except Exception:
            objects = []

        if objects:
            for i, obj in enumerate(objects):
                obj["published_date"] = results[i].get("published_date", "") if i < len(results) else ""
                obj["description"] = _clean(obj.get("description", ""))
                obj["security"] = _clean(obj.get("security", ""))
                obj["image"] = await _get_photo(obj.get("name", ""), city, obj_type)
            return objects

    # Если вообще всё пусто — возвращаем общий поиск без ограничений
    logger.info("Все источники пусты — финальный поиск без shown")
    results = await _search_source(base_query, "")
    if not results:
        return []

    prompt = _build_prompt(obj_type, city, results, set())
    try:
        text = await asyncio.to_thread(_groq, prompt)
        objects = _parse_json(text)
    except Exception:
        return []

    for i, obj in enumerate(objects):
        obj["published_date"] = results[i].get("published_date", "") if i < len(results) else ""
        obj["description"] = _clean(obj.get("description", ""))
        obj["security"] = _clean(obj.get("security", ""))
        obj["image"] = await _get_photo(obj.get("name", ""), city, obj_type)
    return objects


async def search_by_name(name: str, city: str) -> dict:
    response = await asyncio.to_thread(_tavily, f"{name} {city} заброшка крыша", True)
    results = response.get("results", [])
    images = response.get("images", [])

    if not results:
        return {"not_found": True}

    prompt = f"""Найди инфу об объекте "{name}" в {city}.

Данные:
{_fmt(results)}

JSON:
{{"name":"...","coords":"...","address":"...","description":"..."}}

Не найдено — верни: {{"not_found":true}}
"""

    try:
        text = await asyncio.to_thread(_groq, prompt)
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["image"] = images[0] if images else await _get_photo(name, city, "zabroshka")
        return result
    except Exception:
        return {"not_found": True}
