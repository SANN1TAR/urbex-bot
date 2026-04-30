# Поиск объектов по иерархии источников: Викимапия → Урбантрип → Telegram → VK → общий
# Получает: тип объекта (zabroshka), город, список уже показанных
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
    # Снос и реновация
    "под снос", "снос", "реновация", "расселённый", "переселение",
    # Кинотеатры и культура (действующие)
    "кинотеатр", "театр", "филармония", "дворец культуры", "дк ",
    # Несуществующие объекты (теги Викимапии)
    "исторический слой", "исчезнувший", "невидимый объект", "historical",
    "снесён", "снесено", "снесена", "не существует", "больше не существует",
    # Не городские объекты
    "деревня", "село ", "сельское", "посёлок", "пос.", "садовое товарищество", "снт ",
    # Сгоревшие
    "сгоревш", "пожар",
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
    "zabroshka": "заброшенное здание улица адрес урбекс заброшка",
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


_FAKE_ADDR = re.compile(
    r'исходя|вероятно|возможно|предположительно|по всей видимости|'
    r'без указания|не указан|неизвестно|unknown|н/д|n/a',
    re.IGNORECASE
)

def _clean_address(addr: str, city: str) -> str:
    if not addr:
        return ""
    if _FAKE_ADDR.search(addr):
        return ""
    bad = re.compile(
        r'^(центр\s+\w+|у\s+\w+|рядом|около|вблизи|недалеко|неподалёку|'
        r'неподалеку|' + re.escape(city) + r'$)',
        re.IGNORECASE
    )
    if bad.match(addr.strip()):
        return ""
    return addr


def _clean_description(text: str) -> str:
    if not text:
        return ""
    text = _clean(text)
    # убираем мусорные фразы про "неизвестное время"
    text = re.sub(r'[,.]?\s*[а-яё]+\s+в\s+неизвестн\w+\s+время\.?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[,.]?\s*заброшен\w*\s+в\s+неизвестн\w+\s+время\.?', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', text).strip()


def _tavily(query: str, images: bool = False) -> dict:
    return tavily_client.search(query, max_results=8, include_images=images)


def _groq(prompt: str) -> str:
    try:
        text = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        ).choices[0].message.content
        logger.info(f"Groq ответил: {text[:80]}")
        return text
    except Exception as e:
        logger.error(f"Groq упал: {e}")
        raise


async def _get_photo(name: str, city: str) -> str:
    try:
        r = await asyncio.to_thread(_tavily, f"{name} {city} здание фасад заброшка", True)
        imgs = r.get("images", [])
        return imgs[0] if imgs else ""
    except Exception:
        return ""


def _build_prompt(obj_type: str, city: str, results: list, shown: set) -> str:
    task = f"Найди 3 заброшенных объекта в городе {city}. {NO_LIST} Малоизвестные реальные заброшки. Только в самом городе {city} — не в пригородах, не в области, не в административно присоединённых районах типа Зеленограда или Новой Москвы."
    exclude = f"\n\nСТРОГО НЕ ПОВТОРЯТЬ:\n" + "\n".join(f"- {n}" for n in shown) if shown else ""
    data = "\n".join(f"- {r['title']}: {r['content']}" for r in results)

    return f"""{task}{exclude}

СТРОГИЕ ПРАВИЛА — нарушение недопустимо:
1. Только реально заброшенные объекты — не действующие заводы, жилые дома, госучреждения, торговые центры, парки.
2. coords — ТОЛЬКО если координаты прямо написаны в данных ниже. Не угадывать, не вычислять, не придумывать. Если нет — пустая строка "".
3. address — ТОЛЬКО если адрес прямо написан в данных ниже. Не угадывать, не писать "вероятно", "исходя из описания", "без указания". Если нет — пустая строка "".
4. Отвечай ТОЛЬКО на русском языке.
5. Возвращай ТОЛЬКО JSON, без пояснений.

Данные из источников:
{data}

JSON массив (меньше 3 — дай сколько есть, ничего нет — верни []):
[{{"name":"...","coords":"...","address":"...","description":"...","security":"..."}}]
"""


def _dedup_coords(objects: list) -> list:
    seen_coords = set()
    for obj in objects:
        coords = obj.get("coords", "").strip()
        if not coords:
            continue
        if coords in seen_coords:
            obj["coords"] = ""
        else:
            seen_coords.add(coords)
    return objects


def _process_obj(obj: dict, results: list, idx: int, city: str) -> dict:
    obj["published_date"] = results[idx].get("published_date", "") if idx < len(results) else ""
    obj["description"] = _clean_description(obj.get("description", ""))
    obj["security"] = _clean(obj.get("security", ""))
    obj["address"] = _clean_address(obj.get("address", ""), city)
    return obj


def _dedup_objects(objects: list) -> list:
    seen_names = set()
    seen_addrs = set()
    result = []
    for obj in objects:
        norm_name = _normalize_name(obj.get("name", ""))
        addr = obj.get("address", "").strip().lower()
        if norm_name in seen_names:
            continue
        if addr and addr in seen_addrs:
            continue
        seen_names.add(norm_name)
        if addr:
            seen_addrs.add(addr)
        result.append(obj)
    return result


async def search_objects(obj_type: str, city: str, shown: set) -> list:
    base = f"{BASE_QUERIES[obj_type]} {city}"

    # Собираем результаты со всех источников параллельно
    queries = [(f"{base} {src}".strip(), name) for src, name in SOURCES]
    tasks = [asyncio.to_thread(_tavily, q) for q, _ in queries]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for (_, source_name), res in zip(queries, raw):
        if isinstance(res, Exception):
            continue
        batch = res.get("results", [])
        logger.info(f"Источник '{source_name}': {len(batch)} результатов")
        for r in batch[:3]:
            r["_source"] = source_name
            all_results.append(r)

    if not all_results:
        return []

    # Один вызов к LLM со всеми данными
    try:
        text = await asyncio.to_thread(_groq, _build_prompt(obj_type, city, all_results, shown))
        logger.info(f"Groq: {text[:100]}")
        objects = _parse_json(text)
    except Exception as e:
        logger.error(f"Groq упал на финальном: {e}")
        return []

    objects = [o for o in objects if not _is_banned(o) and not _is_shown(o.get("name", ""), shown)]
    if not objects and shown:
        # Если всё отфильтровалось из-за shown — пробуем без него
        try:
            text = await asyncio.to_thread(_groq, _build_prompt(obj_type, city, all_results, set()))
            objects = [o for o in _parse_json(text) if not _is_banned(o)]
        except Exception:
            return []

    for i, obj in enumerate(objects):
        _process_obj(obj, all_results, i, city)
        obj["source_name"] = all_results[i].get("_source", "интернет") if i < len(all_results) else "интернет"
        obj["image"] = await _get_photo(obj.get("name", ""), city)

    return _dedup_coords(_dedup_objects(objects))


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
        text = await asyncio.to_thread(_groq, prompt)
        result = _parse_json(text)
        if not result.get("not_found"):
            result["description"] = _clean(result.get("description", ""))
            result["address"] = _clean_address(result.get("address", ""), city)
            result["image"] = images[0] if images else await _get_photo(name, city)
        return result
    except Exception:
        return {"not_found": True}
