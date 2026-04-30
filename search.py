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

NO_LIST = "Нельзя: МГУ, Кремль, телебашни, Москва-Сити, ВДНХ, Останкино, госучреждения."

BANNED_WORDS = {
    "москва-сити", "moscow city", "москва сити", "останкино", "останкинская",
    "кремль", "мгу", "вднх", "лужники", "газпром", "сити", "федерация",
    "эволюция", "империя", "меркурий", "нафта", "око", "башня 2000",
    "большой театр", "гум", "цум", "фсб", "фсо", "минобороны",
}


def _is_banned(obj: dict) -> bool:
    text = (obj.get("name", "") + " " + obj.get("description", "")).lower()
    return any(word in text for word in BANNED_WORDS)

SOURCES = [
    "site:wikimapia.org",
    "site:urbantrip.ru",
    "site:t.me",
    "site:vk.com",
    "",
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
    if not text:
        return ""
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\b\w+\.(ru|com|org|net|io)\b', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', text).strip()


def _clean_address(addr: str, city: str) -> str:
    if not addr:
        return ""
    bad = re.compile(
        r'^(центр\s+\w+|у\s+\w+|рядом|около|вблизи|недалеко|неподалёку|'
        r'неподалеку|неподалеку|' + re.escape(city) + r'$)',
        re.IGNORECASE
    )
    if bad.match(addr.strip()):
        return ""
    return addr


def _tavily(query: str, images: bool = False) -> dict:
    return tavily_client.search(query, max_results=8, include_images=images)


def _groq(prompt: str) -> str:
    return groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


async def _get_photo(name: str, city: str, obj_type: str) -> str:
    suffix = "здание фасад заброшка" if obj_type == "zabroshka" else "высотка крыша"
    try:
        r = await asyncio.to_thread(_tavily, f"{name} {city} {suffix}", True)
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


def _build_prompt(obj_type: str, city: str, results: list, shown: set) -> str:
    task = (
        f"Найди 3 точки для руфинга (крыша жилой/нежилой высотки) в {city}. {NO_LIST} Не аренда, не рестораны."
        if obj_type == "roof"
        else f"Найди 3 заброшенных объекта в {city}. {NO_LIST} Малоизвестные реальные заброшки."
    )
    exclude = f"\n\nСТРОГО НЕ ПОВТОРЯТЬ:\n" + "\n".join(f"- {n}" for n in shown) if shown else ""
    data = "\n".join(f"- {r['title']}: {r['content']}" for r in results)

    return f"""{task}{exclude}

Отвечай ТОЛЬКО на русском языке.

Для каждого объекта:
- name: название
- coords: координаты "55.7558, 37.6173" — ТОЛЬКО если есть в тексте ниже
- address: конкретная улица с номером или название района. Не писать просто название города.
- description: состояние, атмосфера. Без ссылок.
- security: охрана/залаз — только если есть инфа

Данные:
{data}

JSON массив:
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]

Меньше 3 — дай сколько есть. Нет ничего — верни [].
"""


def _process_obj(obj: dict, results: list, idx: int, city: str) -> dict:
    obj["published_date"] = results[idx].get("published_date", "") if idx < len(results) else ""
    obj["description"] = _clean(obj.get("description", ""))
    obj["security"] = _clean(obj.get("security", ""))
    obj["address"] = _clean_address(obj.get("address", ""), city)
    return obj


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    base = f"{BASE_QUERIES[obj_type]} {city}"

    for source in SOURCES:
        query = f"{base} {source}".strip()
        try:
            results = (await asyncio.to_thread(_tavily, query)).get("results", [])
        except Exception:
            continue

        logger.info(f"Источник '{source or 'общий'}': {len(results)} результатов")
        if not results:
            continue

        try:
            text = await asyncio.to_thread(_groq, _build_prompt(obj_type, city, results, shown))
            logger.info(f"Groq: {text[:100]}")
            objects = _parse_json(text)
        except Exception:
            continue

        objects = [o for o in objects if not _is_banned(o)]
        if objects:
            for i, obj in enumerate(objects):
                _process_obj(obj, results, i, city)
                obj["image"] = await _get_photo(obj.get("name", ""), city, obj_type)
            return objects

    # Финальный резерв — без ограничений по shown
    try:
        results = (await asyncio.to_thread(_tavily, base)).get("results", [])
        text = await asyncio.to_thread(_groq, _build_prompt(obj_type, city, results, set()))
        objects = [o for o in _parse_json(text) if not _is_banned(o)]
        for i, obj in enumerate(objects):
            _process_obj(obj, results, i, city)
            obj["image"] = await _get_photo(obj.get("name", ""), city, obj_type)
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    try:
        response = await asyncio.to_thread(_tavily, f"{name} {city} заброшка крыша", True)
        results = response.get("results", [])
        images = response.get("images", [])
    except Exception:
        return {"not_found": True}

    if not results:
        return {"not_found": True}

    data = "\n".join(f"- {r['title']}: {r['content']}" for r in results)
    prompt = f"""Найди инфу об объекте "{name}" в {city}. Отвечай только на русском.

Данные:
{data}

JSON:
{{"name":"...","coords":"...","address":"...","description":"..."}}

Не найдено — верни: {{"not_found":true}}
"""

    try:
        text = await asyncio.to_thread(_groq, prompt)
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["address"] = _clean_address(result.get("address", ""), city)
            result["image"] = images[0] if images else await _get_photo(name, city, "zabroshka")
        return result
    except Exception:
        return {"not_found": True}
