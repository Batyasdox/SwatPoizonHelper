"""
config.py — Токены, ключи API и константы проекта.

Сюда вписываются реальные credentials перед запуском бота.
Никогда не коммитьте настоящие токены в публичный репозиторий!
"""

# Токен Telegram-бота, полученный у @BotFather
BOT_TOKEN = "8840186453:AAH7taH0Gg85cxFgAVgUt1lK8JXECyT04SE"

# Клиентские credentials (ClientID + ClientSecret) для GigaChat API от Сбера,
# в формате "ClientID_ClientSecret" из личного кабинета developers.sber.ru
GIGACHAT_CREDENTIALS = "01a0ed3d-e2dc-7370-8531-083598da521e_96661a08-938f-4c06-a453-c88018cef4a3"

# Название модели GigaChat, которую будем использовать для вердиктов.
# Можно поменять на "GigaChat-Pro" или "GigaChat-Max", если они есть в вашем тарифе.
GIGACHAT_MODEL = "GigaChat"

# ВАЖНО: значение scope ОБЯЗАТЕЛЬНО должно точно совпадать с тем, что указано
# в личном кабинете Sber ID (developers.sber.ru -> ваш проект -> GigaAPI ->
# карточка ключа -> блок "Спецификация доступа"). Именно из этого блока
# скопируйте строку и вставьте сюда. Ошибочный/неподдерживаемый scope —
# единственная причина ошибки OAuth "scope data format invalid".
# Частые варианты:
#   GIGACHTAPI                    — стандартный scope для GigaChat;
#   GIGACHAT_API_PERS             — персональный ("Индивидуальный") режим;
#   GIGACHAT_API_B2B              — корпоративный режим;
#   GIGACHAT_API_CORP_FQBK        и т.п. — прочие корпоративные режимы.
GIGACHAT_SCOPE = "GIGACHTAPI"

# Реальные рабочие эндпоинты GigaChat API (developers.sber.ru / GigaChat API docs):
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"  # Авторизация (OAuth)
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"  # Запросы (Chat Completions)

# Дефолтный курс юаня к рублю (используется, если у пользователя ещё ничего сохранено)
DEFAULT_YUAN_RATE = 13.35

# Имя файла базы данных SQLite
DB_FILENAME = "users_v2.db"
