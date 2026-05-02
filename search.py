# Search for abandoned objects via Tavily + urban3p.ru scraping
# Returns objects with verified location (lat/lon or address) only

import asyncio
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

SOURCES = [
    "site:urban3p.ru",
    "site:urban3p.com",
]

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

        # Coordinates via /onmap POST — use same domain as source URL
        lat, lon = None, None
        m = re.search(r'(urban3p\.(ru|com))/object(\d+)', url)
        if m:
            domain = m.group(1)
            obj_id = m.group(3)
            try:
                async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "Mozilla/5.0"}) as client:
                    r = await client.post(
                        f"https://{domain}/object{obj_id}/onmap",
                        data={"submitted": "смотреть на карте"},
                    )
                    ll = re.search(r'([0-9]+\.[0-9]{4,})[,\s]+([0-9]+\.[0-9]{4,})', r.text)
                    if ll:
                        parsed = _parse_coords(f"{ll.group(1)}, {ll.group(2)}")
                        if parsed:
                            lat, lon = parsed
                    logger.info(f"/onmap {domain} object{obj_id}: coords={'yes' if lat else 'no'}")
            except httpx.TimeoutException as e:
                logger.debug(f"Timeout on /onmap for {url}: {e}")
            except httpx.ConnectError as e:
                logger.debug(f"ConnectError on /onmap for {url}: {e}")
            except Exception as e:
                logger.warning(f"Unexpected error on /onmap for {url}: {e}")

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


async def _fetch_from_web(city: str, pool: asyncpg.Pool) -> list[dict]:
    """Fetch new objects from urban3p.ru via Tavily. Returns only objects with location."""
    if _tavily_client is None:
        raise RuntimeError("search not initialized — call init_search() first")

    objects = []
    seen_names: set[str] = set()

    for source in SOURCES:
        try:
            data = await asyncio.to_thread(
                lambda s=source: _tavily_client.search(
                    f"заброшенная {city} {s}",
                    max_results=10,
                    include_images=True,
                    search_depth="advanced",
                )
            )
        except Exception as e:
            logger.warning(f"Tavily search error ({source}): {e}")
            continue

        results = data.get("results", [])
        logger.info(f"Tavily source '{source}': {len(results)} results for {city}")

        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")

            # Only individual object pages
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

            # Fallback: extract address from Tavily content snippet
            if lat is None and not address:
                addr_match = re.search(
                    r'(?:ул\.|улица|пер\.|проспект|шоссе|бульвар|площадь)\s+[\w\s]+,?\s*(?:д\.?\s*\d+[\w/]*)?',
                    content, re.IGNORECASE
                )
                if addr_match:
                    address = re.sub(r'\s+', ' ', addr_match.group(0)).strip()

            # Location filter: skip if no lat/lon AND no address
            if lat is None and not address:
                logger.info(f"Skipping '{name}' — no location data")
                continue

            # Coordinate deduplication
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
                "osm_id": name,  # use name as unique key (urban3p has no stable ID in URL context)
            })
            seen_names.add(norm)

        if len(objects) >= 20:
            break

    logger.info(f"Fetched {len(objects)} objects with location for {city}")
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

    # Get from archive
    objects = await get_objects(pool, city, shown_ids, limit=3)
    if not objects:
        # Shown set exhausted — reset and try again
        objects = await get_objects(pool, city, set(), limit=3)
    return objects
