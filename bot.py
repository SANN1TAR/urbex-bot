# Telegram-бот по поиску заброшенных объектов. Интерфейс: тиндер — по одному объекту.

import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from database import get_user, init_db, save_user, get_all_cached_cities, get_cache_age_days, CACHE_TTL_DAYS
from search import search_by_name, search_objects, _fetch_and_cache, _counters

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

_shown_global: dict[int, set] = {}

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
        [KeyboardButton(text="🔍 Поиск по названию"), KeyboardButton(text="🏙️ Сменить город")],
    ],
    resize_keyboard=True,
)

NAV_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="➡️ Следующий", callback_data="next"),
    InlineKeyboardButton(text="🔄 Заново", callback_data="restart"),
]])

MENU_BUTTONS = {"🏚️ Заброшка", "🔍 Поиск по названию", "🏙️ Сменить город"}
OBJ_TYPE_NAMES = {"zabroshka": "заброшки"}

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
}


def _resolve_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


def _format_obj(obj: dict) -> str:
    coords = obj.get("coords", "")
    address = obj.get("address", "")
    if coords:
        location = f"\n🗺 {coords}"
    elif address:
        location = f"\n📍 {address}"
    else:
        location = ""

    security = obj.get("security", "")
    sec_line = f"\n🔒 {security}" if security and security.lower() not in ("неизвестно", "") else ""

    return (
        f"<b>{obj.get('name', 'Без названия')}</b>"
        f"{location}"
        f"{sec_line}"
    )


class Reg(StatesGroup):
    city = State()


class Search(StatesGroup):
    query = State()


class Browsing(StatesGroup):
    active = State()


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user:
        await message.answer(
            f"О, вернулся. Город {user['city']} — поехали, чё надо?",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(DISCLAIMER, parse_mode="HTML")
        await message.answer(
            "Здорово, ёпта. Я тут из рода экскурсоводов — знаю почти все дыры в городе.\n\n"
            "Знаю заброшенные объекты почти в каждом городе.\n\n"
            "Из какого ты города? Пиши как угодно — МСК, ЕКБ, Питер, полное название.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Reg.city)


@dp.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    uid = message.from_user.id
    _shown_global.pop(uid, None)
    _counters.clear()
    await state.clear()
    await message.answer("Перезагрузил. Всё с нуля — поехали.", reply_markup=MAIN_KB)


@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await message.answer("Сначала напиши город:", reply_markup=ReplyKeyboardRemove())
        return
    city = _resolve_city(message.text)
    await save_user(message.from_user.id, city)
    await state.clear()
    await message.answer(VPN_NOTE, parse_mode="HTML")
    await message.answer(f"{city} — знаю там пару мест. Чё ищешь?", reply_markup=MAIN_KB)


async def _send_one(message: Message, obj: dict):
    image = obj.get("image", "")
    if image:
        try:
            await message.answer_photo(photo=image)
        except Exception:
            pass
    await message.answer(
        _format_obj(obj),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=NAV_KB,
    )
    await message.answer(STALE_NOTE)


async def start_browsing(message: Message, state: FSMContext, obj_type: str, city: str):
    uid = message.from_user.id
    shown = _shown_global.get(uid, set())

    await message.answer(f"Ща пробью {OBJ_TYPE_NAMES[obj_type]} в {city}... 🔍")
    objects = await search_objects(obj_type, city, shown)

    if not objects:
        await message.answer("Попробуй позже или смени город.", reply_markup=MAIN_KB)
        return

    new_names = {o.get("name", "") for o in objects}
    combined = shown | new_names
    _shown_global[uid] = combined if len(combined) <= 30 else new_names

    await state.set_state(Browsing.active)
    await state.update_data(
        obj_type=obj_type,
        city=city,
        cache=json.dumps([dict(o) for o in objects], ensure_ascii=False),
        idx=0,
    )
    await _send_one(message, objects[0])


@dp.callback_query(F.data == "next", Browsing.active)
async def handle_next(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = await state.get_data()
    cache = json.loads(data["cache"])
    idx = data["idx"] + 1

    if idx < len(cache):
        await state.update_data(idx=idx)
        await _send_one(callback.message, cache[idx])
        return

    uid = callback.from_user.id
    shown = _shown_global.get(uid, set())
    await callback.message.answer("Ищу ещё... 🔍")
    objects = await search_objects(data["obj_type"], data["city"], shown)

    if not objects:
        await callback.message.answer("Попробуй позже или смени город.", reply_markup=MAIN_KB)
        await state.clear()
        return

    new_names = {o.get("name", "") for o in objects}
    combined = shown | new_names
    _shown_global[uid] = combined if len(combined) <= 30 else new_names

    await state.update_data(
        cache=json.dumps([dict(o) for o in objects], ensure_ascii=False),
        idx=0,
    )
    await _send_one(callback.message, objects[0])


@dp.callback_query(F.data == "restart", Browsing.active)
async def handle_restart(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    data = await state.get_data()
    uid = callback.from_user.id
    _shown_global.pop(uid, None)
    await state.clear()
    await start_browsing(callback.message, state, data["obj_type"], data["city"])


async def _require_user(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
    return user


@dp.message(F.text == "🏚️ Заброшка")
async def handle_zabroshka(message: Message, state: FSMContext):
    user = await _require_user(message)
    if not user:
        return
    await state.clear()
    await start_browsing(message, state, "zabroshka", user["city"])


@dp.message(F.text == "🏙️ Сменить город")
async def handle_change_city(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Из какого города теперь ищем?", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.city)


@dp.message(F.text == "🔍 Поиск по названию")
async def handle_search_prompt(message: Message, state: FSMContext):
    user = await _require_user(message)
    if not user:
        return
    await state.clear()
    await message.answer("Называй объект, пробью что знаю:")
    await state.set_state(Search.query)


@dp.message(Search.query)
async def handle_search_query(message: Message, state: FSMContext):
    if message.text in MENU_BUTTONS:
        await state.clear()
        await message.answer("Выбирай:", reply_markup=MAIN_KB)
        return
    query = message.text.strip()
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer(f"Пробиваю '{query}'...")

    result = await search_by_name(query, user["city"])
    if not result or result.get("not_found"):
        await message.answer(f"Не нашёл ничего по '{query}'.")
        return

    image = result.get("image", "")
    if image:
        try:
            await message.answer_photo(photo=image)
        except Exception:
            pass
    await message.answer(_format_obj(result), parse_mode="HTML", disable_web_page_preview=True)


async def _refresh_cache_loop():
    while True:
        await asyncio.sleep(24 * 3600)
        cities = await get_all_cached_cities()
        for city in cities:
            age = await get_cache_age_days(city)
            if age > CACHE_TTL_DAYS:
                logging.info(f"Фоновое обновление кеша: {city}")
                await _fetch_and_cache(city)


async def main():
    await init_db()
    asyncio.create_task(_refresh_cache_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
