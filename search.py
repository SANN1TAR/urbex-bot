# Поиск заброшенных объектов через urban3p.ru (Tavily) + кеш в SQLite
# Получает: тип объекта (zabroshka), город, список уже показанных
# Отдаёт: список объектов с названием, координатами/адресом, фото

import asyncio
import json
import logging
import os
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

JUNK_RE = re.compile(
    r'(\d+\s+заброшен|\bтоп[\s-]?\d+|\bч\.\s*\d+|часть\s+\d+|\||\bдзен\b|youtube|'
    r'заброшенные места|лучшие заброшки|самые|обзор|путешест)',
    re.IGNORECASE
)


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r'[«»"\'.,\-–—()]', '', name)
    return re.sub(r'\s+', ' ', name)


def _extract_name(title: str) -> str:
    if '/' in title:
        title = title.split('/')[0].strip()
    title = re.sub(
        r'\s*[-–]\s*(urban3p|urbantrip|urbact|заброшки|заброшенные|урбекс).*$',
        '', title, flags=re.IGNORECASE
    ).strip()
    title = re.sub(r'^заброшенные объекты в\s+', '', title, flags=re.IGNORECASE).strip()
    return title if len(title) > 3 else ""


def _is_junk_name(name: str, title: str) -> bool:
    text = (name + " " + title).lower()
    if any(w in text for w in BANNED_WORDS):
        return True
    if JUNK_RE.search(name) or JUNK_RE.search(title):
        return True
    if name.count(",") >= 2:
        return True
    return False


def _extract_address(text: str) -> str:
    patterns = [
        r'(?:ул\.|улица|пер\.|переулок|пр\.|проспект|ш\.|шоссе|бул\.|бульвар|пл\.|площадь)\s+[\w\s]+,?\s*д\.?\s*\d+[\w/]*',
        r'[\w\s]+ (?:ул\.|улица),?\s*\d+[\w/]*',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""


async def _get_coords_nominatim(name: str, city: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": f"{name}, {city}, Россия", "format": "json", "limit": 1},
                headers={"User-Agent": "UrbexBot/1.0"},
            )
            data = resp.json()
            if data:
                return f"{float(data[0]['lat']):.4f}, {float(data[0]['lon']):.4f}"
    except Exception:
        pass
    return ""


async def _scrape_object_page(url: str) -> dict:
    if not url or "urban3p" not in url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                return {}
            html = resp.text

        # OG фото
        img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not img:
            img = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
        image = img.group(1) if img else ""

        # Адрес из текста
        addr = re.search(r'(?:ул\.|улица|пер\.|проспект|пр-т|шоссе|бульвар|площадь)[^<]{3,50}', html, re.IGNORECASE)
        address = re.sub(r'\s+', ' ', addr.group(0)).strip() if addr else ""

        # Координаты через /onmap
        coords = ""
        m = re.search(r'/object(\d+)', url)
        if m:
            try:
                async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as client:
                    r = await client.post(
                        f"https://urban3p.ru/object{m.group(1)}/onmap",
                        data={"submitted": "смотреть на карте"},
                    )
                    ll = re.search(r'([5-7][0-9]\.[0-9]{4,})[,\s]+([3-7][0-9]\.[0-9]{4,})', r.text)
                    if ll:
                        coords = f"{ll.group(1)}, {ll.group(2)}"
            except Exception:
                pass

        logger.info(f"Парсинг {url[-30:]}: фото={'да' if image else 'нет'}, coords={coords or 'нет'}")
        return {"image": image, "coords": coords, "address": address}
    except Exception as e:
        logger.warning(f"Scrape error: {e}")
        return {}


def _tavily_photo(name: str, city: str) -> str:
    try:
        r = tavily_client.search(f"{name} {city} заброшка фото", max_results=3, include_images=True)
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


async def _fetch_from_web(city: str) -> list:
    objects = []
    seen_names = set()

    for source in SOURCES:
        try:
            data = await asyncio.to_thread(
                lambda s=source: tavily_client.search(
                    f"заброс заброшенная {city} {s}",
                    max_results=10,
                    include_images=True,
                    search_depth="advanced",
                )
            )
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

            # Только страницы конкретных объектов
            if "urban3p" in url and not re.search(r'/object\d+', url):
                continue

            name = _extract_name(title)
            if not name:
                continue
            norm = _normalize_name(name)
            if norm in seen_names:
                continue
            if _is_junk_name(name, title):
                continue
            if city.lower() not in content.lower() and city.lower() not in title.lower():
                continue

            page_data = await _scrape_object_page(url)

            image = page_data.get("image") or (images[i] if i < len(images) else "")
            if not image:
                image = await asyncio.to_thread(_tavily_photo, name, city)

            coords = page_data.get("coords", "")
            if not coords:
                coords = await _get_coords_nominatim(name, city)

            address = page_data.get("address", "") or _extract_address(content)

            objects.append({
                "name": name,
                "coords": coords,
                "address": address,
                "description": "",
                "security": "",
                "source_name": "Urban3P",
                "image": image,
                "published_date": "",
            })
            seen_names.add(norm)

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
        if await _fetch_and_cache(city) == 0:
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
        text = await asyncio.to_thread(
            lambda: groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": (
                    f'Найди инфу об объекте "{name}" в {city}. Отвечай только на русском.\n\n'
                    f'Данные:\n{content}\n\n'
                    f'JSON: {{"name":"...","coords":"...","address":"...","description":"..."}}\n'
                    f'Не найдено — верни: {{"not_found":true}}'
                )}],
                temperature=0.3,
            ).choices[0].message.content
        )
        text = text.strip()
        if "```" in text:
            parts = text.split("```")
            text = parts[1][4:] if parts[1].startswith("json") else parts[1]
        result = json.loads(text.strip())
        if not result.get("not_found"):
            result["image"] = images[0] if images else ""
        return result
    except Exception:
        return {"not_found": True}
