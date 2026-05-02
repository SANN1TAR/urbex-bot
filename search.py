# Search for abandoned objects via Tavily + urban3p.ru scraping
# Returns objects with verified location (lat/lon or address) only

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta

import asyncpg
import httpx
from tavily import TavilyClient

from database import (
    get_objects, save_objects, get_city_last_fetched,
    update_city_fetched, is_duplicate, CACHE_REFRESH_DAYS,
)

logger = logging.getLogger(__name__)

_tavily_client: TavilyClient | None = None

def init_search(tavily_api_key: str) -> None:
    global _tavily_client
    _tavily_client = TavilyClient(api_key=tavily_api_key)

# Search query templates — {city} replaced at runtime
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

# Bounding boxes for cities [west,south,east,north] — for OSM queries
CITY_BBOXES: dict[str, str] = {
    "Москва": "36.8,55.4,38.0,56.1",
    "Московская область": "35.9,54.7,39.5,56.9",
    "Санкт-Петербург": "29.5,59.7,30.8,60.3",
    "Екатеринбург": "60.4,56.6,61.1,56.97",
    "Новосибирск": "82.6,54.7,83.2,55.2",
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
    # Remove city name in parentheses at end: "Название (Москва)" → "Название"
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
    """Parse '55.1234, 37.5678' into (lat, lon) floats. Returns None if invalid."""
    if not coords_str:
        return None
    m = re.search(r'([0-9]+\.[0-9]+)[,\s]+([0-9]+\.[0-9]+)', coords_str)
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
        # Sanity check: Russia/CIS latitude 40-80, longitude 20-180
        if 40 <= lat <= 80 and 20 <= lon <= 180:
            return (lat, lon)
        return None
    except ValueError:
        return None


async def _get_coords_nominatim(name: str, city: str) -> tuple[float, float] | None:
    """Geocode by name via Nominatim. Returns (lat, lon) only if within Russia/CIS bounds."""
    clean = re.sub(r'заброш\w+\s*', '', name, flags=re.IGNORECASE)
    clean = re.sub(r'\([^)]*\)', '', clean).strip()
    queries = [f"{clean}, {city}, Россия", f"{name}, {city}, Россия"]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            for q in queries:
                resp = await client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={"q": q, "format": "json", "limit": 1, "countrycodes": "ru,kz,ua,by"},
                    headers={"User-Agent": "UrbexBot/1.0"},
                )
                data = resp.json()
                if data:
                    parsed = _parse_coords(f"{data[0]['lat']}, {data[0]['lon']}")
                    if parsed:
                        logger.info(f"Nominatim found coords for '{name}': {parsed}")
                        return parsed
    except Exception as e:
        logger.debug(f"Nominatim error for '{name}': {e}")
    return None


async def _fetch_from_osm(city: str) -> list[dict]:
    """Fetch abandoned buildings from OpenStreetMap via ohsome API.
    Returns objects with precise GPS coordinates — no scraping needed."""
    bbox = CITY_BBOXES.get(city)
    if not bbox:
        logger.info(f"No OSM bbox configured for city: {city}")
        return []

    osm_filter = (
        "abandoned:building=* "
        "or (building=* and disused=yes) "
        "or historic=ruins "
        "or landuse=brownfield "
        "or abandoned=yes"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.ohsome.org/v1/elements/centroid",
                params={
                    "bboxes": bbox,
                    "filter": osm_filter,
                    "time": "2024-01-01",
                    "properties": "tags",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"ohsome API returned {resp.status_code} for {city}")
                return []
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning(f"ohsome API timeout for {city}")
        return []
    except Exception as e:
        logger.error(f"ohsome API error for {city}: {e}")
        return []

    objects = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        lon, lat = float(coords[0]), float(coords[1])
        # Validate coordinates are within Russia/CIS
        if not (40 <= lat <= 80 and 20 <= lon <= 180):
            continue

        tags = props.get("tags", {})
        osm_id = props.get("@osmId", "")

        # Build name from tags
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
    """Build a human-readable name from OSM tags when 'name' tag is missing."""
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
    """Scrape urban3p.ru object page for image, coords, address."""
    if not url or "urban3p" not in url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code != 200:
                return {}
            html = resp.text

        # OG image
        img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
        if not img:
            img = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
        image = img.group(1) if img else ""

        # Address from page text
        addr = re.search(
            r'(?:ул\.|улица|пер\.|переулок|пр\.|проспект|ш\.|шоссе|бул\.|бульвар|пл\.|площадь)\s+[\w\s]+,?\s*д\.?\s*\d+[\w/]*',
            html, re.IGNORECASE
        )
        address = re.sub(r'\s+', ' ', addr.group(0)).strip() if addr else ""

        # Coordinates: search in HTML (JSON data, JS variables, data attributes)
        lat, lon = None, None
        coord_patterns = [
            # JSON: "lat":55.123,"lon":37.456 or "latitude":55.123,"longitude":37.456
            r'"lat(?:itude)?"\s*:\s*"?([4-7][0-9]\.[0-9]+)"?[^}]*?"lo?n(?:gitude)?"\s*:\s*"?([2-9][0-9]\.[0-9]+)"?',
            # JS: lat: 55.123, lon: 37.456
            r'\blat(?:itude)?\s*[:=]\s*([4-7][0-9]\.[0-9]+).*?\blo?n(?:gitude)?\s*[:=]\s*([2-9][0-9]\.[0-9]+)',
            # data-lat="55.123" data-lon="37.456"
            r'data-lat[^=]*=\s*["\']([4-7][0-9]\.[0-9]+)["\'].*?data-lo?n[^=]*=\s*["\']([2-9][0-9]\.[0-9]+)["\']',
            # LatLng(55.123, 37.456) or [55.123, 37.456]
            r'(?:LatLng|center)\s*[\(\[]\s*([4-7][0-9]\.[0-9]+)\s*,\s*([2-9][0-9]\.[0-9]+)',
        ]
        for pattern in coord_patterns:
            cm = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if cm:
                parsed = _parse_coords(f"{cm.group(1)}, {cm.group(2)}")
                if parsed:
                    lat, lon = parsed
                    logger.info(f"Found coords in HTML for {url[-30:]}: {lat}, {lon}")
                    break

        logger.info(f"Scraped {url[-40:]}: image={'yes' if image else 'no'}, lat={lat}, address={address or 'none'}")
        return {"image": image, "lat": lat, "lon": lon, "address": address}

    except httpx.TimeoutException as e:
        logger.warning(f"Timeout scraping {url}: {e}")
        return {}
    except httpx.ConnectError as e:
        logger.warning(f"Connection error scraping {url}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error scraping {url}: {e}")
        return {}


async def _tavily_search_one(query: str) -> list:
    """Run one Tavily query, return results list."""
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
        logger.warning(f"Tavily error for '{query[:40]}': {e}")
        return []


async def _fetch_from_web(city: str, pool: asyncpg.Pool) -> list[dict]:
    """Fetch objects via multiple parallel Tavily queries. Returns only objects with location."""
    if _tavily_client is None:
        raise RuntimeError("search not initialized — call init_search() first")

    queries = [t.format(city=city) for t in SEARCH_TEMPLATES]
    all_results_lists = await asyncio.gather(*[_tavily_search_one(q) for q in queries])
    all_results = [r for sublist in all_results_lists for r in sublist]
    logger.info(f"Total raw results for {city}: {len(all_results)}")

    objects = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    seen_coords: set[tuple[float, float]] = set()

    for r in all_results:
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")

        if url in seen_urls:
            continue
        seen_urls.add(url)

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

        # Fallback 1: address from Tavily content snippet
        if lat is None and not address:
            addr_match = re.search(
                r'(?:ул\.|улица|пер\.|проспект|шоссе|бульвар|площадь)\s+[\w\s]+,?\s*(?:д\.?\s*\d+[\w/]*)?',
                content, re.IGNORECASE
            )
            if addr_match:
                address = re.sub(r'\s+', ' ', addr_match.group(0)).strip()

        # Fallback 2: Nominatim geocoding
        if lat is None and not address:
            coords = await _get_coords_nominatim(name, city)
            if coords:
                lat, lon = coords

        if lat is None and not address:
            logger.info(f"Skipping '{name}' — no location data")
            continue

        # In-batch coord dedup
        if lat is not None and lon is not None:
            coord_key = (round(lat, 3), round(lon, 3))
            if coord_key in seen_coords:
                logger.info(f"Skipping '{name}' — in-batch duplicate coords")
                continue
            seen_coords.add(coord_key)

        # DB coord dedup
        if await is_duplicate(pool, city, lat, lon):
            logger.info(f"Skipping '{name}' — duplicate within 100m")
            continue

        objects.append({
            "name": name,
            "lat": lat,
            "lon": lon,
            "address": address,
            "source_name": "Urban3P",
            "image": image,
            "osm_id": "t_" + hashlib.md5(url.encode()).hexdigest()[:16],
        })
        seen_names.add(norm)

    # Also fetch from OpenStreetMap for cities where we have bbox
    osm_objects = await _fetch_from_osm(city)
    logger.info(f"OSM returned {len(osm_objects)} raw objects for {city}")

    for obj in osm_objects:
        lat = obj["lat"]
        lon = obj["lon"]
        norm = _normalize_name(obj["name"])

        if norm in seen_names:
            continue
        if not obj["name"]:
            continue

        coord_key = (round(lat, 3), round(lon, 3))
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        if await is_duplicate(pool, city, lat, lon):
            continue

        objects.append(obj)
        seen_names.add(norm)

    logger.info(f"Total fetched {len(objects)} objects with location for {city}")
    return objects


async def search_objects(
    pool: asyncpg.Pool,
    obj_type: str,
    city: str,
    shown_ids: set[int],
) -> list[dict]:
    """Main entry point. Returns up to 3 objects not yet shown to user."""
    # Check if archive needs refreshing
    last_fetched = await get_city_last_fetched(pool, city)
    needs_refresh = (
        last_fetched is None
        or (datetime.now(timezone.utc) - last_fetched).days >= CACHE_REFRESH_DAYS
    )

    # Also refresh if DB is empty for this city (previous fetch may have yielded 0 objects)
    if not needs_refresh:
        existing = await pool.fetchval("SELECT COUNT(*) FROM objects WHERE city = $1", city)
        if existing == 0:
            logger.info(f"City {city} has 0 objects in archive — forcing refresh")
            needs_refresh = True

    if needs_refresh:
        logger.info(f"Refreshing archive for {city} (last: {last_fetched})")
        try:
            new_objects = await _fetch_from_web(city, pool)
            if new_objects:
                saved = await save_objects(pool, city, new_objects)
                logger.info(f"Added {saved} new objects to archive for {city}")
                await update_city_fetched(pool, city)  # only mark fetched if we got objects
            else:
                logger.warning(f"Fetch returned 0 objects for {city} — not updating fetch timestamp")
        except Exception as e:
            logger.error(f"Failed to fetch from web for {city}: {e}")
            # Fall through to DB — return whatever is cached

    # Get from archive — strictly respect shown_ids (no auto-reset)
    return await get_objects(pool, city, shown_ids, limit=3)
