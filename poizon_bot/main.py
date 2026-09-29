"""
main.py — Точка входа: создание бота, Dispatcher, подключение роутеров и запуск.

Запуск из папки poizon_bot:
    python main.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN
from database import init_db
from handlers import router


async def main() -> None:
    """Инициализация и запуск long-polling бота."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Подключаем единый роутер со всеми хэндлерами (start, settings, calculator)
    dp.include_router(router)

    # Инициализация SQLite выполняется на старте запуска бота
    dp.startup.register(init_db)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
