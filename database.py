import asyncpg
from datetime import datetime, timezone


CACHE_REFRESH_DAYS = 7


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                name TEXT PRIMARY KEY,
                last_fetched_at TIMESTAMP WITH TIME ZONE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                city        TEXT NOT NULL,
                registered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id          SERIAL PRIMARY KEY,
                city        TEXT NOT NULL,
                osm_id      TEXT NOT NULL,
                name        TEXT NOT NULL,
                lat         REAL,
                lon         REAL,
                address     TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT 'Urban3P',
                image       TEXT NOT NULL DEFAULT '',
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(city, osm_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_city ON objects(city)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_latlon ON objects(lat, lon)"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_shown (
                user_id   BIGINT NOT NULL,
                object_id INTEGER NOT NULL,
                shown_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                PRIMARY KEY (user_id, object_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_shown_user ON user_shown(user_id)"
        )


async def get_user(pool: asyncpg.Pool, telegram_id: int) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT telegram_id, city FROM users WHERE telegram_id = $1",
            telegram_id
        )
    if row:
        return {"telegram_id": row["telegram_id"], "city": row["city"]}
    return None


async def save_user(pool: asyncpg.Pool, telegram_id: int, city: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (telegram_id, city)
               VALUES ($1, $2)
               ON CONFLICT (telegram_id) DO UPDATE SET city = $2""",
            telegram_id, city
        )


async def get_objects(
    pool: asyncpg.Pool,
    city: str,
    shown_ids: set[int],
    limit: int = 3,
) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, name, lat, lon, address, image, source_name
               FROM objects
               WHERE city = $1
               ORDER BY RANDOM()
               LIMIT $2""",
            city, limit * 5,
        )
    result = []
    for row in rows:
        if row["id"] in shown_ids:
            continue
        result.append({
            "id": row["id"],
            "name": row["name"],
            "lat": row["lat"],
            "lon": row["lon"],
            "address": row["address"] or "",
            "image": row["image"] or "",
            "source_name": row["source_name"] or "",
        })
        if len(result) >= limit:
            break
    return result


async def save_objects(pool: asyncpg.Pool, city: str, objects: list[dict]) -> int:
    saved = 0
    async with pool.acquire() as conn:
        for obj in objects:
            result = await conn.execute(
                """INSERT INTO objects
                       (city, osm_id, name, lat, lon, address, source_name, image)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (city, osm_id) DO NOTHING""",
                city,
                obj.get("osm_id") or obj.get("name"),
                obj.get("name", ""),
                obj.get("lat"),
                obj.get("lon"),
                obj.get("address", ""),
                obj.get("source_name", "Urban3P"),
                obj.get("image", ""),
            )
            # asyncpg returns command tag like "INSERT 0 1" (inserted) or "INSERT 0 0" (conflict)
            if result.endswith(" 1"):
                saved += 1
    return saved


async def get_city_last_fetched(pool: asyncpg.Pool, city: str) -> datetime | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_fetched_at FROM cities WHERE name = $1", city
        )
    if row and row["last_fetched_at"]:
        return row["last_fetched_at"]
    return None


async def update_city_fetched(pool: asyncpg.Pool, city: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO cities (name, last_fetched_at)
               VALUES ($1, NOW())
               ON CONFLICT (name) DO UPDATE SET last_fetched_at = NOW()""",
            city
        )


async def mark_shown(pool: asyncpg.Pool, user_id: int, object_ids: list[int]) -> None:
    if not object_ids:
        return
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO user_shown (user_id, object_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            [(user_id, oid) for oid in object_ids],
        )


async def get_shown_ids(pool: asyncpg.Pool, user_id: int) -> set[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT object_id FROM user_shown WHERE user_id = $1", user_id
        )
    return {r["object_id"] for r in rows}


async def reset_shown(pool: asyncpg.Pool, user_id: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_shown WHERE user_id = $1", user_id
        )


async def is_duplicate(
    pool: asyncpg.Pool, city: str, lat: float | None, lon: float | None
) -> bool:
    if lat is None or lon is None:
        return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id FROM objects
               WHERE city = $1
               AND lat BETWEEN $2 - 0.001 AND $2 + 0.001
               AND lon BETWEEN $3 - 0.001 AND $3 + 0.001
               LIMIT 1""",
            city, lat, lon
        )
    return row is not None
