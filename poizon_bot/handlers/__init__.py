"""
handlers/__init__.py — Единый Router и класс состояний FSM для всех хэндлеров бота.

Все модули (start, settings, calculator) импортируют отсюда `router` и `BotStates`,
поэтому состояние и маршруты общие для всего диалога.
"""

from aiogram import Router
from aiogram.fsm.state import State, StatesGroup


class BotStates(StatesGroup):
    """Все состояния диалогов бота."""

    waiting_for_first_rate = State()   # Первый запуск: ввод стартового курса
    waiting_for_rate = State()         # Изменение курса из настроек
    choosing_type = State()            # Выбор типа заказа (Байер / Карго)
    waiting_for_price = State()        # Ввод цены (руб для Байера, юани для Карго)
    waiting_for_shipping = State()     # Ввод стоимости доставки в рублях
    waiting_for_model_name = State()   # Ввод названия модели для ИИ-анализа


router = Router()

# Импорт модулей с хэндлерами в конце файла, чтобы они зарегистрировались
# на общем `router`. Импортирующие модули (main.py) достаточно подключат router.
from handlers import start, settings, calculator  # noqa: E402,F401

__all__ = ["router", "BotStates"]
