"""
handlers/calculator.py — Основная бизнес-логика расчёта стоимости заказов Poizon.

Сценарии:
 1) "🙋‍♂️ Покупка через Байера": байер называет цену СРАЗУ в РУБЛЯХ (его комиссия и
    курс уже внутри). Бот запрашивает цену в рублях, доставка тоже в рублях.
    Итог = Цена_Байера_Руб + Доставка_Руб.

 2) "🚛 Только Карго (сам выкупаю)": пользователь выкупает сам, поэтому бот
    запрашивает цену в ЮАНЯХ (¥) и конвертирует по личному курсу из БД.
    Итог = (Цена_Юани * Курс_Из_БД) + Доставка_Руб.

Финал: РЕАЛЬНЫЙ поиск цены модели в магазинах РФ (fetch_real_rf_price,
бесплатный парсинг DuckDuckGo) -> чек. Если цену найти не удалось (rf_price == 0),
в чеке пишется "⚠️ Не удалось найти модель в магазинах РФ", а нейронке
передаётся фраза "В магазинах РФ цена неизвестна, проанализируй только выгоду
рублевой цены с Poizon".

ИИ-вердикт GigaChat добавляется в чек ТОЛЬКО если при запуске бота тест API
прошёл успешно (ai_state.is_ai_available() == True). Если нейронка недоступна —
чек формируется без ИИ-блока, только математика.
"""

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import ai_state
import database
from api.gigachat import fetch_real_rf_price, get_gigachat_verdict
from handlers import BotStates, router
from handlers.start import parse_number, show_main_menu


@router.callback_query(F.data == "menu_calculate")
async def choose_order_type(callback: CallbackQuery, state: FSMContext) -> None:
    """После 'Начать расчёт' предлагаем выбрать тип заказа."""
    await state.set_state(BotStates.choosing_type)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🙋‍♂️ Покупка через Байера", callback_data="type_buyer")],
            [InlineKeyboardButton(text="🚛 Только Карго (сам выкупаю)", callback_data="type_cargo")],
        ]
    )
    await callback.message.edit_text("Выберите тип вашего заказа:", reply_markup=kb)
    await callback.answer()


@router.callback_query(StateFilter(BotStates.choosing_type), F.data.in_({"type_buyer", "type_cargo"}))
async def ask_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Сохраняем тип заказа и просим цену (руб для байера, юани для карго)."""
    order_type = "buyer" if callback.data == "type_buyer" else "cargo"
    await state.update_data(order_type=order_type)
    await state.set_state(BotStates.waiting_for_price)

    if order_type == "buyer":
        text = (
            "💰 Шаг 1/3 — Цена\n\n"
            "Введите стоимость пары в РУБЛЯХ (которую вам озвучил Байер "
            "с учетом его комиссии):"
        )
    else:
        text = (
            "💰 Шаг 1/3 — Цена\n\n"
            "Введите чистую цену товара на Poizon в юанях (¥). "
            "Если есть доставка по Китаю, прибавьте её:"
        )
    await callback.message.answer(text)
    await callback.answer()


@router.message(StateFilter(BotStates.waiting_for_price), F.text)
async def save_price(message: Message, state: FSMContext) -> None:
    """Шаг 1: принимаем цену, валидируем и переходим к доставке."""
    try:
        price = parse_number(message.text)
        if price < 0:
            raise ValueError("Отрицательная цена")
    except ValueError:
        await message.answer("❌ Ошибка! Пожалуйста, введите корректное число.")
        return  # Состояние НЕ сбрасываем

    data = await state.get_data()
    order_type = data.get("order_type", "cargo")

    await state.update_data(price=price)
    await state.set_state(BotStates.waiting_for_shipping)

    if order_type == "buyer":
        text = (
            "🚚 Шаг 2/3 — Доставка\n\n"
            "Введите стоимость доставки до вашего города в рублях (если она не "
            "включена в стоимость Байера, иначе введите 0):"
        )
    else:
        text = (
            "🚚 Шаг 2/3 — Доставка\n\n"
            "Введите стоимость международной доставки Карго в рублях "
            "(за вес, упаковку и страховку):"
        )
    await message.answer(text)


@router.message(StateFilter(BotStates.waiting_for_shipping), F.text)
async def save_shipping(message: Message, state: FSMContext) -> None:
    """Шаг 2: принимаем доставку в рублях и просим название модели."""
    try:
        shipping = parse_number(message.text)
        if shipping < 0:
            raise ValueError("Отрицательная доставка")
    except ValueError:
        await message.answer("❌ Ошибка! Пожалуйста, введите корректное число.")
        return  # Состояние НЕ сбрасываем

    await state.update_data(shipping=shipping)
    await state.set_state(BotStates.waiting_for_model_name)

    await message.answer(
        "🤖 Шаг 3/3 — ИИ-анализ\n\n"
        "Напишите точное название модели кроссовков для ИИ-анализа "
        "(например: Nike Air Force 1 Low):"
    )


@router.message(StateFilter(BotStates.waiting_for_model_name), F.text)
async def finish_calculation(message: Message, state: FSMContext) -> None:
    """Шаг 3 + Финал: считаем итог, ищем реальную цену РФ и печатаем чек."""
    model_name = message.text.strip()
    if not model_name:
        await message.answer("❌ Ошибка! Название модели не может быть пустым. Попробуйте ещё раз:")
        return

    await state.update_data(model_name=model_name)
    data = await state.get_data()

    order_type = data.get("order_type", "cargo")
    price = float(data.get("price", 0.0))
    shipping = float(data.get("shipping", 0.0))
    user_id = message.from_user.id
    rate = database.get_user_rate(user_id)

    if ai_state.is_ai_available():
        await message.answer(
            "⏳ Считаю заказ, ищу реальную цену в магазинах РФ "
            "и отправляю данные в GigaChat..."
        )
    else:
        await message.answer("⏳ Считаю заказ и ищу реальную цену в магазинах РФ...")

    # --- Расчёт итоговой стоимости ---
    if order_type == "buyer":
        # Байер уже всё посчитал в рублях: конвертация по курсу НЕ нужна
        item_rub = price
        total_rub = price + shipping
        type_label = "🙋‍♂️ Покупка через Байера"
        price_line = f"• Стоимость у Байера: <b>{price:,.2f} ₽</b>".replace(",", " ")
        shipping_line = f"• Доставка до города: <b>{shipping:,.2f} ₽</b>".replace(",", " ")
        extra_lines = ""
    else:
        # Карго: пользователь выкупает сам -> юани конвертим по личному курсу из БД
        item_rub = price * rate
        total_rub = item_rub + shipping
        type_label = "🚛 Только Карго (сам выкупаю)"
        price_line = (
            f"• Цена на Poizon: <b>{price:,.2f} ¥</b> × курс {rate:.2f} ₽ = "
            f"<b>{item_rub:,.2f} ₽</b>"
        ).replace(",", " ")
        shipping_line = f"• Международное Карго: <b>{shipping:,.2f} ₽</b>".replace(",", " ")
        extra_lines = f"• Использован ваш личный курс: <b>{rate:.2f} ₽ / 1 ¥</b>\n"

    # --- РЕАЛЬНЫЙ поиск цены такой же пары в магазинах РФ ---
    rf_price = await fetch_real_rf_price(model_name)
    rf_found = rf_price > 0
    savings = (rf_price - total_rub) if rf_found else 0.0

    # --- ИИ-вердикт от GigaChat: только если тест при запуске прошёл успешно ---
    verdict_block = ""
    if ai_state.is_ai_available():
        try:
            # Если rf_price == 0, внутри get_gigachat_verdict нейронке
            # передаётся фраза: "В магазинах РФ цена неизвестна, проанализируй
            # только выгоду рублевой цены с Poizon".
            verdict = await get_gigachat_verdict(model_name, total_rub, rf_price)
            verdict_block = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 <b>ИИ-вердикт от GigaChat (Сбер):</b>\n{verdict}\n"
            )
        except Exception:  # noqa: BLE001 — нейронка отвалилась в процессе работы
            # Раз ошибка — больше ИИ не используем (до перезапуска бота)
            ai_state.set_ai_available(False)

    # --- Блок вывода (ИИ или математика), плюс строка про цену РФ ---
    if rf_found:
        rf_line = (
            f"🇷🇺 Цена такой же пары в магазинах РФ (найдено в сети): "
            f"~{rf_price:,.2f} ₽".replace(",", " ") + "\n"
            + f"💎 Ваша выгода: <b>{savings:,.2f} ₽</b>\n".replace(",", " ")
        )
    else:
        rf_line = "⚠️ Не удалось найти модель в магазинах РФ\n"

    if not verdict_block:
        # ИИ недоступен — добавляем простой математический вывод без нейронки.
        if rf_found:
            savings_pct = (savings / rf_price * 100) if rf_price > 0 else 0.0
            if savings > 0:
                math_block = (
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💡 <b>Вывод:</b> покупка выгоднее покупки в РФ примерно на "
                    f"{savings:,.2f} ₽ ({savings_pct:.1f}%)."
                ).replace(",", " ") + "\n"
            else:
                math_block = (
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💡 <b>Вывод:</b> покупка дороже рыночной цены в РФ примерно на "
                    f"{-savings:,.2f} ₽ ({-savings_pct:.1f}%), возможно выгоднее "
                    f"взять пару локально."
                ).replace(",", " ") + "\n"
        else:
            math_block = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <b>Вывод:</b> сравнить с ценами в РФ не удалось — модель "
                f"не найдена в открытых магазинах. Оценивайте итог самостоятельно.\n"
            )
        verdict_block = math_block

    check_text = (
        f"🧾 <b>ДЕТАЛЬНЫЙ ЧЕК ЗАКАЗА POIZON</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👟 Модель: <b>{model_name}</b>\n"
        f"📦 Тип заказа: {type_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Расходы:</b>\n"
        f"{price_line}\n"
        f"{shipping_line}\n"
        f"{extra_lines}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>ИТОГО: {total_rub:,.2f} ₽</b>\n".replace(",", " ")
        + rf_line
        + f"{verdict_block}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Вернуться в главное меню", callback_data="go_to_main_menu")],
        ]
    )

    await state.clear()
    await message.answer(check_text, reply_markup=kb, parse_mode="HTML")
