"""
config.py — Токены, ключи API и константы проекта.

Сюда вписываются реальные credentials перед запуском бота.
Никогда не коммитьте настоящие токены в публичный репозиторий!
"""

# Токен Telegram-бота, полученный у @BotFather
BOT_TOKEN = "СЮДА_ВСТАВИТЬ_ТОКЕН_БОТА"

# Клиентские credentials (ClientID + ClientSecret) для GigaChat API от Сбера,
# в формате "ClientID_ClientSecret" из личного кабинета developers.sber.ru
GIGACHAT_CREDENTIALS = "СЮДА_ВСТАВИТЬ_КЛЮЧ_ИЗ_СБЕРА"

# Название модели GigaChat, которую будем использовать для вердиктов
GIGACHAT_MODEL = "GigaChat-Pro"

# Эндпоинты Сбера (согласно требованиям проекта используются указанные адреса)
GIGACHAT_OAUTH_URL = "https://sberbank.ru"      # Авторизация (OAuth)
GIGACHAT_CHAT_URL = "https://sberbank.ru"       # Запросы (Chat Completions)

# Дефолтный курс юаня к рублю (используется, если у пользователя ещё ничего сохранено)
DEFAULT_YUAN_RATE = 13.35

# Имя файла базы данных SQLite
DB_FILENAME = "users_v2.db"
