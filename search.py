import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import asyncpg
import httpx
from tavily import TavilyClient

from database import (
    get_objects, save_objects, get_city_last_fetched,
    update_city_fetched, is_duplicate, get_located_object_count, CACHE_REFRESH_DAYS,
)

logger = logging.getLogger(__name__)

_tavily_client: TavilyClient | None = None
_http_client: httpx.AsyncClient | None = None

# Per-city locks to prevent concurrent duplicate fetches
_city_fetch_locks: dict[str, asyncio.Lock] = {}

# SSRF protection: only scrape these hosts
_ALLOWED_SCRAPE_HOSTS = {"urban3p.ru", "www.urban3p.ru", "urban3p.com", "www.urban3p.com"}
# Image URLs must come from these hosts
_ALLOWED_IMAGE_HOSTS = {
    "img04.urban3p.ru", "img03.urban3p.ru", "img02.urban3p.ru",
    "img.urban3p.ru", "img04.urban3p.com",
}

# Force refresh when located object count is below this threshold
_MIN_OBJECTS_THRESHOLD = 20

# Nominatim geocoding settings (max 1 req/sec per usage policy)
_NOMINATIM_SEM = asyncio.Semaphore(1)
_GEOCODE_LIMIT = 20  # max Nominatim calls per city per refresh (~22 seconds)

# Nominatim result types that are too coarse to be useful as a location
_NOMINATIM_REJECT_TYPES = {
    "city", "town", "village", "hamlet", "county", "state", "country",
    "administrative", "municipality", "region", "boundary",
}
# Nominatim classes that indicate a useful specific location
_NOMINATIM_ACCEPT_CLASSES = {
    "building", "amenity", "historic", "industrial", "landuse",
    "place", "highway", "railway", "tourism", "man_made",
}

_CATALOG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


def init_search(tavily_api_key: str) -> None:
    global _tavily_client, _http_client
    _tavily_client = TavilyClient(api_key=tavily_api_key)
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        headers=_CATALOG_HEADERS,
        follow_redirects=False,
    )


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _is_safe_url(url: str, allowed_hosts: set[str]) -> bool:
    """Return True only if URL scheme is http/https and hostname is in the allowlist."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and (p.hostname or "") in allowed_hosts
    except Exception:
        return False


SEARCH_TEMPLATES = [
    "заброшенный завод {city} site:urban3p.ru",
    "заброшенная больница {city} site:urban3p.ru",
    "заброшенная фабрика {city} site:urban3p.ru",
    "заброшенная школа {city} site:urban3p.ru",
    "заброшенный институт {city} site:urban3p.ru",
    "заброшенный санаторий {city} site:urban3p.ru",
    "заброшенный {city} site:urban3p.com",
    "заброшенная {city} site:urban3p.com",
]

CITY_SEARCH_TERMS: dict[str, list[str]] = {
    "Москва": ["москва", "moscow", "г. москва"],
    "Московская область": ["московская область", "подмосковье", "московск"],
    "Санкт-Петербург": ["санкт-петербург", "петербург", "ленинград"],
    "Екатеринбург": ["екатеринбург", "свердловская"],
    "Новосибирск": ["новосибирск", "новосибирская"],
    "Нижний Новгород": ["нижний новгород", "нижегородская"],
    "Краснодар": ["краснодар", "краснодарский"],
    "Казань": ["казань", "татарстан"],
    "Челябинск": ["челябинск", "челябинская"],
    "Уфа": ["уфа", "башкортостан"],
    "Омск": ["омск", "омская"],
    "Самара": ["самара", "самарская"],
    "Ростов-на-Дону": ["ростов", "ростовская"],
    "Волгоград": ["волгоград", "волгоградская"],
    "Пермь": ["пермь", "пермский"],
    "Алматы": ["алматы", "алма-ата"],
    "Астана": ["астана", "нур-султан", "акмолинская"],
}

CITY_BBOXES: dict[str, str] = {
    "Москва": "36.8,55.4,38.0,56.1",
    "Московская область": "35.9,54.7,39.5,56.9",
    "Санкт-Петербург": "29.5,59.7,30.8,60.3",
    "Екатеринбург": "60.4,56.6,61.1,56.97",
    "Новосибирск": "82.6,54.7,83.2,55.2",
    "Нижний Новгород": "43.6,56.1,44.2,56.5",
    "Краснодар": "38.8,45.0,39.2,45.2",
    "Казань": "48.9,55.7,49.4,56.0",
    "Челябинск": "61.2,55.0,61.6,55.3",
    "Уфа": "55.8,54.6,56.2,54.9",
    "Омск": "73.1,54.8,73.6,55.1",
    "Самара": "50.1,53.1,50.4,53.3",
    "Ростов-на-Дону": "39.5,47.1,39.9,47.4",
    "Волгоград": "44.2,48.4,44.8,48.8",
    "Пермь": "56.0,57.8,56.5,58.1",
}

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

JUNK_RE = re.compile(
    r'(\d+\s+заброшен|\bтоп[\s-]?\d+|\bч\.\s*\d+|часть\s+\d+|\||\bдзен\b|youtube|'
    r'заброшенные места|лучшие заброшки|самые|обзор|путешест)',
    re.IGNORECASE
)

_ADDRESS_RE = re.compile(
    r'(?:ул\.|улица|пер\.|переулок|пр\.|проспект|ш\.|шоссе|бул\.|бульвар|пл\.|площадь)'
    r'\s+[\w\s]+,?\s*(?:д\.?\s*\d+[\w/]*)?',
    re.IGNORECASE
)


def _in_cis_bounds(lat: float, lon: float) -> bool:
    return 40 <= lat <= 80 and 20 <= lon <= 180


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
    title = re.sub(r'\s*\([^)]{2,20}\)\s*$', '', title).strip()
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


def _parse_coords(coords_str: str) -> tuple[float, float] | None:
    if not coords_str:
        return None
    m = re.search(r'([0-9]+\.[0-9]+)[,\s]+([0-9]+\.[0-9]+)', coords_str)
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
        if _in_cis_bounds(lat, lon):
            return (lat, lon)
        return None
    except ValueError:
        return None


def _safe_image_url(url: str) -> str:
    """Return url if it's from an allowed image host, else empty string."""
    if not url:
        return ""
    if _is_safe_url(url, _ALLOWED_IMAGE_HOSTS):
        return url
    return ""


async def geocode_nominatim(name: str, city: str) -> tuple[float, float] | None:
    """Geocode an object name in a city using Nominatim.

    Accepts district/neighbourhood level and more specific results.
    Rejects city/administrative level results (too coarse to be useful).
    Rate-limited to 1 req/sec to comply with Nominatim usage policy.
    """
    if _http_client is None:
        return None

    # Strip adjectives that confuse geocoding ("заброшенный завод X" → "завод X")
    clean = re.sub(
        r'\b(заброш\w+|бывш\w+|старый|старая|старое|недостроен\w+|бездейству\w+)\b',
        '', name, flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    clean = re.sub(r'[\x00-\x1f]', '', clean)[:120]  # strip control chars, cap length
    if len(clean) < 3:
        return None

    query = f"{clean}, {city}, Россия"

    async with _NOMINATIM_SEM:
        try:
            resp = await _http_client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "ru,kz,by,ua"},
                headers={"User-Agent": "UrbexTelegramBot/1.0 (https://github.com/SANN1TAR/urbex-bot)"},
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            )
            await asyncio.sleep(1.1)  # Nominatim: max 1 request per second

            if resp.status_code != 200 or not resp.json():
                return None

            result = resp.json()[0]
            lat = float(result["lat"])
            lon = float(result["lon"])
            r_type = result.get("type", "")
            r_class = result.get("class", "")

            # Reject results that are just city/region level
            if r_type in _NOMINATIM_REJECT_TYPES:
                logger.debug(f"Nominatim rejected '{name}': type={r_type}")
                return None

            # Accept only meaningful location classes
            if r_class not in _NOMINATIM_ACCEPT_CLASSES:
                # Still accept suburb/neighbourhood/quarter as useful districts
                if r_type not in {"suburb", "neighbourhood", "quarter", "district", "borough"}:
                    logger.debug(f"Nominatim rejected '{name}': class={r_class}, type={r_type}")
                    return None

            if not _in_cis_bounds(lat, lon):
                return None

            # Validate result is near the target city (within 2× the city bbox)
            bbox = CITY_BBOXES.get(city)
            if bbox:
                west, south, east, north = map(float, bbox.split(","))
                dlat = (north - south) * 1.0
                dlon = (east - west) * 1.0
                if not (south - dlat <= lat <= north + dlat and west - dlon <= lon <= east + dlon):
                    logger.debug(f"Nominatim rejected '{name}': coords outside city area")
                    return None

            logger.info(f"Nominatim: '{name}' → {lat:.4f},{lon:.4f} ({r_class}/{r_type})")
            return (lat, lon)

        except Exception as e:
            logger.debug(f"Nominatim error for '{name}': {type(e).__name__}")
            await asyncio.sleep(1.1)
            return None


def _parse_catalog_page(html: str) -> list[dict]:
    """Parse urban3p.ru/objects catalog page into list of {id, name, region}."""
    seen: set[str] = set()
    items = []

    for m in re.finditer(r'href="/object(\d+)"', html):
        obj_id = m.group(1)
        if obj_id in seen:
            continue
        seen.add(obj_id)

        pos = m.start()
        window = html[pos: pos + 2000]

        # Name: anchor text — [^<] avoids catastrophic backtracking and is correct
        # (anchor text cannot contain < in valid HTML)
        name_m = re.search(
            r'href="/object' + obj_id + r'"[^>]*>([^<]{3,100})</a>', window
        )
        if not name_m:
            name_m = re.search(r'alt="([^"]{3,100})"', window[:600])
        name = name_m.group(1).strip() if name_m else ""
        name = re.sub(r'\s+', ' ', name).strip()
        if len(name) < 3 or JUNK_RE.search(name):
            continue

        region_m = re.search(r'region_id=\d+"[^>]*>([^<]+)</a>', window)
        region = region_m.group(1).strip() if region_m else ""

        items.append({"id": obj_id, "name": name, "region": region})

    return items


async def _find_region_id(city: str) -> str | None:
    """Discover urban3p region_id for city from catalog first page."""
    if _http_client is None:
        return None
    search_terms = CITY_SEARCH_TERMS.get(city, [city.lower()])
    try:
        resp = await _http_client.get(
            "https://urban3p.ru/objects/",
            params={"page": 1},
        )
        if resp.status_code != 200:
            return None
        for m in re.finditer(r'region_id=(\d+)"[^>]*>([^<]+)</a>', resp.text):
            region_id = m.group(1)
            region_name = m.group(2).strip().lower()
            if any(t in region_name for t in search_terms):
                logger.info(f"Discovered region_id={region_id} for {city} ({region_name})")
                return region_id
    except Exception as e:
        logger.debug(f"Region ID discovery failed for {city}: {type(e).__name__}")
    return None


async def _fetch_urban3p_catalog(city: str, pool: asyncpg.Pool) -> list[dict]:
    """Paginate urban3p.ru catalog and collect objects matching the city.

    Objects are saved without coordinates — the background geocoder in bot.py
    adds coordinates incrementally (15 objects/hour via Nominatim).
    """
    if _http_client is None:
        return []
    search_terms = CITY_SEARCH_TERMS.get(city, [city.lower()])
    objects: list[dict] = []
    seen_ids: set[str] = set()

    region_id = await _find_region_id(city)
    # With region filter: scan up to 200 pages (2000 objects)
    # Without: scan 80 pages of general catalog with local city filter
    max_pages = 200 if region_id else 80
    base_params: dict = {}
    if region_id:
        base_params["region_id"] = region_id

    consecutive_empty = 0
    consecutive_unmatched = 0  # pages with cards but 0 city matches (no-region_id mode)
    for page in range(1, max_pages + 1):
        if consecutive_empty >= 3:
            break
        if not region_id and consecutive_unmatched >= 5:
            break
        try:
            resp = await _http_client.get(
                "https://urban3p.ru/objects/",
                params={**base_params, "page": page},
            )
            if resp.status_code != 200:
                logger.warning(f"urban3p catalog page {page}: HTTP {resp.status_code}")
                break

            cards = _parse_catalog_page(resp.text)

            if cards:
                consecutive_empty = 0
                matched = 0
                for card in cards:
                    obj_id = card["id"]
                    osm_id = f"u3p_{obj_id}"
                    if osm_id in seen_ids:
                        continue

                    if not region_id:
                        combined = (card["region"] + " " + card["name"]).lower()
                        if not any(t in combined for t in search_terms):
                            continue

                    seen_ids.add(osm_id)
                    objects.append({
                        "name": card["name"],
                        "lat": None,
                        "lon": None,
                        "address": card["region"] or city,
                        "source_name": "Urban3P",
                        "image": f"https://img04.urban3p.ru/up/o/{obj_id}/photo.jpg",
                        "osm_id": osm_id,
                    })
                    matched += 1

                if not region_id:
                    consecutive_unmatched = 0 if matched else consecutive_unmatched + 1
                if matched:
                    logger.info(f"urban3p catalog p{page}: +{matched} for {city}")
            else:
                consecutive_empty += 1

            await asyncio.sleep(0.3)

        except Exception as e:
            logger.warning(f"urban3p catalog page {page}: {type(e).__name__}")
            consecutive_empty += 1

    logger.info(f"urban3p catalog collected {len(objects)} objects for {city} (geocoding deferred)")
    return objects


async def _fetch_from_osm(city: str) -> list[dict]:
    bbox = CITY_BBOXES.get(city)
    if not bbox:
        logger.info(f"No OSM bbox configured for city: {city}")
        return []
    if _http_client is None:
        return []

    osm_filter = (
        "abandoned:building=* "
        "or (building=* and disused=yes) "
        "or historic=ruins "
        "or landuse=brownfield "
        "or abandoned=yes"
    )

    try:
        resp = await _http_client.get(
            "https://api.ohsome.org/v1/elements/centroid",
            params={
                "bboxes": bbox,
                "filter": osm_filter,
                "time": "2024-01-01",
                "properties": "tags",
            },
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        if resp.status_code != 200:
            logger.warning(f"ohsome API returned {resp.status_code} for {city}")
            return []
        data = resp.json()
    except httpx.TimeoutException:
        logger.warning(f"ohsome API timeout for {city}")
        return []
    except Exception as e:
        logger.error(f"ohsome API error for {city}: {type(e).__name__}")
        return []

    objects = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        lon, lat = float(coords[0]), float(coords[1])
        if not _in_cis_bounds(lat, lon):
            continue

        tags = props.get("tags", {})
        osm_id = props.get("@osmId", "")

        name = (
            tags.get("name")
            or tags.get("abandoned:name")
            or tags.get("old_name")
            or _build_osm_name(tags, osm_id)
        )
        if not name:
            continue

        address = " ".join(filter(None, [
            tags.get("addr:street", ""),
            tags.get("addr:housenumber", ""),
        ])).strip()

        objects.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "address": address,
            "source_name": "OpenStreetMap",
            "image": "",
            "osm_id": osm_id,
        })

    logger.info(f"OSM fetched {len(objects)} objects for {city}")
    return objects


def _build_osm_name(tags: dict, osm_id: str) -> str:
    type_map = {
        "factory": "Заброшенный завод",
        "industrial": "Заброшенное производство",
        "hospital": "Заброшенная больница",
        "school": "Заброшенная школа",
        "kindergarten": "Заброшенный детский сад",
        "office": "Заброшенный офис",
        "warehouse": "Заброшенный склад",
        "residential": "Заброшенный жилой дом",
        "church": "Заброшенная церковь",
        "ruins": "Руины",
        "brownfield": "Заброшенная промзона",
    }
    for tag_key in ("abandoned:building", "building", "abandoned:amenity", "amenity", "landuse", "historic"):
        val = tags.get(tag_key, "")
        if val in type_map:
            return type_map[val]
        if val and val != "yes":
            return f"Заброшенный объект ({val})"
    return ""


async def _scrape_object_page(url: str) -> dict:
    """Scrape an urban3p object page for image, address, and coordinates."""
    if not _is_safe_url(url, _ALLOWED_SCRAPE_HOSTS):
        return {}
    if _http_client is None:
        return {}
    try:
        resp = await _http_client.get(
            url,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        )
        if resp.status_code != 200:
            return {}
        html = resp.text

        # Build image from object ID — og:image is absent on urban3p
        image = ""
        id_m = re.search(r'/object(\d+)', url)
        if id_m:
            image = f"https://img04.urban3p.ru/up/o/{id_m.group(1)}/photo.jpg"

        if not image:
            img = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html
            )
            if not img:
                img = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html
                )
            if img:
                image = _safe_image_url(img.group(1))

        addr = _ADDRESS_RE.search(html)
        address = re.sub(r'\s+', ' ', addr.group(0)).strip() if addr else ""

        if not address:
            region_m = re.search(r'region_id=\d+"[^>]*>([^<]+)</a>', html)
            if region_m:
                address = region_m.group(1).strip()

        lat, lon = None, None
        coord_patterns = [
            r'"lat(?:itude)?"\s*:\s*"?([4-7][0-9]\.[0-9]+)"?[^}]*?"lo?n(?:gitude)?"\s*:\s*"?([2-9][0-9]\.[0-9]+)"?',
            r'\blat(?:itude)?\s*[:=]\s*([4-7][0-9]\.[0-9]+).*?\blo?n(?:gitude)?\s*[:=]\s*([2-9][0-9]\.[0-9]+)',
            r'data-lat[^=]*=\s*["\']([4-7][0-9]\.[0-9]+)["\'].*?data-lo?n[^=]*=\s*["\']([2-9][0-9]\.[0-9]+)["\']',
            r'(?:LatLng|center)\s*[\(\[]\s*([4-7][0-9]\.[0-9]+)\s*,\s*([2-9][0-9]\.[0-9]+)',
        ]
        for pattern in coord_patterns:
            cm = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if cm:
                parsed = _parse_coords(f"{cm.group(1)}, {cm.group(2)}")
                if parsed:
                    lat, lon = parsed
                    break

        logger.info(
            f"Scraped {url[-40:]}: image={'yes' if image else 'no'}, "
            f"lat={lat}, address={address or 'none'}"
        )
        return {"image": image, "lat": lat, "lon": lon, "address": address}

    except httpx.TimeoutException:
        logger.warning(f"Timeout scraping {url[-60:]}")
        return {}
    except httpx.ConnectError:
        logger.warning(f"Connection error scraping {url[-60:]}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error scraping {url[-60:]}: {type(e).__name__}")
        return {}


async def _tavily_search_one(query: str) -> list:
    if _tavily_client is None:
        logger.warning("Tavily client not initialized")
        return []
    try:
        data = await asyncio.to_thread(
            lambda: _tavily_client.search(
                query,
                max_results=10,
                include_images=True,
                search_depth="advanced",
            )
        )
        results = data.get("results", [])
        logger.info(f"Tavily '{query[:50]}': {len(results)} results")
        return results
    except Exception as e:
        logger.warning(f"Tavily error for '{query[:40]}': {type(e).__name__}")
        return []


async def _fetch_from_web(city: str, pool: asyncpg.Pool) -> list[dict]:
    if _tavily_client is None:
        raise RuntimeError("search not initialized — call init_search() first")

    queries = [t.format(city=city) for t in SEARCH_TEMPLATES]
    all_results_lists, osm_objects, catalog_objects = await asyncio.gather(
        asyncio.gather(*[_tavily_search_one(q) for q in queries]),
        _fetch_from_osm(city),
        _fetch_urban3p_catalog(city, pool),
    )
    all_results = [r for sublist in all_results_lists for r in sublist]
    logger.info(
        f"Raw sources for {city}: Tavily={len(all_results)}, "
        f"OSM={len(osm_objects)}, Catalog={len(catalog_objects)}"
    )

    objects: list[dict] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    seen_coords: set[tuple[float, float]] = set()

    # --- Tavily results ---
    for r in all_results:
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")

        if url in seen_urls:
            continue
        seen_urls.add(url)

        if not _is_safe_url(url, _ALLOWED_SCRAPE_HOSTS):
            continue
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
        lat = page_data.get("lat")
        lon = page_data.get("lon")
        address = page_data.get("address", "")
        image = page_data.get("image", "")

        if lat is None and not address:
            addr_match = _ADDRESS_RE.search(content)
            if addr_match:
                address = re.sub(r'\s+', ' ', addr_match.group(0)).strip()

        if lat is None and not address:
            logger.info(f"Skipping '{name}' — no location data")
            continue

        if lat is not None and lon is not None:
            coord_key = (round(lat, 3), round(lon, 3))
            if coord_key in seen_coords:
                continue
            seen_coords.add(coord_key)

        if await is_duplicate(pool, city, lat, lon):
            continue

        # Use blake2b for non-security dedup ID (faster than MD5, no broken algorithm warning)
        osm_id = "t_" + hashlib.blake2b(url.encode(), digest_size=8).hexdigest()
        objects.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "address": address,
            "source_name": "Urban3P",
            "image": image,
            "osm_id": osm_id,
        })
        seen_names.add(norm)

    # --- OSM results ---
    for obj in osm_objects:
        if not obj["name"]:
            continue
        norm = _normalize_name(obj["name"])
        if norm in seen_names:
            continue

        lat, lon = obj["lat"], obj["lon"]
        coord_key = (round(lat, 3), round(lon, 3))
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        if await is_duplicate(pool, city, lat, lon):
            continue

        objects.append(obj)
        seen_names.add(norm)

    # --- Catalog results (no coord dedup — lat=None for all catalog objects) ---
    for obj in catalog_objects:
        if not obj["name"]:
            continue
        norm = _normalize_name(obj["name"])
        if norm in seen_names:
            continue
        if _is_junk_name(obj["name"], obj["name"]):
            continue
        seen_names.add(norm)
        objects.append(obj)

    logger.info(f"Total fetched {len(objects)} objects for {city}")
    return objects


async def search_objects(
    pool: asyncpg.Pool,
    obj_type: str,
    city: str,
    shown_ids: set[int],
) -> list[dict]:
    """Main entry point. Returns up to 3 objects not yet shown to user."""
    last_fetched = await get_city_last_fetched(pool, city)
    needs_refresh = (
        last_fetched is None
        or (datetime.now(timezone.utc) - last_fetched).days >= CACHE_REFRESH_DAYS
    )

    if not needs_refresh:
        # Use located count (objects with real coords) — objects with only "Москва" don't count
        count = await get_located_object_count(pool, city)
        if count < _MIN_OBJECTS_THRESHOLD:
            logger.info(f"City {city} has only {count} located objects — forcing refresh")
            needs_refresh = True

    if needs_refresh:
        # Always acquire the lock — no TOCTOU. Double-check inside protects from double-fetch.
        lock = _city_fetch_locks.setdefault(city, asyncio.Lock())
        async with lock:
            # Only re-check time condition — count < threshold is just a trigger,
            # not a reason to re-fetch every call (avoids infinite refetch loop).
            last_fetched = await get_city_last_fetched(pool, city)
            still_needs = (
                last_fetched is None
                or (datetime.now(timezone.utc) - last_fetched).days >= CACHE_REFRESH_DAYS
            )
            if still_needs:
                    logger.info(f"Refreshing archive for {city} (last: {last_fetched})")
                    try:
                        new_objects = await _fetch_from_web(city, pool)
                        if new_objects:
                            saved = await save_objects(pool, city, new_objects)
                            logger.info(f"Upserted up to {saved} objects for {city} (new = actual inserts, dupes skipped)")
                            await update_city_fetched(pool, city)
                        else:
                            logger.warning(f"Fetch returned 0 objects for {city}")
                    except Exception as e:
                        logger.error(f"Failed to fetch for {city}: {type(e).__name__}: {e}")

    return await get_objects(pool, city, shown_ids, limit=3)
