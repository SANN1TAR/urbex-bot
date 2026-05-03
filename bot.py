import asyncio
import json
import logging

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from config import get_config
from database import get_user, init_db, save_user, mark_shown, get_shown_ids, reset_shown
from search import search_objects, init_search

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
# In-memory guard: prevents duplicate responses when user double-taps fast
_loading_users: set[int] = set()

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

STALE_NOTE = "⚡ Не серчай, инфа может быть устаревшей. Перед вылазкой перепроверь."

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏚️ Заброшка")],
        [KeyboardButton(text="🏙️ Сменить город")],
    ],
    resize_keyboard=True,
)

BROWSING_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏚️ Заброшка")],
        [KeyboardButton(text="✖️ Завершить поиск")],
    ],
    resize_keyboard=True,
)

MENU_BUTTONS = {"🏚️ Заброшка", "🏙️ Сменить город", "✖️ Завершить поиск"}
OBJ_TYPE = "zabroshka"
OBJ_TYPE_NAMES = {OBJ_TYPE: "заброшки"}

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
🏚️ <b>Заброшка</b> — начать поиск. Во время сессии: следующий объект.
✖️ <b>Завершить поиск</b> — закончить сессию, вернуться в меню.
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
async def cmd_start(message: Message, state: FSMContext):
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
            "Здорово, ёпта. Я тут из рода экскурсоводов — знаю почти все дыры в городе.\n\n"
            "Из какого ты города? Пиши как угодно — МСК, МО, Питер, полное название.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Reg.city)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    await reset_shown(_pool, message.from_user.id)
    await state.clear()
    await message.answer("Перезагрузил. Ищу заново — поехали.", reply_markup=MAIN_KB)


@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await message.answer("Сначала напиши город:", reply_markup=ReplyKeyboardRemove())
        return
    city = _resolve_city(message.text)
    await save_user(_pool, message.from_user.id, city)
    await state.clear()
    await message.answer(VPN_NOTE, parse_mode="HTML")
    await message.answer(f"{city} — знаю там пару мест. Чё ищешь?", reply_markup=MAIN_KB)


async def _send_one(message: Message, obj: dict):
    image = obj.get("image", "")
    if image:
        try:
            await message.answer_photo(photo=image)
        except Exception as e:
            logger.debug(f"Failed to send photo: {e}")
    try:
        await message.answer(
            _format_obj(obj),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await message.answer(STALE_NOTE)
    except Exception as e:
        logger.error(f"Failed to send object card: {e}")


async def _show_next(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    if uid in _loading_users:
        return
    _loading_users.add(uid)
    try:
        data = await state.get_data()
        cache = json.loads(data["cache"])
        idx = data["idx"] + 1

        if idx < len(cache):
            await state.update_data(idx=idx)
            await _send_one(message, cache[idx])
            return

        shown_ids = await get_shown_ids(_pool, uid)
        await message.answer("Ищу ещё... 🔍")
        objects = await search_objects(_pool, data["obj_type"], data["city"], shown_ids)

        if not objects:
            await state.clear()
            await message.answer(
                "Показал всё что знаю 🏁\n\n"
                "Нажми 🏚️ <b>Заброшка</b> чтобы начать заново.",
                parse_mode="HTML",
                reply_markup=MAIN_KB,
            )
            return

        new_ids = [o["id"] for o in objects]
        await mark_shown(_pool, uid, new_ids)
        await state.update_data(cache=_serialize_cache(objects), idx=0)
        await _send_one(message, objects[0])
    finally:
        _loading_users.discard(uid)


async def _start_session(message: Message, state: FSMContext, obj_type: str, city: str):
    uid = message.from_user.id
    await reset_shown(_pool, uid)

    await message.answer(f"Ща пробью {OBJ_TYPE_NAMES[obj_type]} в {city}... 🔍")
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


@dp.message(F.text == "🏚️ Заброшка", Browsing.active)
async def handle_next_in_session(message: Message, state: FSMContext):
    await _show_next(message, state)


@dp.message(F.text == "🏚️ Заброшка")
async def handle_zabroshka(message: Message, state: FSMContext):
    user = await _require_user(message)
    if not user:
        return
    await state.clear()
    await _start_session(message, state, OBJ_TYPE, user["city"])


@dp.message(F.text == "✖️ Завершить поиск")
async def handle_stop_browsing(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Завершил. Когда захочешь — нажми 🏚️ Заброшка.", reply_markup=MAIN_KB)


@dp.message(F.text == "🏙️ Сменить город")
async def handle_change_city(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Из какого города теперь ищем?", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.city)


async def _refresh_cache_loop(pool: asyncpg.Pool) -> None:
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
                    logger.error(f"Background refresh failed for {city}: {e}")
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cache refresh loop error: {e}")


async def main():
    global _pool
    cfg = get_config()
    init_search(cfg.tavily_api_key)
    _pool = await asyncpg.create_pool(cfg.database_url, min_size=2, max_size=10)
    await init_db(_pool)
    bot = Bot(token=cfg.telegram_token)
    task = asyncio.create_task(_refresh_cache_loop(_pool))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _pool.close()


if __name__ == "__main__":
    asyncio.run(main())
