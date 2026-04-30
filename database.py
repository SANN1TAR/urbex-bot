# Работа с базой данных: хранит пользователей (telegram_id, имя, город)
# Получает: telegram_id, имя, город
# Отдаёт: данные пользователя или None

import aiosqlite

DB_PATH = "urbex_bot.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id, name, city FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"telegram_id": row[0], "name": row[1], "city": row[2]}
            return None


async def save_user(telegram_id: int, name: str, city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (telegram_id, name, city) VALUES (?, ?, ?)",
            (telegram_id, name, city)
        )
        await db.commit()
