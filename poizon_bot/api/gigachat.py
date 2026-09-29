"""
api/gigachat.py — Взаимодействие с API GigaChat от Сбера + реальный поиск цен в РФ.

Здесь реализованы:
 - тест доступности API при запуске бота (test_gigachat_api);
 - получение Access Token по OAuth (с уникальным RqUID через uuid.uuid4());
 - отправка промпта в Chat-эндпоинт и получение ИИ-вердикта;
 - РЕАЛЬНЫЙ (бесплатный, без платных API-ключей) поиск цены модели в магазинах
   России через парсинг HTML-выдачи DuckDuckGo (fetch_real_rf_price).

Логика использования ИИ: если тест при запуске прошёл — нейронка используется
в чеках; если тест упал — ИИ-аналитика просто НЕ используется (флаг
ai_state.AI_AVAILABLE остаётся False). get_gigachat_verdict при любой ошибке
бросает исключение наружу, чтобы вызывающий код мог сам отключить ИИ.

ВАЖНО: рабочие эндпоинты GigaChat задаются в config.py
(GIGACHAT_OAUTH_URL / GIGACHAT_CHAT_URL). Все post-запросы выполняются
с ssl=False, как требуется проектом.
"""

import asyncio
import base64
import json
import logging
import re
import uuid
from urllib.parse import quote_plus

import aiohttp

from config import (
    GIGACHAT_CREDENTIALS,
    GIGACHAT_CHAT_URL,
    GIGACHAT_MODEL,
    GIGACHAT_OAUTH_URL,
)

logger = logging.getLogger("root")

# Список candidate scope'ов, которые бот перебирает при авторизации.
# Разные типы ключей в кабинете Сбера требуют разные значения scope:
#   - GIGACHTAPI                  — классический «Механизм вызова GigaChat»;
#   - GIGACHAT_API_PERS           — персональный режим доступа;
#   - GIGACHAT_API_CORP           — корпоративный режим;
#   - GIGACHAT_API_CORP_RTM       — корпоративный RTM;
#   - GIGACHAT_API_STATE          / GIGACHAT_API_GOV — гос/муниципальные.
# Бот сам подберёт подходящий — ничего вручную вписывать не нужно.
SCOPE_CANDIDATES = [
    "GIGACHTAPI",
    "GIGACHAT_API_PERS",
    "GIGACHAT_API_PERSONAL",
    "GIGACHAT_API_CORP",
    "GIGACHAT_API_CORP_RTM",
    "GIGACHAT_API_STATE",
    "GIGACHAT_API_GOV",
]

# Кэш: успешно подобранный scope запоминаем, чтобы не долбить OAuth подряд.
_resolved_scope: str | None = None


def _get_basic_auth_header(credentials: str) -> str:
    """
    Формирует заголовок Authorization: Basic ... для OAuth-запроса.

    Принимает credentials в любом из форматов:
      - "ClientID_ClientSecret"  (разделитель '_', как просит Сбер);
      - "ClientID:ClientSecret"  (разделитель ':').
    Нормализует к "ClientID:ClientSecret" и кодирует в base64.
    """
    credentials = credentials.strip()
    # У множественных '_' в UUID нет, поэтому первый '_' — почти наверняка
    # разделитель ClientID/ClientSecret. Если его нет — пробуем ':' .
    if "_" in credentials:
        client_id, _, client_secret = credentials.partition("_")
    elif ":" in credentials:
        client_id, _, client_secret = credentials.partition(":")
    else:
        raise ValueError(
            "GIGACHAT_CREDENTIALS должен быть в формате 'ClientID_ClientSecret'"
        )
    normalized = f"{client_id.strip()}:{client_secret.strip()}"
    token = base64.b64encode(normalized.encode("utf-8")).decode("utf-8")
    return f"Basic {token}"


async def _request_token_with_scope(
    session: aiohttp.ClientSession, credentials: str, scope: str
) -> tuple[int, str]:
    """
    Делает ОДИН OAuth-запрос с указанным scope.
    Возвращает (HTTP-статус, текст ответа). Исключений не бросает.
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Accept": "application/json",
        "Authorization": _get_basic_auth_header(credentials),
        "RqUID": str(uuid.uuid4()),  # обязательный уникальный ID запроса (uuid4)
    }
    payload = {"scope": scope}

    async with session.post(
        GIGACHAT_OAUTH_URL, data=payload, headers=headers, ssl=False
    ) as resp:
        return resp.status, await resp.text()


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    """
    Получает временный Access Token у OAuth-сервера Сбера.

    Автоматически перебирает список SCOPE_CANDIDATES, пока не получит
    успешный ответ (HTTP 200 + access_token). Успешно подобранный scope
    кэшируется в модульной переменной _resolved_scope.
    """
    global _resolved_scope

    candidates = SCOPE_CANDIDATES
    # Если ранее уже подобрали рабочий scope — пробуем его первым.
    if _resolved_scope and _resolved_scope in candidates:
        candidates = [_resolved_scope] + [s for s in candidates if s != _resolved_scope]

    last_error = ""
    for scope in candidates:
        status, body_text = await _request_token_with_scope(
            session, GIGACHAT_CREDENTIALS, scope
        )
        if status == 200:
            try:
                data = json.loads(body_text)
            except ValueError:
                last_error = f"scope={scope}: ответ не JSON: {body_text[:200]}"
                continue
            access_token = data.get("access_token")
            if access_token:
                if _resolved_scope != scope:
                    _resolved_scope = scope
                    logger.info("✅ GigaChat OAuth: подобран рабочий scope '%s'", scope)
                return access_token
            last_error = f"scope={scope}: в ответе нет access_token: {body_text[:200]}"
        else:
            last_error = f"scope={scope}: HTTP {status}: {body_text[:200]}"
            # Если ошибка явно про неверные credentials (401), смысла крутить
            # остальные scope нет — сервер аутентификацию не прошёл.
            if status in (401, 403):
                break
        await asyncio.sleep(0.2)  # маленькая пауза между попытками

    raise RuntimeError(f"OAuth Сбера отверг все варианты scope. Последняя ошибка: {last_error}")


async def get_gigachat_verdict(model_name: str, total_poizon_rub: float, rf_price_rub: float) -> str:
    """
    Асинхронная функция: запрашивает у GigaChat ёмкий вердикт на русском языке
    о выгодности покупки кроссовок.

    Args:
        model_name:       точное название модели кроссовок;
        total_poizon_rub: итоговая стоимость заказа с Poizon в рублях;
        rf_price_rub:     НАЙДЕННАЯ цена такой же пары в магазинах РФ в рублях
                          (0.0 означает, что цену найти не удалось — в этом
                          случае нейронке передаётся специальная формулировка).

    Returns:
        Текст вердикта (3-4 предложения) от нейросети.

    Raises:
        Любое исключение при ошибках сети/авторизации/API пробрасывается
        наружу — вызывающий код (calculator) сам решит, что ИИ недоступен,
        отключит его флагом и сформирует чек без ИИ-вердикта.
    """
    if rf_price_rub and rf_price_rub > 0:
        price_part = (
            f"выгодна ли покупка кроссовок {model_name} за цену {total_poizon_rub:.2f} руб "
            f"по сравнению с РФ ценой {rf_price_rub:.2f} руб? "
            f"Укажи примерную выгоду или переплату в рублях и процентах."
        )
    else:
        price_part = (
            f"В магазинах РФ цена неизвестна, проанализируй только выгоду "
            f"рублевой цены с Poizon: кроссовки {model_name} стоят "
            f"{total_poizon_rub:.2f} руб под заказ. Оцени, нормальная ли это "
            f"рыночная цена для такой модели."
        )

    prompt = (
        f"Ты — эксперт по покупкам на маркетплейсе Poizon (Dewu). "
        f"Дай ёмкий вердикт на русском языке (3-4 предложения): "
        f"{price_part} "
        f"Если в названии модели есть слово 'Custom' или 'Кастом', "
        f"обязательно подчеркни эксклюзивность и уникальность такой пары, "
        f"и то, что для кастомных моделей экономия может быть вторична."
    )

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)

        chat_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        chat_payload = {
            "model": GIGACHAT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "stream": False,
        }

        async with session.post(
            GIGACHAT_CHAT_URL, json=chat_payload, headers=chat_headers, ssl=False
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"GigaChat Chat вернул HTTP {resp.status}: {body[:300]}")
            data = await resp.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"GigaChat вернул пустой ответ: {str(data)[:300]}")

    verdict = choices[0]["message"]["content"].strip()
    if not verdict:
        raise RuntimeError("GigaChat вернул пустой текст вердикта")
    return verdict


async def test_gigachat_api() -> tuple[bool, str]:
    """
    Тестовый запрос к API GigaChat, выполняется при запуске бота.

    Делает реальный (короткий) запрос через OAuth + Chat и проверяет,
    что нейронка отвечает.

    Returns:
        (True,  "текст ответа нейронки")  — если API работает, вердикт можно использовать;
        (False, "описание ошибки")        — если любая ошибка, ИИ использовать НЕЛЬЗЯ.
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            access_token = await _get_access_token(session)

            chat_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            chat_payload = {
                "model": GIGACHAT_MODEL,
                "messages": [{"role": "user", "content": "Ответь одним словом: ОК"}],
                "temperature": 0.1,
                "stream": False,
            }

            async with session.post(
                GIGACHAT_CHAT_URL, json=chat_payload, headers=chat_headers, ssl=False
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return False, f"HTTP {resp.status}: {body[:200]}"
                data = await resp.json()

        choices = data.get("choices") or []
        if not choices:
            return False, f"Пустой ответ модели: {str(data)[:200]}"

        answer = choices[0]["message"]["content"].strip()
        if not answer:
            return False, "Модель вернула пустой текст"
        return True, answer

    except Exception as exc:  # noqa: BLE001 — ловим ВСЁ: сеть, DNS, SSL, JSON, таймаут
        return False, f"{type(exc).__name__}: {exc}"


# ============================================================================
# РЕАЛЬНЫЙ ПОИСК ЦЕН В МАГАЗИНАХ РФ (без платных API-ключей)
# ============================================================================

# Фейковые User-Agent'ы: меняем их между запросами, чтобы DuckDuckGo
# не заблокила бота как «бота».
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# HTML-версии поисковой выдачи DuckDuckGo (бесплатные, без ключей).
DDG_SEARCH_URLS = [
    "https://html.duckduckgo.com/html/",
    "https://lite.duckduckgo.com/lite/",
]

# Минимальная осмысленная цена оригинальных кроссовок в РФ (меньше — мусор).
MIN_VALID_RF_PRICE = 3000.0
# Максимальная разумная планка (выше — скорее всего оптовая пачка или ошибка).
MAX_VALID_RF_PRICE = 1_500_000.0

# Регулярки для поиска цен в тексте выдачи:
#  1) "от 12 900 руб", "12900 рублей", "цена 12 900 ₽";
#  2) "₽ после числа": "12 900 ₽";
#  3) "цена: 12900" / "стоимость 12 900" — число без suffix, но после слова про цену.
PRICE_PATTERNS = [
    re.compile(r"(?:от|до|цена|стоимость)?\s*([\d][\d\s\u00a0\u2009]{3,})\s*(?:руб\.?л?е?й?|рублей?\b)", re.IGNORECASE),
    re.compile(r"([\d][\d\s\u00a0\u2009]{3,})\s*₽"),
    re.compile(r"(?:цена|стоимость|прайс)[^\d]{0,15}([\d][\d\s\u00a0\u2009]{3,})", re.IGNORECASE),
]


def _extract_prices_from_text(text: str) -> list[float]:
    """
    Вытаскивает из произвольного HTML/текста все денежные числа вида
    "от Х ХХХ руб" / "ХХХХХ ₽", фильтрует слишком маленькие (< 3000 руб,
    оригинальные кроссовки столько стоить не могут) и слишком большие.
    Возвращает список УНИКАЛЬНЫХ цен (дубликаты от разных регулярных
    выражений убираются).
    """
    found: set[float] = set()
    for pattern in PRICE_PATTERNS:
        for match in pattern.findall(text):
            digits = re.sub(r"\D", "", match)
            if not digits:
                continue
            try:
                value = float(digits)
            except ValueError:
                continue
            if MIN_VALID_RF_PRICE <= value <= MAX_VALID_RF_PRICE:
                found.add(value)
    return sorted(found)


async def _fetch_ddg_page(
    session: aiohttp.ClientSession, url: str, query: str
) -> str:
    """
    Грузит одну страницу поисковой выдачи DuckDuckGo (GET + фейковый User-Agent).
    При HTTP 202/403/429 (антибот-проверка DuckDuckGo) повторяет попытку с другим
    User-Agent'ом и растущей паузой. При любой ошибке возвращает пустую строку —
    падать здесь нельзя.
    """
    max_attempts = 5
    for attempt in range(max_attempts):
        headers = {
            "User-Agent": USER_AGENTS[(hash(query) + attempt * 7) % len(USER_AGENTS)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        try:
            async with session.get(
                url,
                params={"q": query, "kl": "ru-ru"},
                headers=headers,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    return await resp.text(errors="ignore")
                logger.warning("DuckDuckGo %s вернул HTTP %s (попытка %d/%d)",
                               url, resp.status, attempt + 1, max_attempts)
                # 202/403/429 — антибот-пауза, пробуем ещё раз с другой «личностью»
                if resp.status in (202, 403, 429):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return ""
        except Exception as exc:  # noqa: BLE001 — сеть/DNS/таймаут: считаем, что страница не найдена
            logger.warning("DuckDuckGo %s: ошибка запроса (попытка %d/%d): %s: %s",
                           url, attempt + 1, max_attempts, type(exc).__name__, exc)
            await asyncio.sleep(1.5)
    return ""


async def fetch_real_rf_price(model_name: str) -> float:
    """
    Асинхронный РЕАЛЬНЫЙ поиск цены модели в магазинах России
    (Яндекс.Маркет, СДЭК.Шоппинг, Спортмастер и др.) через бесплатный
    парсинг HTML-выдачи DuckDuckGo. Никаких платных API-ключей не требует.

    Алгоритм:
      1. Формирует поисковый запрос вида "{model_name} купить в России цена руб".
      2. GET-запросом (с фейковым User-Agent) грузит страницы html.duckduckgo.com
         и lite.duckduckgo.com.
      3. Регулярными выражениями собирает все совпадения цен ("от Х ХХХ руб",
         "ХХХХХ ₽" и т.п.).
      4. Фильтрует цены меньше 3000 руб и возвращает СРЕДНЕЕ арифметическое
         из оставшихся.

    Returns:
        Найденная средняя цена в рублях либо 0.0, если ничего не нашлось
        или произошла любая ошибка.
    """
    model_name = (model_name or "").strip()
    if not model_name:
        return 0.0

    queries = [
        f"{model_name} купить в России цена руб",
        f"{model_name} купить цена руб Яндекс Маркет",
    ]

    all_prices: list[float] = []
    try:
        connector = aiohttp.TCPConnector(ssl=False, limit=5)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=45)
        ) as session:
            for query in queries:
                for url in DDG_SEARCH_URLS:
                    page_html = await _fetch_ddg_page(session, url, query)
                    if page_html:
                        all_prices.extend(_extract_prices_from_text(page_html))
                    await asyncio.sleep(1.0)  # вежливая пауза, не спамим поисковик
                if all_prices:
                    break  # первый запрос уже дал цены — второй не нужен
    except Exception as exc:  # noqa: BLE001 — любая ошибка => возвращаем 0
        logger.warning("fetch_real_rf_price('%s'): ошибка: %s: %s",
                       model_name, type(exc).__name__, exc)
        return 0.0

    if not all_prices:
        logger.info("fetch_real_rf_price('%s'): цены не найдены", model_name)
        return 0.0

    # Отбрасываем явный мусор: считаем среднее по уникальным значениям,
    # предварительно убрав единичные выбросы ниже медианы вдвое.
    unique_prices = sorted(set(all_prices))
    if len(unique_prices) >= 3:
        median = unique_prices[len(unique_prices) // 2]
        unique_prices = [p for p in unique_prices if p >= median / 2]

    average = sum(unique_prices) / len(unique_prices)
    logger.info(
        "fetch_real_rf_price('%s'): найдено %d цен, среднее = %.2f руб",
        model_name, len(unique_prices), average,
    )
    return round(average, 2)
