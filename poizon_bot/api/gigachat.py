"""
api/gigachat.py — Взаимодействие с API GigaChat от Сбера + ЧЕСТНЫЙ реальный
поиск цен в магазинах России.

Здесь реализованы:
 - тест доступности API при запуске бота (test_gigachat_api);
 - получение Access Token по OAuth (с уникальным RqUID через uuid.uuid4());
 - отправка промпта в Chat-эндпоинт и получение ИИ-вердикта;
 - РЕАЛЬНЫЙ (бесплатный, без платных API-ключей) поиск цены модели ЗАДАННОГО
   РАЗМЕРА в магазинах России через парсинг HTML-выдачи DuckDuckGo
   (fetch_real_rf_price) с УМНЫМ ДИНАМИЧЕСКИМ ФИЛЬТРОМ:
     * все цены ниже total_poizon_rub * 0.85 отсекаются — это гарантированно
       убирает мусор (носки, шнурки, стельки, подделки/паль);
     * из оставшихся выбрасываются одна самая низкая и одна самая высокая цена
       (усечённое среднее), считаются только адекватные значения.

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

    raise RuntimeError(
        f"OAuth Сбера отверг все варианты scope. Последняя ошибка: {last_error}"
    )


def sanitize_verdict_text(text: str) -> str:
    """
    Очищает текст вердикта нейронки для безопасной вставки в HTML-сообщение
    Telegram (parse_mode=HTML).

    Markdown-акцент **текст** ломает HTML-разметку, поэтому двойные звёздочки
    заменяются на HTML-теги <b> и </b> ПО ОЧЕРЕДИ: первая '**' открывает
    жирный (<b>), вторая — закрывает (</b>), третья снова открывает и т.д.
    Если количество '**' нечётное и тег остался незакрытым — он докрывается.
    Одиночные '*' вычищаются полностью, а '<'/'>' экранируются, чтобы
    пользовательский ввод или текст модели не сломали разметку сообщения.
    """
    if not text:
        return ""

    text = text.replace("<", "&lt;").replace(">", "&gt;")

    result_parts: list[str] = []
    open_tag = True  # следующая пара '**' должна открыть <b>
    i = 0
    while i < len(text):
        if text[i : i + 2] == "**":
            result_parts.append("<b>" if open_tag else "</b>")
            open_tag = not open_tag
            i += 2
        else:
            result_parts.append(text[i])
            i += 1

    cleaned = "".join(result_parts)

    # Если осталась незакрытая <b> (нечётное число '**') — докрываем </b>,
    # иначе Telegram выдаст ошибку парсинга HTML.
    if cleaned.count("<b>") > cleaned.count("</b>"):
        cleaned += "</b>"

    # На всякий случай убираем одиночные звёздочки, которые могли остаться.
    cleaned = cleaned.replace("*", "")
    return cleaned.strip()


async def get_gigachat_verdict(
    model_name: str,
    total_poizon_rub: float,
    rf_price_rub: float,
    shoe_size: str = "",
) -> str:
    """
    Асинхронная функция: запрашивает у GigaChat ёмкий вердикт на русском языке
    о выгодности покупки кроссовок КОНКРЕТНОГО РАЗМЕРА.

    Args:
        model_name:       точное название модели кроссовок;
        total_poizon_rub: итоговая стоимость заказа с Poizon в рублях;
        rf_price_rub:     НАЙДЕННАЯ (после динамической фильтрации) цена такой
                          же пары в РФ в рублях. 0.0 означает, что цену найти
                          не удалось — нейронке передаётся специальная
                          формулировка про анализ только рублевой цены Poizon;
        shoe_size:        размер обуви (например "42 EU"), добавляется в промпт,
                          чтобы ИИ учитывал редкость/ходовость размера.

    Returns:
        Текст вердикта (3-4 предложения) от нейросети (уже без markdown '**').

    Raises:
        Любое исключение при ошибках сети/авторизации/API пробрасывается
        наружу — вызывающий код (calculator) сам решит, что ИИ недоступен,
        отключит его флагом и сформирует чек без ИИ-вердикта.
    """
    size_part = f" размера {shoe_size}" if shoe_size else ""

    if rf_price_rub and rf_price_rub > 0:
        price_part = (
            f"выгодна ли покупка кроссовок {model_name}{size_part} за цену "
            f"{total_poizon_rub:.2f} руб по сравнению с РФ ценой "
            f"{rf_price_rub:.2f} руб? Обязательно учитывай размер "
            f"{shoe_size or 'не указан'}: если размер ходовой — сравнение "
            f"корректно, если редкий — цена в РФ может быть выше из-за дефицита. "
            f"Укажи примерную выгоду или переплату в рублях и процентах."
        )
    else:
        price_part = (
            f"В магазинах РФ цена неизвестна, проанализируй только выгоду "
            f"рублевой цены с Poizon: кроссовки {model_name}{size_part} стоят "
            f"{total_poizon_rub:.2f} руб под заказ. Оцени, нормальная ли это "
            f"рыночная цена для такой модели и размера."
        )

    prompt = (
        f"Ты — эксперт по покупкам на маркетплейсе Poizon (Dewu). "
        f"Дай ёмкий вердикт на русском языке (3-4 предложения): "
        f"{price_part} "
        f"Если в названии модели есть слово 'Custom' или 'Кастом', "
        f"обязательно подчеркни эксклюзивность и уникальность такой пары, "
        f"и то, что для кастомных моделей экономия может быть вторична. "
        f"Пиши обычный текст, без markdown-разметки со звёздочками."
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
                raise RuntimeError(
                    f"GigaChat Chat вернул HTTP {resp.status}: {body[:300]}"
                )
            data = await resp.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"GigaChat вернул пустой ответ: {str(data)[:300]}")

    verdict = choices[0]["message"]["content"].strip()
    if not verdict:
        raise RuntimeError("GigaChat вернул пустой текст вердикта")

    # Чистим markdown '**' -> HTML <b></b>, чтобы не ломать parse_mode="HTML".
    return sanitize_verdict_text(verdict)


async def test_gigachat_api() -> tuple[bool, str]:
    """
    Тестовый запрос к API GigaChat, выполняется при запуске бота.

    Делает реальный (короткий) запрос через OAuth + Chat и проверяет,
    что нейронка отвечает.

    Returns:
        (True,  "текст ответа нейронки")  — если API работает, вердикт можно
                                            использовать;
        (False, "описание ошибки")        — если любая ошибка, ИИ использовать
                                            НЕЛЬЗЯ.
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
# ЧЕСТНЫЙ РЕАЛЬНЫЙ ПОИСК ЦЕН В МАГАЗИНАХ РФ (без платных API-ключей)
# ============================================================================

# Фейковые User-Agent'ы: меняем их между запросами, чтобы поисковик
# не заблокировал бота как «бота».
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

# Жёсткие границы осмысленных цен (подстраховка к динамическому фильтру):
# меньше 3000 руб оригинальные кроссовки стоить не могут (это носки/шнурки),
# больше 1.5 млн — оптовая пачка или ошибка парсинга.
MIN_VALID_RF_PRICE = 3000.0
MAX_VALID_RF_PRICE = 1_500_000.0

# Коэффициент динамического порога: любые цены НИЖЕ
# (total_poizon_rub * DYNAMIC_FILTER_COEF) гарантированно мусор
# (носки, шнурки, стельки, паль) — оригинал в РФ не может стоить
# сильно дешевле закупки в Китае с доставкой.
DYNAMIC_FILTER_COEF = 0.85

# Регулярки для поиска цен в тексте выдачи:
#  1) "от 12 900 руб", "12900 рублей", "цена 12 900 ₽";
#  2) "₽ после числа": "12 900 ₽";
#  3) "цена: 12900" — число без suffix, но сразу после слова про цену.
PRICE_PATTERNS = [
    re.compile(
        r"(?:от|до|цена|стоимость)?\s*([\d][\d\s\u00a0\u2009]{3,})\s*"
        r"(?:руб\.?л?е?й?|рублей?\b)",
        re.IGNORECASE,
    ),
    re.compile(r"([\d][\d\s\u00a0\u2009]{3,})\s*\u20bd"),
    re.compile(
        r"(?:цена|стоимость|прайс)[^\d]{0,15}([\d][\d\s\u00a0\u2009]{3,})",
        re.IGNORECASE,
    ),
]


def _extract_prices_from_text(text: str) -> list[float]:
    """
    Вытаскивает из произвольного HTML/текста все денежные числа вида
    "от Х ХХХ руб" / "ХХХХХ ₽". Предварительно отсеивает явный мусор по
    жёстким границам (< 3000 руб и > 1.5 млн руб). Возвращает список
    УНИКАЛЬНЫХ цен (дубликаты от разных регулярных выражений убираются).
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
                logger.warning(
                    "DuckDuckGo %s вернул HTTP %s (попытка %d/%d)",
                    url, resp.status, attempt + 1, max_attempts,
                )
                # 202/403/429 — антибот-пауза, пробуем ещё раз с другой «личностью»
                if resp.status in (202, 403, 429):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return ""
        except Exception as exc:  # noqa: BLE001 — сеть/DNS/таймаут: страница не найдена
            logger.warning(
                "DuckDuckGo %s: ошибка запроса (попытка %d/%d): %s: %s",
                url, attempt + 1, max_attempts, type(exc).__name__, exc,
            )
            await asyncio.sleep(1.5)
    return ""


async def fetch_real_rf_price(
    model_name: str, shoe_size: str, total_poizon_rub: float
) -> float:
    """
    Асинхронный ЧЕСТНЫЙ поиск цены модели ЗАДАННОГО РАЗМЕРА в магазинах России
    (Яндекс.Маркет, СДЭК.Шоппинг, Спортмастер и др.) через бесплатный парсинг
    HTML-выдачи DuckDuckGo. Никаких платных API-ключей не требует.

    Алгоритм:
      1. Формирует точный поисковый запрос:
         "{model_name} {shoe_size} купить в россии цена руб".
      2. Делает асинхронный GET-запрос через aiohttp с фейковым User-Agent.
      3. Извлекает регулярными выражениями все найденные цены в список.
      4. ДИНАМИЧЕСКАЯ ФИЛЬТРАЦИЯ: min_allowed_price = total_poizon_rub * 0.85;
         все цены НИЖЕ порога удаляются (гарантированно отсекает шнурки,
         носки и паль).
      5. УСЕЧЁННОЕ СРЕДНЕЕ: список сортируется по возрастанию; если элементов
         больше 4 — удаляются ОДНА самая низкая и ОДНА самая высокая цена.
      6. Возвращает среднее арифметическое оставшихся цен.

    Args:
        model_name:       точное название модели кроссовок;
        shoe_size:        размер обуви, вшитый в поисковый запрос ("42 EU");
        total_poizon_rub: итоговая рублёвая цена заказа с Poizon — база для
                          динамического порога фильтрации.

    Returns:
        Найденная усечённая средняя цена в рублях либо 0.0, если после
        фильтрации список пуст или произошла любая ошибка.
    """
    model_name = (model_name or "").strip()
    shoe_size = (shoe_size or "").strip()
    if not model_name:
        return 0.0

    # 1. Точный поисковый запрос с размером.
    queries = [
        f"{model_name} {shoe_size} купить в россии цена руб",
        f"{model_name} {shoe_size} купить цена руб",
    ]

    all_prices: list[float] = []
    try:
        connector = aiohttp.TCPConnector(ssl=False, limit=5)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=60)
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
        logger.warning(
            "fetch_real_rf_price('%s'): ошибка: %s: %s",
            model_name, type(exc).__name__, exc,
        )
        return 0.0

    if not all_prices:
        logger.info(
            "fetch_real_rf_price('%s %s'): цены не найдены", model_name, shoe_size
        )
        return 0.0

    # 4. ДИНАМИЧЕСКАЯ ФИЛЬТРАЦИЯ: отсечь всё, что дешевле 85% цены Poizon.
    unique_prices = sorted(set(all_prices))
    total_poizon_rub = float(total_poizon_rub or 0.0)
    if total_poizon_rub > 0:
        min_allowed_price = total_poizon_rub * DYNAMIC_FILTER_COEF
        unique_prices = [p for p in unique_prices if p >= min_allowed_price]
        logger.debug(
            "fetch_real_rf_price('%s'): порог %.2f руб, после фильтра %d цен",
            model_name, min_allowed_price, len(unique_prices),
        )

    if not unique_prices:
        logger.info(
            "fetch_real_rf_price('%s %s'): после динамической фильтрации не "
            "осталось ни одной честной цены (весь мусор отсеян)",
            model_name, shoe_size,
        )
        return 0.0

    # 5. УСЕЧЁННОЕ СРЕДНЕЕ: при выборке > 4 убираем самую низкую и самую высокую.
    trimmed = list(unique_prices)  # список уже отсортирован по возрастанию
    if len(trimmed) > 4:
        trimmed = trimmed[1:-1]

    # 6. Среднее арифметическое оставшихся.
    average = sum(trimmed) / len(trimmed)
    logger.info(
        "fetch_real_rf_price('%s %s'): собрано %d уникальных цен, после "
        "фильтрации %d, итого среднее = %.2f руб",
        model_name, shoe_size, len(set(all_prices)), len(trimmed), average,
    )
    return round(average, 2)
