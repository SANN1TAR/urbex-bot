# Работа с базой данных: хранит пользователей (telegram_id, город)
# Получает: telegram_id, город
# Отдаёт: данные пользователя или None

import aiosqlite

DB_PATH = "urbex_bot.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
