# Поиск заброшенных объектов через urban3p.ru (Tavily) + кеш в SQLite
# Получает: тип объекта (zabroshka), город, список уже показанных
# Отдаёт: список объектов с названием, описанием, фото

import asyncio
import json
import logging
import os
import random
import re

import httpx
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


def _is_valid_url(url: str) -> bool:
    # Только страницы конкретных объектов, не категории и не регионы
    if "urban3p" in url:
        return bool(re.search(r'/object\d+', url))
    return True


def _is_valid_description(text: str) -> str:
    # Убираем мусор — списки никнеймов, списки городов
    if not text:
        return ""
    # Если текст выглядит как список никнеймов (много слов слитно без пробелов)
    words = text.split()
    if len(words) > 5:
        avg_word_len = sum(len(w) for w in words) / len(words)
        if avg_word_len > 10:  # длинные слитные слова = никнеймы или города слитно
            return ""
    # Убираем строки с перечислением регионов
    if re.search(r'(область|край|округ).{0,20}(область|край|округ)', text):
        return ""
    return text.strip()


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
    # Убираем суффикс категории: "Завод ЗиЛ (Москва) / Заводы" → "Завод ЗиЛ (Москва)"
    if '/' in title:
        title = title.split('/')[0].strip()
    # Убираем суффиксы сайтов после тире
    title = re.sub(
        r'\s*[-–]\s*(urban3p|urbantrip|urbact|заброшки|заброшенные|урбекс).*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    # Убираем "Заброшенные объекты в ..."
    title = re.sub(r'^заброшенные объекты в\s+', '', title, flags=re.IGNORECASE).strip()
    return title if len(title) > 3 else ""


def _extract_image(images: list) -> str:
    for img in images:
        if img and img.startswith("http"):
            return img
    return ""


async def _get_coords_nominatim(name: str, city: str) -> str:
    """Ищет координаты через Nominatim по названию объекта"""
    query = f"{name}, {city}, Россия"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "UrbexBot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params=params, headers=headers
            )
            data = resp.json()
            if data:
                return f"{float(data[0]['lat']):.4f}, {float(data[0]['lon']):.4f}"
    except Exception:
        pass
    return ""


async def _scrape_object_page(url: str) -> dict:
    """Парсит страницу объекта urban3p.ru — берёт OG фото"""
    if not url or "urban3p" not in url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                return {}
            html = resp.text

        # Фото из og:image
        img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not img:
            img = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
        image = img.group(1) if img else ""

        # Адрес из текста страницы
        addr = re.search(r'(?:ул\.|улица|пер\.|проспект|пр-т|шоссе|бульвар|площадь)[^<]{3,50}', html, re.IGNORECASE)
        address = re.sub(r'\s+', ' ', addr.group(0)).strip() if addr else ""

        # Координаты через /onmap endpoint
        coords = ""
        obj_match = re.search(r'/object(\d+)', url)
        if obj_match:
            onmap_url = f"https://urban3p.ru/object{obj_match.group(1)}/onmap"
            try:
                async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as client:
                    r = await client.post(onmap_url, data={"submitted": "смотреть на карте"})
                    lat_lon = re.search(r'([5-7][0-9]\.[0-9]{4,})[,\s]+([3-7][0-9]\.[0-9]{4,})', r.text)
                    if lat_lon:
                        coords = f"{lat_lon.group(1)}, {lat_lon.group(2)}"
            except Exception:
                pass

        logger.info(f"Парсинг {url}: фото={'да' if image else 'нет'}, coords={coords or 'нет'}")
        return {"image": image, "coords": coords, "address": address}
    except Exception as e:
        logger.warning(f"Scrape error {url}: {e}")
        return {}


def _tavily_search(query: str, images: bool = False) -> dict:
    return tavily_client.search(
        query,
        max_results=10,
        include_images=images,
        search_depth="advanced",
    )


def _tavily_photo(name: str, city: str) -> str:
    try:
        r = tavily_client.search(
            f"{name} {city} заброшка фото",
            max_results=3,
            include_images=True,
        )
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


async def _fetch_from_web(city: str) -> list:
    objects = []
    seen_names = set()

    for source in SOURCES:
        query = f"заброс заброшенная {city} {source}"
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
            # Только страницы конкретных объектов
            if not _is_valid_url(url):
                continue
            # Пропускаем статьи-списки и мусор
            if JUNK_RE.search(name) or JUNK_RE.search(title):
                continue
            # Название не должно быть перечислением типов объектов
            if name.count(",") >= 2:
                continue
            # Пропускаем если в тексте не упоминается нужный город
            if city.lower() not in content.lower() and city.lower() not in title.lower():
                continue

            raw_desc = re.sub(r'\s+', ' ', content).strip()[:400] if content else ""
            desc = _is_valid_description(raw_desc)
            if not desc:
                continue
            desc = desc[:300]
            address = _extract_address(content)
            coords = ""
            # Парсим страницу объекта — там OG фото и координаты
            page_data = await _scrape_object_page(url)
            image = page_data.get("image") or _extract_image(images[i:i+1] if i < len(images) else [])
            if not image:
                image = await asyncio.to_thread(_tavily_photo, name, city)
            coords = page_data.get("coords", "")
            if not coords:
                coords = await _get_coords_nominatim(name, city)
            if not address:
                address = page_data.get("address", "")

            obj = {
                "name": name,
                "coords": coords,
                "address": address,
                "description": desc,
                "security": "",
                "source_name": "Urban3P",
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
            result["image"] = _extract_image(images)
        return result
    except Exception:
        return {"not_found": True}
