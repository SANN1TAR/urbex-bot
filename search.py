# Поиск объектов через Tavily + Groq
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

QUERIES = {
    "zabroshka": [
        "заброшка здание урбекс адрес координаты",
        "urbex заброшенное здание вход описание",
        "заброшка строение улица охрана",
    ],
    "roof": [
        "руф многоэтажка здание крыша залаз",
        "руфинг высотка здание адрес как попасть",
        "крышелазание дом здание открытая крыша",
    ],
}

NO_LIST = "Нельзя: МГУ, Кремль, телебашни, Москва-Сити, ВДНХ, Останкино, госучреждения, ФСБ объекты."

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


def _sort(results: list) -> list:
    return sorted(results, key=lambda r: (
        0 if "wikimapia" in r.get("url", "") else 1,
        r.get("published_date") or ""
    ))


def _fmt_results(results: list) -> str:
    return "\n".join(f"- {r['title']}: {r['content']}" for r in results)


def _tavily(query: str, images: bool = False) -> dict:
    return tavily_client.search(query, max_results=10, include_images=images)


def _groq(prompt: str) -> str:
    return groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
    ).choices[0].message.content


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


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    key = f"{obj_type}_{city}"
    counter = _counters.get(key, 0)
    query_base = QUERIES[obj_type][counter % len(QUERIES[obj_type])]
    _counters[key] = counter + 1

    response = await asyncio.to_thread(_tavily, f"{query_base} {city}")
    results = _sort(response.get("results", []))
    logger.info(f"Tavily: {len(results)} результатов для '{query_base} {city}'")

    if not results:
        return []

    if obj_type == "roof":
        task = f"Найди 3 точки для руфинга (крыша жилой/нежилой высотки) в {city}. {NO_LIST} Не аренда, не рестораны."
    else:
        task = f"Найди 3 заброшенных здания или объекта в {city}. {NO_LIST} Малоизвестные реальные заброшки."

    exclude_block = ""
    if shown:
        names = "\n".join(f"- {n}" for n in shown)
        exclude_block = f"\n\nСТРОГО ЗАПРЕЩЕНО включать эти объекты (уже показаны):\n{names}\nЕсли в данных только они — верни []."

    prompt = f"""{task}{exclude_block}

Для каждого:
- name: название здания/объекта
- coords: координаты "55.7558, 37.6173" — ТОЛЬКО если они есть в тексте ниже. Не придумывай.
- address: улица и дом или район (только если нет coords, только если точнее чем просто город)
- description: состояние, что внутри/снаружи, атмосфера. Без ссылок и названий сайтов.
- security: охрана, залаз, камеры (только если есть инфа)

Данные:
{_fmt_results(results)}

JSON массив без лишнего текста:
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]

Меньше 3 — дай сколько есть. Нет ничего — верни [].
"""

    text = await asyncio.to_thread(_groq, prompt)
    logger.info(f"Groq: {text[:150]}")

    try:
        objects = _parse_json(text)
        for i, obj in enumerate(objects):
            obj["published_date"] = results[i].get("published_date", "") if i < len(results) else ""
            obj["description"] = _clean(obj.get("description", ""))
            obj["security"] = _clean(obj.get("security", ""))
            obj["image"] = await _get_photo(obj.get("name", ""), city, obj_type)
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    response = await asyncio.to_thread(_tavily, f"{name} {city} заброшка крыша", True)
    results = _sort(response.get("results", []))
    images = response.get("images", [])

    if not results:
        return {"not_found": True}

    prompt = f"""Найди инфу об объекте "{name}" в {city}.

Данные:
{_fmt_results(results)}

JSON без лишнего текста:
{{"name":"...","coords":"...","address":"...","description":"..."}}

Не найдено — верни: {{"not_found":true}}
"""

    text = await asyncio.to_thread(_groq, prompt)

    try:
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["image"] = images[0] if images else await _get_photo(name, city, "zabroshka")
        return result
    except Exception:
        return {"not_found": True}
