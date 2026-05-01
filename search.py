# Поиск заброшенных объектов через urban3p.ru (Tavily) + кеш в SQLite
# Получает: тип объекта (zabroshka), город, список уже показанных
# Отдаёт: список объектов с названием, описанием, фото

import asyncio
import json
import logging
import os
import random
import re

from groq import Groq
from tavily import TavilyClient
from dotenv import load_dotenv

from database import get_objects, save_objects, get_city_count, get_cache_age_days, CACHE_TTL_DAYS

load_dotenv()
logger = logging.getLogger(__name__)

tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

_counters: dict = {}

BANNED_WORDS = {
    "москва-сити", "moscow city", "москва сити", "сити", "федерация",
    "бизнес-центр", "бизнес центр", "офисный центр",
    "кремль", "мгу", "вднх", "лужники", "газпром", "фсб", "фсо",
    "останкино", "большой театр", "гум", "цум",
    "администрация", "мэрия", "правительство", "министерство",
    "торговый центр", "торговый комплекс", "молл",
    "ресторан", "кафе", "отель", "гостиница",
    "музей", "галерея", "мемориал", "стадион", "арена",
    "снесён", "снесено", "снесена",
    "деревня", "садовое товарищество",
}

SOURCES = [
    "site:urban3p.ru",
    "site:urban3p.com",
]

# Паттерны мусорных названий — статьи, топы, списки
JUNK_RE = re.compile(
    r'(\d+\s+заброшен|\bтоп[\s-]?\d+|\bч\.\s*\d+|часть\s+\d+|\||\bдзен\b|youtube|'
    r'заброшенные места|лучшие заброшки|самые|обзор|путешест)',
    re.IGNORECASE
)


def _is_banned(obj: dict) -> bool:
    text = (obj.get("name", "") + " " + obj.get("description", "")).lower()
    return any(word in text for word in BANNED_WORDS)


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r'[«»"\'.,\-–—()]', '', name)
    return re.sub(r'\s+', ' ', name)


def _is_shown(name: str, shown: set) -> bool:
    norm = _normalize_name(name)
    return any(_normalize_name(s) == norm for s in shown)


def _extract_address(text: str) -> str:
    # Ищем паттерны адресов в тексте
    patterns = [
        r'(?:ул\.|улица|пер\.|переулок|пр\.|проспект|ш\.|шоссе|бул\.|бульвар|пл\.|площадь)\s+[\w\s]+,?\s*д\.?\s*\d+[\w/]*',
        r'[\w\s]+ (?:ул\.|улица),?\s*\d+[\w/]*',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""


def _extract_name(title: str) -> str:
    # Убираем суффиксы сайтов
    name = re.sub(
        r'\s*[-|–|/]\s*(urban3p|urbantrip|urbact|заброшки|заброшенные|урбекс).*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    # Убираем "Заброшенные объекты в ..."
    name = re.sub(r'^заброшенные объекты в\s+', '', name, flags=re.IGNORECASE).strip()
    return name if len(name) > 3 else ""


def _extract_image(url: str, images: list) -> str:
    # Для urban3p.ru формируем URL превью по ID объекта
    m = re.search(r'/object(\d+)', url)
    if m:
        return f"https://img04.urban3p.ru/up/o/{m.group(1)}/preview.jpg"
    return images[0] if images else ""


def _tavily_search(query: str, images: bool = False) -> dict:
    return tavily_client.search(
        query,
        max_results=10,
        include_images=images,
        search_depth="advanced",
    )


async def _fetch_from_web(city: str) -> list:
    objects = []
    seen_names = set()

    for source in SOURCES:
        query = f"заброшка {city} {source}"
        try:
            data = await asyncio.to_thread(_tavily_search, query, True)
        except Exception as e:
            logger.warning(f"Tavily error ({source}): {e}")
            continue

        results = data.get("results", [])
        images = data.get("images", [])
        logger.info(f"Источник '{source}': {len(results)} результатов")

        for i, r in enumerate(results):
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")

            name = _extract_name(title)
            if not name or _normalize_name(name) in seen_names:
                continue
            if any(w in name.lower() for w in BANNED_WORDS):
                continue
            # Пропускаем статьи-списки и мусор
            if JUNK_RE.search(name) or JUNK_RE.search(title):
                continue
            # Пропускаем если в тексте упоминается другой город (не тот что ищем)
            if city.lower() not in content.lower() and city.lower() not in title.lower():
                continue

            desc = re.sub(r'\s+', ' ', content).strip()[:300] if content else ""
            address = _extract_address(content)
            image = _extract_image(url, images[i:i+1] if i < len(images) else [])

            obj = {
                "name": name,
                "coords": "",
                "address": address,
                "description": desc,
                "security": "",
                "source_name": "Urban3P" if "urban3p" in url else "интернет",
                "image": image,
                "published_date": r.get("published_date", ""),
            }

            seen_names.add(_normalize_name(name))
            objects.append(obj)

        if len(objects) >= 20:
            break

    logger.info(f"Собрано объектов для {city}: {len(objects)}")
    return objects


async def _fetch_and_cache(city: str) -> int:
    objects = await _fetch_from_web(city)
    if not objects:
        return 0
    await save_objects(city, objects)
    return len(objects)


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    count = await get_city_count(city)
    age = await get_cache_age_days(city)

    if count == 0 or age > CACHE_TTL_DAYS:
        logger.info(f"Кеш для {city}: {count} объектов, возраст {age:.1f} дней — обновляем")
        fetched = await _fetch_and_cache(city)
        if fetched == 0:
            return []
    else:
        logger.info(f"Кеш для {city}: {count} объектов — берём из базы")

    objects = await get_objects(city, shown, limit=3)
    if not objects:
        objects = await get_objects(city, set(), limit=3)

    return objects


async def search_by_name(name: str, city: str) -> dict:
    try:
        data = await asyncio.to_thread(
            lambda: tavily_client.search(
                f"{name} {city} заброшка урбекс",
                max_results=5,
                include_images=True,
            )
        )
        results = data.get("results", [])
        images = data.get("images", [])
        if not results:
            return {"not_found": True}

        content = "\n".join(f"- {r['title']}: {r['content']}" for r in results)
        prompt = f"""Найди инфу об объекте "{name}" в {city}. Отвечай только на русском.

Данные:
{content}

JSON: {{"name":"...","coords":"...","address":"...","description":"..."}}
Не найдено — верни: {{"not_found":true}}
"""
        text = await asyncio.to_thread(
            lambda: groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            ).choices[0].message.content
        )
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1][4:] if parts[1].startswith("json") else parts[1]
        result = json.loads(text.strip())
        if not result.get("not_found"):
            result["image"] = _extract_image(results[0].get("url", ""), images)
        return result
    except Exception:
        return {"not_found": True}
