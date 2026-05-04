import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ErrorEvent, KeyboardButton, Message, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, TelegramObject,
)

from config import get_config
from database import (
    get_user, init_db, save_user, mark_shown, get_shown_ids, reset_shown,
    get_ungeocoded_objects, update_object_coords,
)
from search import search_objects, init_search, close_http_client, geocode_nominatim

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

DISCLAIMER = (
    "⚠️ <b>Стоп, читай сюда.</b>\n\n"
    "Этот бот даёт инфу про заброшенные объекты в твоём городе.\n\n"
    "Бот и его создатель <b>не несут никакой ответственности</b> за то, что с тобой случится — "
    "поймает охрана, полиция, провалишься, упадёшь, получишь штраф или ещё что.\n\n"
    "Всё что ты делаешь — на свой страх и риск. Сам полез — сам отвечаешь.\n\n"
    "Удачных вылазок. 🚪"
)

VPN_NOTE = (
    "🔒 <b>Важно:</b> часть ссылок ведёт в инстаграм и ютуб. "
    "Если не открываются — врубай VPN."
)

STALE_NOTE = "⚡ Братишка, архив старый — инфа тоже. Перед вылазкой перепроверь местечко на всякий случай, ради своей же безопасности."

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Начать поиск")],
        [KeyboardButton(text="🏙️ Сменить город")],
    ],
    resize_keyboard=True,
)

BROWSING_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➡️ Следующая")],
        [KeyboardButton(text="❌ Закончить поиск")],
    ],
    resize_keyboard=True,
)

MENU_BUTTONS = {"🔍 Начать поиск", "🏙️ Сменить город", "➡️ Следующая", "❌ Закончить поиск"}
OBJ_TYPE = "zabroshka"
OBJ_TYPE_NAMES = {OBJ_TYPE: "заброшки"}

_CITY_MAX_LEN = 100
_CITY_RE = re.compile(r'^[\w\s\-\.]+$', re.UNICODE)

CITY_ALIASES = {
    "мск": "Москва", "москва": "Москва",
    "спб": "Санкт-Петербург", "спт": "Санкт-Петербург", "питер": "Санкт-Петербург",
    "екб": "Екатеринбург", "екат": "Екатеринбург",
    "нск": "Новосибирск", "новосиб": "Новосибирск",
    "ннов": "Нижний Новгород", "нн": "Нижний Новгород",
    "крд": "Краснодар", "казань": "Казань",
    "чел": "Челябинск", "уфа": "Уфа", "омск": "Омск", "самара": "Самара",
    "рнд": "Ростов-на-Дону", "ростов": "Ростов-на-Дону",
    "вгд": "Волгоград", "пермь": "Пермь",
    "алм": "Алматы", "алматы": "Алматы", "алма-ата": "Алматы",
    "аст": "Астана", "астана": "Астана", "нур": "Астана",
    "мо": "Московская область", "подмосковье": "Московская область",
    "московская область": "Московская область",
}

dp = Dispatcher(storage=MemoryStorage())


# --- Throttling middleware (replaces _loading_users set) ---

class ThrottlingMiddleware(BaseMiddleware):
    """Silently drops messages from the same user within `rate` seconds."""

    def __init__(self, rate: float = 0.5) -> None:
        self.rate = rate
        self._last_call: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            text = getattr(event, "text", "") or ""
            if not text.startswith("/"):
                now = time.monotonic()
                if now - self._last_call.get(user.id, 0) < self.rate:
                    return
                self._last_call[user.id] = now
                # Evict oldest 20% when dict grows too large (LRU-style, no global clear)
                if len(self._last_call) > 10_000:
                    oldest = sorted(self._last_call.items(), key=lambda x: x[1])
                    for uid, _ in oldest[:2_000]:
                        del self._last_call[uid]
        return await handler(event, data)


dp.message.middleware(ThrottlingMiddleware(rate=0.5))


# --- Global error handler ---

@dp.errors()
async def global_error_handler(event: ErrorEvent) -> None:
    exc = event.exception
    if isinstance(exc, TelegramRetryAfter):
        logger.warning(f"Telegram flood control: retry after {exc.retry_after}s")
        await asyncio.sleep(exc.retry_after)
    else:
        logger.exception(
            "Unhandled error on update %s: %s",
            getattr(event.update, "update_id", "?"),
            exc,
        )


# --- Helpers ---

def _resolve_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


def _format_obj(obj: dict) -> str:
    lat = obj.get("lat")
    lon = obj.get("lon")
    address = obj.get("address", "")
    if lat is not None and lon is not None:
        location = f"\n🗺 {lat:.4f}, {lon:.4f}"
    elif address:
        location = f"\n📍 {address}"
    else:
        location = ""
    return f"<b>{obj.get('name', 'Без названия')}</b>{location}"


def _serialize_cache(objects: list[dict]) -> str:
    return json.dumps([dict(o) for o in objects], ensure_ascii=False)


class Reg(StatesGroup):
    city = State()


class Browsing(StatesGroup):
    active = State()


async def _require_user(message: Message) -> dict | None:
    user = await get_user(_pool, message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
    return user


HELP_TEXT = """
<b>🏚 Как пользоваться ботом</b>

<b>Кнопки:</b>
🔍 <b>Начать поиск</b> — начать поиск заброшек в твоём городе.
➡️ <b>Следующая</b> — следующий объект во время поиска.
❌ <b>Закончить поиск</b> — выйти из поиска, вернуться в меню.
🏙️ <b>Сменить город</b> — изменить город поиска.

<b>Команды:</b>
/start — начало работы
/restart — сброс и поиск новых объектов
/help — это сообщение

<b>Почему нет адреса или координат?</b>
Не у всех объектов есть публичные координаты. Если локации нет — такой объект бот не показывает.

<b>⚠️ Важно:</b> вся информация из открытых источников и может быть устаревшей. Перед выездом проверяй сам.
"""


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(_pool, message.from_user.id)
    if user:
        await message.answer(
            f"О, вернулся. Город {user['city']} — поехали, чё надо?",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(DISCLAIMER, parse_mode="HTML")
        await message.answer(
            "Здарово, я твой гид по забросам 🚪\n\n"
            "Ты скажи откуда ты, а я пока поищу что у меня в архиве завалялось.\n\n"
            "Пиши как угодно — МСК, МО, Питер, полное название.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Reg.city)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext) -> None:
    await reset_shown(_pool, message.from_user.id)
    await state.clear()
    await message.answer("Перезагрузил. Ищу заново — поехали.", reply_markup=MAIN_KB)


@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext) -> None:
    if message.text in MENU_BUTTONS:
        await message.answer("Сначала напиши город:", reply_markup=ReplyKeyboardRemove())
        return
    text = (message.text or "").strip()
    if not text or len(text) > _CITY_MAX_LEN or not _CITY_RE.match(text):
        await message.answer(
            "Ты из меня клоуна не строй — я и так перед тобой с этими бумагами кручусь. "
            "Нормально напиши название города.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    city = _resolve_city(text)
    await save_user(_pool, message.from_user.id, city)
    await state.clear()
    await message.answer(f"{city} — принял. Жми 🏚️ Заброшка.", reply_markup=MAIN_KB)


async def _send_one(message: Message, obj: dict) -> None:
    image = obj.get("image", "")
    if image:
        try:
            await message.answer_photo(photo=image)
        except Exception as e:
            logger.warning(f"Failed to send photo for '{obj.get('name')}': {type(e).__name__}")
    try:
        await message.answer(
            _format_obj(obj),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.answer(STALE_NOTE)
    except Exception as e:
        logger.error(f"Failed to send object card: {type(e).__name__}: {e}")


async def _show_next(message: Message, state: FSMContext) -> None:
    data = await state.get_data()

    # Guard against missing FSM state (e.g. after Railway restart with MemoryStorage)
    if not {"cache", "idx", "obj_type", "city"}.issubset(data.keys()):
        await state.clear()
        await message.answer(
            "Сессия сброшена после перезапуска. Нажми 🔍 Начать поиск чтобы начать заново.",
            reply_markup=MAIN_KB,
        )
        return

    cache = json.loads(data["cache"])
    idx = data["idx"] + 1

    if idx < len(cache):
        await state.update_data(idx=idx)
        await _send_one(message, cache[idx])
        return

    uid = message.from_user.id
    shown_ids = await get_shown_ids(_pool, uid)
    await message.answer("Ищу ещё... 🔍")
    objects = await search_objects(_pool, data["obj_type"], data["city"], shown_ids)

    if not objects:
        await state.clear()
        await message.answer(
            "Всё, что скопилось в архиве — показал 🗂\n\n"
            "Заходи позже, возможно что-то ещё появится.\n"
            "Нажми 🏚️ <b>Заброшка</b> чтобы начать заново.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    new_ids = [o["id"] for o in objects]
    await mark_shown(_pool, uid, new_ids)
    await state.update_data(cache=_serialize_cache(objects), idx=0)
    await _send_one(message, objects[0])


async def _start_session(message: Message, state: FSMContext, obj_type: str, city: str) -> None:
    uid = message.from_user.id
    await reset_shown(_pool, uid)

    await message.answer(f"{city} — знаю там пару мест. Ну что, начинаю искать... 🔍")
    objects = await search_objects(_pool, obj_type, city, set())

    if not objects:
        await state.clear()
        await message.answer(
            "Пока пусто — база пополняется. Попробуй позже или смени город.",
            reply_markup=MAIN_KB,
        )
        return

    new_ids = [o["id"] for o in objects]
    await mark_shown(_pool, uid, new_ids)

    await state.set_state(Browsing.active)
    await state.update_data(
        obj_type=obj_type,
        city=city,
        cache=_serialize_cache(objects),
        idx=0,
    )
    await message.answer("Нашёл. Листай:", reply_markup=BROWSING_KB)
    await _send_one(message, objects[0])


@dp.message(F.text == "➡️ Следующая", Browsing.active)
async def handle_next_in_session(message: Message, state: FSMContext) -> None:
    await _show_next(message, state)


@dp.message(F.text == "🔍 Начать поиск")
async def handle_zabroshka(message: Message, state: FSMContext) -> None:
    user = await _require_user(message)
    if not user:
        return
    await state.clear()
    await _start_session(message, state, OBJ_TYPE, user["city"])


@dp.message(F.text == "❌ Закончить поиск")
async def handle_stop_browsing(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Закончили. Когда захочешь — нажми 🔍 Начать поиск.", reply_markup=MAIN_KB)


@dp.message(F.text == "🏙️ Сменить город")
async def handle_change_city(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Из какого города теперь ищем?", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.city)


async def _geocode_loop(pool: asyncpg.Pool) -> None:
    """Background task: incrementally geocode catalog objects that lack coordinates.

    Runs every hour, processes 15 objects per city via Nominatim (1 req/sec).
    Over 24 h a city accumulates ~15 × 24 × 40% ≈ 144 new located objects.
    """
    error_delay = 300
    while True:
        try:
            async with pool.acquire() as conn:
                cities = [r["city"] for r in await conn.fetch("SELECT DISTINCT city FROM users")]

            for city in cities:
                objects = await get_ungeocoded_objects(pool, city, limit=15)
                geocoded = 0
                for obj in objects:
                    coords = await geocode_nominatim(obj["name"], city)
                    if coords:
                        await update_object_coords(pool, obj["id"], *coords)
                        geocoded += 1
                if geocoded:
                    logger.info(f"Geocode loop: +{geocoded} coords for {city}")

            error_delay = 300
            await asyncio.sleep(3600)  # run every hour
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Geocode loop error (retry in {error_delay}s): {type(e).__name__}")
            await asyncio.sleep(error_delay)
            error_delay = min(error_delay * 2, 3600)


async def _refresh_cache_loop(pool: asyncpg.Pool) -> None:
    error_delay = 300
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT DISTINCT city FROM users")
            cities = [r["city"] for r in rows]
            for city in cities:
                try:
                    logger.info(f"Background refresh for {city}")
                    await search_objects(pool, OBJ_TYPE, city, set())
                except Exception as e:
                    logger.error(
                        f"Background refresh failed for {city}: {type(e).__name__}"
                    )
            error_delay = 300
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(
                f"Cache refresh loop error (retry in {error_delay}s): {type(e).__name__}"
            )
            await asyncio.sleep(error_delay)
            error_delay = min(error_delay * 2, 3600)


async def _healthcheck_handler(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        await asyncio.wait_for(reader.read(1024), timeout=5.0)
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"\r\n"
            b"ok"
        )
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    global _pool
    try:
        cfg = get_config()
    except EnvironmentError as e:
        logger.critical(str(e))
        raise SystemExit(1)

    init_search(cfg.tavily_api_key)

    # statement_cache_size=0 is required when using Supabase PgBouncer (port 6543)
    # to prevent DuplicatePreparedStatementError in transaction pooling mode
    _pool = await asyncpg.create_pool(
        cfg.database_url,
        min_size=2,
        max_size=8,
        statement_cache_size=0,
        command_timeout=30,
    )
    await init_db(_pool)

    bot = Bot(token=cfg.telegram_token)

    port = int(os.getenv("PORT", "8080"))
    hc_server = await asyncio.start_server(_healthcheck_handler, "0.0.0.0", port)
    logger.info(f"Healthcheck listening on :{port}")

    cache_task = asyncio.create_task(_refresh_cache_loop(_pool))
    geocode_task = asyncio.create_task(_geocode_loop(_pool))
    try:
        await dp.start_polling(bot)
    finally:
        cache_task.cancel()
        geocode_task.cancel()
        await asyncio.gather(cache_task, geocode_task, return_exceptions=True)
        hc_server.close()
        await hc_server.wait_closed()
        await bot.session.close()
        await close_http_client()
        await _pool.close()


if __name__ == "__main__":
    asyncio.run(main())
