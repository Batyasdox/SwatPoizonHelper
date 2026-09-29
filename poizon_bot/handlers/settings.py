"""
handlers/settings.py — Экран настроек: показ текущего курса юаня и его изменение.

Логика:
 - "⚙️ Настройки" (callback menu_settings) -> показываем текущий курс из БД + кнопки.
 - "✏️ Изменить курс юаня" (callback set_custom_rate) -> состояние BotStates.waiting_for_rate.
 - Ввод нового числа -> валидация через try-except на float -> update_user_rate().
"""

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database
from handlers import BotStates, router
from handlers.start import parse_number


@router.callback_query(F.data == "menu_settings")
async def show_settings(callback: CallbackQuery) -> None:
    """Показ настроек пользователя: текущий курс юаня и кнопки управления."""
    user_id = callback.from_user.id
    current_rate = database.get_user_rate(user_id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить курс юаня", callback_data="set_custom_rate")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="go_to_main_menu")],
        ]
    )

    await callback.message.edit_text(
        f"⚙️ <b>Настройки</b>\n\n"
        f"Ваш текущий курс юаня: <b>{current_rate:.2f} ₽</b> за 1 ¥\n\n"
        f"Измените курс, если он устарел:",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "set_custom_rate")
async def ask_new_rate(callback: CallbackQuery, state: FSMContext) -> None:
    """Переводим пользователя в состояние ожидания нового курса."""
    await state.set_state(BotStates.waiting_for_rate)
    await callback.message.answer(
        "Введите новый курс юаня к рублю (например: 13.35):"
    )
    await callback.answer()


@router.message(StateFilter(BotStates.waiting_for_rate), F.text)
async def apply_new_rate(message: Message, state: FSMContext) -> None:
    """Приём и сохранение нового курса с валидацией ввода."""
    try:
        new_rate = parse_number(message.text)
        if new_rate <= 0:
            raise ValueError("Курс должен быть больше нуля")
    except ValueError:
        await message.answer("❌ Ошибка! Пожалуйста, введите корректное число.")
        return  # Состояние НЕ сбрасываем — ждём повторного ввода

    database.update_user_rate(message.from_user.id, new_rate)
    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="go_to_main_menu")],
        ]
    )
    await message.answer(
        f"✅ Курс обновлён! Теперь расчёты идут по курсу: <b>{new_rate:.2f} ₽</b> за 1 ¥.",
        reply_markup=kb,
    )
