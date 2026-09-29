"""
main.py — Точка входа: создание бота, Dispatcher, подключение роутеров и запуск.

Важно: при старте бота выполняется тестовый запрос к API GigaChat:
 - если API работает — ИИ-вердикты от нейронки включаются (ai_state.AI_AVAILABLE = True);
 - если при тесте ошибка — ИИ-аналитика просто НЕ используется, бот работает
   только с математическими расчётами.

Запуск из папки poizon_bot:
    python main.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from api.gigachat import test_gigachat_api
from config import BOT_TOKEN
from database import init_db
from handlers import router
import ai_state


async def check_ai_on_startup() -> None:
    """
    Тест доступности API GigaChat при запуске бота.
    При успехе включаем ИИ-аналитику, при ошибке — оставляем её выключенной.
    """
    ok, info = await test_gigachat_api()
    if ok:
        ai_state.set_ai_available(True)
        logging.info("✅ GigaChat доступен. ИИ-вердикты ВКЛЮЧЕНЫ. Ответ теста: %s", info)
    else:
        ai_state.set_ai_available(False)
        logging.warning("❌ GigaChat недоступен (%s). ИИ-аналитика ОТКЛЮЧЕНА, "
                        "бот будет работать только с математическими расчётами.", info)


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

    # Тест нейронки при запуске: ок -> используем ИИ, ошибка -> не используем
    dp.startup.register(check_ai_on_startup)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")
