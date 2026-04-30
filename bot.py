# Главный файл Telegram-бота по поиску заброшек, руфов и бомбарей
# Получает: сообщения от пользователей в Telegram
# Отдаёт: результаты поиска с названием, адресом, описанием и фото

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
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


class Registration(StatesGroup):
    waiting_name = State()
    waiting_city = State()


class SearchByName(StatesGroup):
    waiting_query = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏚️ Заброшка"), KeyboardButton(text="🏗️ Крыша")],
            [KeyboardButton(text="⛏️ Бомбарь"), KeyboardButton(text="🔍 Поиск по названию")],
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user:
        await message.answer(
            f"С возвращением, {user['name']}! Город: {user['city']}.\nВыбирай:",
            reply_markup=main_keyboard(),
        )
    else:
        await message.answer(
            "Привет! Я помогу найти заброшки, крыши и бомбари.\n\nКак тебя зовут?",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Registration.waiting_name)


@dp.message(Registration.waiting_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(f"Норм, {message.text.strip()}. В каком городе ищем?")
    await state.set_state(Registration.waiting_city)


@dp.message(Registration.waiting_city)
async def process_city(message: Message, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    city = message.text.strip()
    await save_user(message.from_user.id, name, city)
    await state.clear()
    await message.answer(
        f"Готово! Имя: {name}, город: {city}.\nВыбирай что ищешь:",
        reply_markup=main_keyboard(),
    )


async def send_objects(message: Message, obj_type: str, type_name: str, city: str):
    await message.answer(f"Ищу {type_name} в {city}... Подожди немного 🔍")

    objects = await search_objects(obj_type, city)

    if not objects:
        await message.answer("Ничего не нашёл. Попробуй позже или измени город.")
        return

    for i, obj in enumerate(objects, 1):
        name = obj.get("name", "Без названия")
        address = obj.get("address", "адрес неизвестен")
        description = obj.get("description", "")
        source = obj.get("source", "")
        image = obj.get("image", "")

        if image:
            try:
                await message.answer_photo(
                    photo=image,
                    caption=f"📸 Фото из поиска",
                )
            except Exception:
                pass

        text = (
            f"<b>{i}. {name}</b>\n"
            f"📍 {address}\n\n"
            f"{description}\n\n"
            f"🔗 {source}"
        )
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.message(F.text == "🏚️ Заброшка")
async def handle_zabroshka(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await send_objects(message, "zabroshka", "заброшки", user["city"])


@dp.message(F.text == "🏗️ Крыша")
async def handle_roof(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await send_objects(message, "roof", "крыши", user["city"])


@dp.message(F.text == "⛏️ Бомбарь")
async def handle_digger(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await send_objects(message, "digger", "бомбари", user["city"])


@dp.message(F.text == "🔍 Поиск по названию")
async def handle_search_prompt(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — напиши /start")
        return
    await message.answer("Напиши название объекта:")
    await state.set_state(SearchByName.waiting_query)


@dp.message(SearchByName.waiting_query)
async def process_search_by_name(message: Message, state: FSMContext):
    query = message.text.strip()
    await state.clear()

    user = await get_user(message.from_user.id)
    await message.answer(f"Ищу '{query}'...")

    result = await search_by_name(query, user["city"])

    if not result or result.get("not_found"):
        await message.answer(f"Ничего не нашёл по запросу '{query}'.")
        return

    name = result.get("name", query)
    images = result.get("images", [])

    for img_url in images[:2]:
        try:
            await message.answer_photo(
                photo=img_url,
                caption="📸 Фото из поиска",
            )
        except Exception:
            pass

    text = (
        f"<b>{name}</b>\n"
        f"📍 {result.get('address', 'адрес неизвестен')}\n\n"
        f"{result.get('description', '')}\n\n"
        f"🔗 {result.get('source', '')}"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
