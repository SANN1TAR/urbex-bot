# Главный файл Telegram-бота по поиску заброшек и крыш
# Получает: сообщения от пользователей в Telegram
# Отдаёт: результаты поиска с координатами, описанием и фото

import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from database import get_user, init_db, save_user
from search import search_by_name, search_objects

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

# Глобальная память показанных объектов до рестарта бота
_shown_global: dict[int, set] = {}

DISCLAIMER = (
    "⚠️ <b>Стоп, читай сюда.</b>\n\n"
    "Этот бот даёт инфу про заброшки и крыши в твоём городе.\n\n"
    "Бот и его создатель <b>не несут никакой ответственности</b> за то, что с тобой случится — "
    "поймает охрана, полиция, провалишься, упадёшь, получишь штраф или ещё что.\n\n"
    "Всё что ты делаешь — на свой страх и риск. Сам полез — сам отвечаешь.\n\n"
    "Удачных вылазок. 🚪"
)

VPN_NOTE = (
    "🔒 <b>Важно:</b> часть ссылок ведёт в инстаграм и ютуб. "
    "Если не открываются — врубай VPN."
)

STALE_NOTE = (
    "⚡ Не серчай, но часть инфы может быть устаревшей — я за этим не слежу. "
    "Перед вылазкой лучше перепроверь сам."
)

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏚️ Заброшка"), KeyboardButton(text="🏗️ Крыша")],
        [KeyboardButton(text="🔍 Поиск по названию"), KeyboardButton(text="🏙️ Сменить город")],
    ],
    resize_keyboard=True,
)

MORE_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🔥 Ещё 3", callback_data="more"),
    InlineKeyboardButton(text="✅ Достаточно", callback_data="enough"),
]])

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

OBJ_TYPE_NAMES = {"zabroshka": "заброшки", "roof": "крыши"}


def _resolve_city(raw: str) -> str:
    return CITY_ALIASES.get(raw.lower().strip(), raw.strip().title())


def _format_obj(num: int, obj: dict) -> str:
    coords = obj.get("coords", "")
    address = obj.get("address", "")
    location = f"\n🗺 {coords}" if coords else (f"\n📍 {address}" if address else "")

    date = obj.get("published_date", "")
    date_line = f"\n📅 {date[:10]}" if date else ""

    security = obj.get("security", "")
    sec_line = f"\n🔒 {security}" if security and security.lower() != "неизвестно" else ""

    prefix = f"{num}. " if num > 0 else ""
    return (
        f"<b>{prefix}{obj.get('name', 'Без названия')}</b>"
        f"{location}\n\n"
        f"{obj.get('description', '')}"
        f"{sec_line}"
        f"{date_line}"
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
            "Может, тебе понадобятся крыши. Может, заброшки.\n\n"
            "Из какого ты города? Пиши как угодно — МСК, ЕКБ, Питер, полное название.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Reg.city)


@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    city = _resolve_city(message.text)
    await save_user(message.from_user.id, city)
    await state.clear()
    await message.answer(VPN_NOTE, parse_mode="HTML")
    await message.answer(f"{city} — знаю там пару мест. Чё ищешь?", reply_markup=MAIN_KB)


async def _send_results(message: Message, objects: list):
    for i, obj in enumerate(objects, 1):
        image = obj.get("image", "")
        if image:
            try:
                await message.answer_photo(photo=image)
            except Exception:
                pass
        await message.answer(_format_obj(i, obj), parse_mode="HTML", disable_web_page_preview=True)


async def send_objects(message: Message, state: FSMContext, obj_type: str, city: str):
    uid = message.from_user.id
    shown = _shown_global.get(uid, set())

    await message.answer(f"Ща пробью {OBJ_TYPE_NAMES[obj_type]} в {city}... 🔍")
    objects = await search_objects(obj_type, city, shown)

    if not objects:
        await message.answer("Пусто. Либо нет инфы, либо всё снесли. Попробуй позже.")
        return

    new_names = {obj.get("name", "") for obj in objects}
    _shown_global[uid] = shown | new_names

    await _send_results(message, objects)
    await message.answer(STALE_NOTE)
    await state.set_state(Browsing.active)
    await state.update_data(obj_type=obj_type, city=city)
    await message.answer("Ещё поискать или хватит?", reply_markup=MORE_KB)


@dp.callback_query(F.data == "more", Browsing.active)
async def handle_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    obj_type = data.get("obj_type")
    city = data.get("city")
    uid = callback.from_user.id
    shown = _shown_global.get(uid, set())

    await callback.message.answer(f"Ищу ещё {OBJ_TYPE_NAMES.get(obj_type)}... 🔍")
    objects = await search_objects(obj_type, city, shown)

    if not objects:
        await callback.message.answer("Больше ничего не нашёл, братан.")
        await state.clear()
        return

    _shown_global[uid] = shown | {obj.get("name", "") for obj in objects}
    await _send_results(callback.message, objects)
    await callback.message.answer(STALE_NOTE)
    await callback.message.answer("Ещё поискать или хватит?", reply_markup=MORE_KB)


@dp.callback_query(F.data == "enough", Browsing.active)
async def handle_enough(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Ок, завязали. Чё ещё надо?", reply_markup=MAIN_KB)


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
    await send_objects(message, state, "zabroshka", user["city"])


@dp.message(F.text == "🏗️ Крыша")
async def handle_roof(message: Message, state: FSMContext):
    user = await _require_user(message)
    if not user:
        return
    await state.clear()
    await send_objects(message, state, "roof", user["city"])


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
    query = message.text.strip()
    await state.clear()
    user = await get_user(message.from_user.id)
    await message.answer(f"Пробиваю '{query}'...")

    result = await search_by_name(query, user["city"])
    if not result or result.get("not_found"):
        await message.answer(f"Хрен знает что за '{query}'. Не нашёл ничего.")
        return

    image = result.get("image", "")
    if image:
        try:
            await message.answer_photo(photo=image)
        except Exception:
            pass

    await message.answer(_format_obj(0, result), parse_mode="HTML", disable_web_page_preview=True)


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
