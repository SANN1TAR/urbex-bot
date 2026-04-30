# Главный файл Telegram-бота по поиску заброшек, руфов и бомбарей
# Получает: сообщения от пользователей в Telegram
# Отдаёт: результаты поиска с названием, адресом, описанием

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from database import get_user, init_db, save_user
from search import search_by_name, search_objects

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
dp = Dispatcher(storage=MemoryStorage())

DISCLAIMER = (
    "⚠️ <b>Стоп, читай сюда.</b>\n\n"
    "Этот бот даёт инфу про заброшки, крыши и бомбари в твоём городе.\n\n"
    "Бот и его создатель <b>не несут никакой ответственности</b> за то, что с тобой случится — "
    "поймает охрана, полиция, провалишься, упадёшь, получишь штраф или ещё что.\n\n"
    "Всё что ты делаешь — ты делаешь на свой страх и риск. Сам полез — сам отвечаешь.\n\n"
    "Удачных вылазок. 🚪"
)

VPN_NOTE = (
    "🔒 <b>Важно:</b> часть ссылок ведёт в инстаграм и ютуб. "
    "Если у тебя они не открываются — врубай VPN, иначе половина инфы будет недоступна."
)

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏚️ Заброшка"), KeyboardButton(text="🏗️ Крыша")],
        [KeyboardButton(text="🔍 Поиск по названию")],
    ],
    resize_keyboard=True,
)

MORE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🔥 Ещё 3", callback_data="more"),
        InlineKeyboardButton(text="✅ Достаточно", callback_data="enough"),
    ]
])

OBJ_TYPE_NAMES = {
    "zabroshka": "заброшки",
    "roof": "крыши",
}


class Reg(StatesGroup):
    name = State()
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
            f"О, {user['name']}, вернулся. Город {user['city']} — поехали, чё надо?",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(DISCLAIMER, parse_mode="HTML")
        await message.answer(
            "Здорово, ёпта. Я тут из рода экскурсоводов — знаю почти все дыры в городе.\n\n"
            "Может, тебе понадобятся крыши. Может, заброшки.\n\n"
            "Ты скажи как тебя кличут, а я пока подыщу что тебе понадобится.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Reg.name)


@dp.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(f"Нормально, {message.text.strip()}. В каком городе шаришься?")
    await state.set_state(Reg.city)


@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    data = await state.get_data()
    city = message.text.strip()
    await save_user(message.from_user.id, data["name"], city)
    await state.clear()
    await message.answer(VPN_NOTE, parse_mode="HTML")
    await message.answer(
        f"{city} — знаю там пару мест. Чё ищешь?",
        reply_markup=MAIN_KB,
    )


def _date_note(pub_date: str) -> str:
    if not pub_date:
        return ""
    return f"\n📅 Опубликовано: {pub_date[:10]}"


async def send_objects(message: Message, state: FSMContext, obj_type: str, city: str):
    type_name = OBJ_TYPE_NAMES[obj_type]
    await message.answer(f"Ща пробью {type_name} в {city}... 🔍")
    objects = await search_objects(obj_type, city)

    if not objects:
        await message.answer("Пусто. Либо нет инфы, либо всё снесли. Попробуй позже.")
        return

    shown = [obj.get("name", "") for obj in objects]
    await _send_results(message, objects)

    await state.set_state(Browsing.active)
    await state.update_data(obj_type=obj_type, city=city, shown=shown)
    await message.answer("Ещё поискать или хватит?", reply_markup=MORE_KB)


async def _send_results(message: Message, objects: list):
    for i, obj in enumerate(objects, 1):
        date_note = _date_note(obj.get("published_date", ""))
        security = obj.get("security", "")
        security_line = f"\n🔒 Охрана: {security}" if security else ""

        await message.answer(
            f"<b>{i}. {obj.get('name', 'Без названия')}</b>\n"
            f"📍 {obj.get('address', 'адрес неизвестен')}\n\n"
            f"{obj.get('description', '')}"
            f"{security_line}"
            f"{date_note}\n\n"
            f"🔗 {obj.get('source', '')}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@dp.callback_query(F.data == "more", Browsing.active)
async def handle_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    data = await state.get_data()
    obj_type = data.get("obj_type")
    city = data.get("city")
    shown = data.get("shown", [])
    type_name = OBJ_TYPE_NAMES.get(obj_type, "объекты")

    await callback.message.answer(f"Ищу ещё {type_name}... 🔍")
    objects = await search_objects(obj_type, city, shown=shown)

    if not objects:
        await callback.message.answer("Больше ничего не нашёл, братан.")
        await state.clear()
        return

    shown += [obj.get("name", "") for obj in objects]
    await _send_results(callback.message, objects)
    await state.update_data(shown=shown)
    await callback.message.answer("Ещё поискать или хватит?", reply_markup=MORE_KB)


@dp.callback_query(F.data == "enough", Browsing.active)
async def handle_enough(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await callback.message.answer("Ок, завязали. Чё ещё надо?", reply_markup=MAIN_KB)


@dp.message(F.text == "🏚️ Заброшка")
async def handle_zabroshka(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await state.clear()
    await send_objects(message, state, "zabroshka", user["city"])


@dp.message(F.text == "🏗️ Крыша")
async def handle_roof(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await state.clear()
    await send_objects(message, state, "roof", user["city"])



@dp.message(F.text == "🔍 Поиск по названию")
async def handle_search_prompt(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
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

    date_note = _date_note(result.get("published_date", ""))
    await message.answer(
        f"<b>{result.get('name', query)}</b>\n"
        f"📍 {result.get('address', 'адрес неизвестен')}\n\n"
        f"{result.get('description', '')}"
        f"{date_note}\n\n"
        f"🔗 {result.get('source', '')}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
