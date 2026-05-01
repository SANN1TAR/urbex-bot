# Работа с базой данных: пользователи + кеш заброшенных объектов по городам
# Получает: telegram_id, город, объекты
# Отдаёт: данные пользователя, список объектов для города

import json
import aiosqlite

DB_PATH = "urbex_bot.db"
CACHE_TTL_DAYS = 7


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                osm_id TEXT,
                name TEXT NOT NULL,
                coords TEXT,
                address TEXT,
                description TEXT,
                security TEXT,
                source_name TEXT,
                image TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(city, osm_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_objects_city ON objects(city)")
        await db.commit()


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, city FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"telegram_id": row[0], "city": row[1]}
            return None


async def save_user(telegram_id: int, city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (telegram_id, city) VALUES (?, ?)",
            (telegram_id, city)
        )
        await db.commit()


async def get_objects(city: str, shown: set, limit: int = 3) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT name, coords, address, description, security, source_name, image
            FROM objects
            WHERE city = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (city, limit * 5),
        ) as cursor:
            rows = await cursor.fetchall()

    result = []
    for row in rows:
        name = row[0]
        if name in shown:
            continue
        result.append({
            "name": name,
            "coords": row[1] or "",
            "address": row[2] or "",
            "description": row[3] or "",
            "security": row[4] or "",
            "source_name": row[5] or "",
            "image": row[6] or "",
            "published_date": "",
        })
        if len(result) >= limit:
            break
    return result


async def save_objects(city: str, objects: list):
    async with aiosqlite.connect(DB_PATH) as db:
        for obj in objects:
            osm_id = obj.get("osm_id") or obj.get("name")
            await db.execute(
                """
                INSERT OR IGNORE INTO objects
                    (city, osm_id, name, coords, address, description, security, source_name, image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    city,
                    osm_id,
                    obj.get("name", ""),
                    obj.get("coords", ""),
                    obj.get("address", ""),
                    obj.get("description", ""),
                    obj.get("security", ""),
                    obj.get("source_name", ""),
                    obj.get("image", ""),
                ),
            )
        await db.commit()


async def get_city_count(city: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM objects WHERE city = ?", (city,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_cache_age_days(city: str) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT (julianday('now') - julianday(MAX(created_at)))
            FROM objects WHERE city = ?
            """,
            (city,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] is not None:
                return float(row[0])
    return 999.0


async def get_all_cached_cities() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT DISTINCT city FROM objects"
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]
