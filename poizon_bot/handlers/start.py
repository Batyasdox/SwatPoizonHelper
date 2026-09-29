"""
handlers/start.py — Команда /start, онбординг нового пользователя и главное меню.

Логика:
 - Если юзера нет в БД -> просим ввести стартовый курс юаня (BotStates.waiting_for_first_rate),
   после ввода сохраняем курс и сразу показываем Главное Меню.
 - Если юзер есть -> сразу показываем Главное Меню с инлайн-кнопками.
"""

from aiogram import F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.markdown import hbold

import database
from handlers import BotStates, router


def parse_number(raw_text: str) -> float:
    """
    Очищает строку от мусора ('¥', 'руб', 'р', 'Р', пробелы) и приводит к float.
    При некорректном вводе бросает ValueError — вызывающий код ловит его через try-except.
    """
    cleaned = raw_text.strip()
    for trash in ("¥", "руб", "р", "Р", " ", "\u00a0"):
        cleaned = cleaned.replace(trash, "")
    # Поддержка запятой как десятичного разделителя
    cleaned = cleaned.replace(",", ".")
    if not cleaned:
        raise ValueError("Пустое число")
    return float(cleaned)


async def show_main_menu(message: Message) -> None:
    """Отправляет Главное Меню с инлайн-кнопками."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧮 Начать расчёт", callback_data="menu_calculate")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        ]
    )
    await message.answer(
        f"{hbold('Главное меню')}\n\n"
        f"Выберите действие:",
        reply_markup=kb,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Обработка /start: проверка пользователя в БД и выдача меню/онбординга."""
    user_id = message.from_user.id
    await state.clear()

    if database.is_user_exists(user_id):
        await show_main_menu(message)
    else:
        await message.answer(
            "👋 Привет! Прежде чем начать расчёты, пожалуйста, введите ваш "
            "актуальный курс юаня к рублю (например: 13.35):"
        )
        await state.set_state(BotStates.waiting_for_first_rate)


@router.message(BotStates.waiting_for_first_rate, F.text)
async def save_first_rate(message: Message, state: FSMContext) -> None:
    """Приём первого курса юаня от нового пользователя."""
    try:
        new_rate = parse_number(message.text)
        if new_rate <= 0:
            raise ValueError("Курс должен быть больше нуля")
    except ValueError:
        await message.answer("❌ Ошибка! Пожалуйста, введите корректное число.")
        return  # Состояние НЕ сбрасываем — ждём повторного ввода

    database.update_user_rate(message.from_user.id, new_rate)
    await state.clear()

    await message.answer(
        f"✅ Отлично! Ваш курс юаня сохранён: <b>{new_rate:.2f} ₽</b> за 1 ¥."
    )
    await message.answer("Теперь можно пользоваться ботом:")
    await show_main_menu(message)


@router.callback_query(F.data == "go_to_main_menu")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню по кнопке 'Назад'."""
    await state.clear()
    await callback.message.edit_text("Возвращаемся в главное меню 👇")
    await show_main_menu(callback.message)
    await callback.answer()
