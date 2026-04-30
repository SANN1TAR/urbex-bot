# Поиск объектов по иерархии источников: Викимапия → Урбантрип → Telegram → VK → общий
# Получает: тип объекта (zabroshka), город, список уже показанных
# Отдаёт: список объектов с координатами/адресом, описанием, фото

import asyncio
import json
import logging
import os
import re

import google.generativeai as genai
from tavily import TavilyClient
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
_gemini_model = genai.GenerativeModel("gemini-2.0-flash")
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

NO_LIST = "Нельзя: МГУ, Кремль, телебашни, Москва-Сити, ВДНХ, Останкино, госучреждения."

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
    "действующий завод", "работающий завод", "действующая фабрика",
    "торговый центр", "торговый комплекс", "тц ", "тк ", "молл",
    "гипермаркет", "супермаркет", "магазин", "рынок",
    "ресторан", "кафе", "отель", "гостиница", "хостел",
    # Парки и общественные пространства
    "сквер", "парк культуры", "аллея", "фонтан", "бульвар", "набережная",
    "музей", "дом-музей", "галерея", "выставка", "мемориал", "памятник",
    "ботанический", "зоопарк", "цирк", "стадион", "арена",
    # Жилые дома
    "жилой дом", "жилой квартал", "жилой комплекс", "жк ", "многоквартирный",
}


def _is_banned(obj: dict) -> bool:
    text = (obj.get("name", "") + " " + obj.get("description", "")).lower()
    return any(word in text for word in BANNED_WORDS)

SOURCES = [
    ("site:wikimapia.org", "Викимапия"),
    ("site:urbantrip.ru", "Урбантрип"),
    ("site:urbantrip.ru OR site:wikimapia.org", "Викимапия/Урбантрип"),
    ("site:vk.com", "ВКонтакте"),
    ("site:t.me", "Telegram"),
    ("site:youtube.com OR site:instagram.com", "YouTube/Instagram"),
    ("", "интернет"),
]

BASE_QUERIES = {
    "zabroshka": "заброшка здание адрес координаты урбекс",
}

_counters: dict = {}


def _normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r'[«»"\'.,\-–—()]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name


def _is_shown(name: str, shown: set) -> bool:
    norm = _normalize_name(name)
    return any(_normalize_name(s) == norm for s in shown)


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


def _gemini(prompt: str) -> str:
    return _gemini_model.generate_content(prompt).text


async def _get_photo(name: str, city: str) -> str:
    try:
        r = await asyncio.to_thread(_tavily, f"{name} {city} здание фасад заброшка", True)
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


def _build_prompt(obj_type: str, city: str, results: list, shown: set) -> str:
    task = f"Найди 3 заброшенных объекта в {city}. {NO_LIST} Малоизвестные реальные заброшки."
    exclude = f"\n\nСТРОГО НЕ ПОВТОРЯТЬ:\n" + "\n".join(f"- {n}" for n in shown) if shown else ""
    data = "\n".join(f"- {r['title']}: {r['content']}" for r in results)

    return f"""{task}{exclude}

СТРОГИЕ ПРАВИЛА — нарушение недопустимо:
1. Только реально заброшенные объекты — не действующие заводы, жилые дома, госучреждения, торговые центры, парки.
2. coords — ТОЛЬКО если координаты прямо написаны в данных ниже. Не угадывать, не вычислять, не придумывать. Если нет — пустая строка "".
3. address — только конкретная улица с номером или район. Если нет точного адреса — пустая строка "". Не писать просто название города.
4. Отвечай ТОЛЬКО на русском языке.
5. Возвращай ТОЛЬКО JSON, без пояснений.

Данные из источников:
{data}

JSON массив (меньше 3 — дай сколько есть, ничего нет — верни []):
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]
"""


def _process_obj(obj: dict, results: list, idx: int, city: str) -> dict:
    obj["published_date"] = results[idx].get("published_date", "") if idx < len(results) else ""
    obj["description"] = _clean(obj.get("description", ""))
    obj["security"] = _clean(obj.get("security", ""))
    obj["address"] = _clean_address(obj.get("address", ""), city)
    return obj


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    base = f"{BASE_QUERIES[obj_type]} {city}"

    for source, source_name in SOURCES:
        query = f"{base} {source}".strip()
        try:
            results = (await asyncio.to_thread(_tavily, query)).get("results", [])
        except Exception:
            continue

        logger.info(f"Источник '{source_name}': {len(results)} результатов")
        if not results:
            continue

        try:
            text = await asyncio.to_thread(_gemini, _build_prompt(obj_type, city, results, shown))
            logger.info(f"Gemini: {text[:100]}")
            objects = _parse_json(text)
        except Exception:
            continue

        objects = [o for o in objects if not _is_banned(o) and not _is_shown(o.get("name", ""), shown)]
        if objects:
            for i, obj in enumerate(objects):
                _process_obj(obj, results, i, city)
                obj["source_name"] = source_name
                obj["image"] = await _get_photo(obj.get("name", ""), city)
            return objects

    # Финальный резерв — без ограничений по shown
    try:
        results = (await asyncio.to_thread(_tavily, base)).get("results", [])
        text = await asyncio.to_thread(_gemini, _build_prompt(obj_type, city, results, set()))
        objects = [o for o in _parse_json(text) if not _is_banned(o)]
        for i, obj in enumerate(objects):
            _process_obj(obj, results, i, city)
            obj["source_name"] = "интернет"
            obj["image"] = await _get_photo(obj.get("name", ""), city)
        return objects
    except Exception:
        return []


async def search_by_name(name: str, city: str) -> dict:
    try:
        response = await asyncio.to_thread(_tavily, f"{name} {city} заброшка урбекс", True)
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
        text = await asyncio.to_thread(_gemini, prompt)
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["address"] = _clean_address(result.get("address", ""), city)
            result["image"] = images[0] if images else await _get_photo(name, city)
        return result
    except Exception:
        return {"not_found": True}
