# Поиск заброшенных объектов через OpenStreetMap (Overpass API + Nominatim)
# Получает: тип объекта (zabroshka), город, список уже показанных
# Отдаёт: список объектов с реальными координатами/адресом из OSM, фото

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

load_dotenv()
logger = logging.getLogger(__name__)

tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

_counters: dict = {}

BANNED_WORDS = {
    # Небоскрёбы и бизнес-центры
    "москва-сити", "moscow city", "москва сити", "сити", "федерация",
    "эволюция", "империя", "меркурий", "нафта", "око", "башня 2000",
    "бизнес-центр", "бизнес центр", "офисный центр", "технопарк",
    # Госучреждения и силовые структуры
    "кремль", "мгу", "вднх", "лужники", "газпром", "фсб", "фсо", "минобороны",
    "останкино", "останкинская", "большой театр", "гум", "цум",
    "администрация", "мэрия", "правительство", "министерство", "прокуратура",
    "полиция", "суд", "тюрьма", "следственный", "росгвардия",
    # Действующие предприятия
    "торговый центр", "торговый комплекс", "молл",
    "гипермаркет", "супермаркет", "ресторан", "кафе", "отель", "гостиница",
    # Парки и общественные пространства
    "парк культуры", "ботанический", "зоопарк", "цирк", "стадион", "арена",
    "музей", "галерея", "мемориал",
    # Несуществующие объекты
    "исторический слой", "исчезнувший", "невидимый объект",
    "снесён", "снесено", "снесена",
    # Не городские
    "деревня", "сельское", "садовое товарищество", "снт ",
}

BUILDING_TYPES = {
    "industrial": "Заброшенный завод",
    "warehouse": "Заброшенный склад",
    "school": "Заброшенная школа",
    "hospital": "Заброшенная больница",
    "hotel": "Заброшенная гостиница",
    "office": "Заброшенный офис",
    "residential": "Заброшенный жилой дом",
    "apartments": "Заброшенный жилой дом",
    "church": "Заброшенная церковь",
    "factory": "Заброшенная фабрика",
    "dormitory": "Заброшенное общежитие",
    "garage": "Заброшенные гаражи",
    "manor": "Заброшенная усадьба",
    "farm": "Заброшенная ферма",
    "military": "Заброшенный военный объект",
    "yes": "Заброшенное здание",
}


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


async def _get_city_coords(city: str) -> tuple | None:
    params = {"q": f"{city}, Россия", "format": "json", "limit": 1}
    headers = {"User-Agent": "UrbexTelegramBot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params=params, headers=headers,
            )
            data = resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.error(f"Nominatim error: {e}")
    return None


async def _overpass_search(lat: float, lon: float, radius: int = 20000) -> list:
    query = f"""
[out:json][timeout:30];
(
  way["abandoned"="yes"](around:{radius},{lat},{lon});
  way["disused"="yes"]["building"](around:{radius},{lat},{lon});
  way["ruins"="yes"]["building"](around:{radius},{lat},{lon});
  way["building:condition"="ruins"](around:{radius},{lat},{lon});
  way["abandoned:building"](around:{radius},{lat},{lon});
  node["abandoned"="yes"](around:{radius},{lat},{lon});
  node["abandoned:building"](around:{radius},{lat},{lon});
);
out center tags;
"""
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
            )
            elements = resp.json().get("elements", [])
            logger.info(f"Overpass: {len(elements)} объектов найдено")
            return elements
    except Exception as e:
        logger.error(f"Overpass error: {e}")
        return []


def _osm_to_obj(element: dict) -> dict | None:
    tags = element.get("tags", {})

    # Координаты
    if element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")

    if not lat or not lon:
        return None

    # Название
    name = tags.get("name:ru") or tags.get("name") or tags.get("old_name:ru") or tags.get("old_name")
    if not name:
        btype = (
            tags.get("abandoned:building") or
            tags.get("building") or
            tags.get("disused:building") or "yes"
        )
        name = BUILDING_TYPES.get(btype, "Заброшенное здание")

    # Адрес из OSM тегов — только реальные данные
    addr_parts = []
    if tags.get("addr:street"):
        addr_parts.append(tags["addr:street"])
    if tags.get("addr:housenumber"):
        addr_parts.append(f"д. {tags['addr:housenumber']}")
    address = ", ".join(addr_parts) if addr_parts else ""

    # Описание
    description = tags.get("description:ru") or tags.get("description") or ""
    if not description:
        btype = tags.get("building") or tags.get("abandoned:building") or ""
        desc_type = BUILDING_TYPES.get(btype, "")
        description = desc_type if desc_type and desc_type != name else "Заброшенный объект."

    # Охрана
    access = tags.get("access", "")
    security = ""
    if access == "no":
        security = "Закрытая территория"
    elif access == "private":
        security = "Частная территория"

    return {
        "name": name,
        "coords": f"{lat:.4f}, {lon:.4f}",
        "address": address,
        "description": description,
        "security": security,
        "source_name": "OpenStreetMap",
        "image": "",
        "published_date": "",
    }


async def _get_photo(name: str, city: str) -> str:
    try:
        r = await asyncio.to_thread(
            lambda: tavily_client.search(
                f"{name} {city} заброшка фото",
                max_results=3,
                include_images=True,
            )
        )
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    coords = await _get_city_coords(city)
    if not coords:
        logger.warning(f"Координаты города не найдены: {city}")
        return []

    lat, lon = coords
    logger.info(f"Город {city}: {lat:.4f}, {lon:.4f}")

    elements = await _overpass_search(lat, lon)
    if not elements:
        return []

    # Конвертируем и фильтруем
    objects = []
    for el in elements:
        obj = _osm_to_obj(el)
        if obj and not _is_banned(obj) and not _is_shown(obj["name"], shown):
            objects.append(obj)

    logger.info(f"После фильтрации: {len(objects)} объектов")

    # Если всё отсеялось из-за shown — сбрасываем shown
    if not objects:
        objects = [o for o in (_osm_to_obj(el) for el in elements) if o and not _is_banned(o)]

    if not objects:
        return []

    selected = random.sample(objects, min(3, len(objects)))

    for obj in selected:
        obj["image"] = await _get_photo(obj["name"], city)

    return selected


async def search_by_name(name: str, city: str) -> dict:
    # Ищем в OSM по имени
    coords = await _get_city_coords(city)
    if coords:
        lat, lon = coords
        query = f"""
[out:json][timeout:15];
(
  way["name"~"{name}",i](around:25000,{lat},{lon});
  node["name"~"{name}",i](around:25000,{lat},{lon});
  way["old_name"~"{name}",i](around:25000,{lat},{lon});
);
out center tags;
"""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://overpass-api.de/api/interpreter",
                    data={"data": query},
                )
                elements = resp.json().get("elements", [])
                if elements:
                    obj = _osm_to_obj(elements[0])
                    if obj:
                        obj["image"] = await _get_photo(name, city)
                        return obj
        except Exception as e:
            logger.error(f"Overpass name search error: {e}")

    # Fallback: Tavily + Groq
    try:
        response = await asyncio.to_thread(
            lambda: tavily_client.search(
                f"{name} {city} заброшка урбекс",
                max_results=5,
                include_images=True,
            )
        )
        results = response.get("results", [])
        images = response.get("images", [])
        if not results:
            return {"not_found": True}

        data = "\n".join(f"- {r['title']}: {r['content']}" for r in results)
        prompt = f"""Найди инфу об объекте "{name}" в {city}. Отвечай только на русском.

Данные:
{data}

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
            result["image"] = images[0] if images else await _get_photo(name, city)
        return result
    except Exception:
        return {"not_found": True}
