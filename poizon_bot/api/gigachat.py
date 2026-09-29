"""
api/gigachat.py — Взаимодействие с API GigaChat от Сбера + УМНЫЙ анализ
поисковой выдачи для честного поиска цен в магазинах России.

НОВАЯ ЛОГИКА (GigaChat как умный аналитик поисковой выдачи):
 1. fetch_search_results(model_name, shoe_size) — асинхронно грузит HTML-выдачу
    DuckDuckGo по запросу "{model_name} {shoe_size} купить в россии" и собирает
    первые 5-7 результатов (заголовки сайтов + сниппеты) в одну текстовую
    строку search_results_text. Никаких платных API-ключей не требуется.
 2. get_gigachat_verdict(model_name, shoe_size, total_poizon_rub, credentials)
    передаёт этот сырой текст поисковой выдачи нейронке вместе с системным
    промптом. GigaChat САМ анализирует выдачу:
      - игнорирует оверпрайс официальных дорогих ритейлеров (Street Beat,
        SuperStep, Brandshop и т.д.);
      - ищет реальные цены на Poizon-площадках и у локальных реселлеров РФ
        (Poizon Shop, СДЭК.Шоппинг, Ozon/Маркет с доставкой из-за рубежа);
      - вычленяет адекватную рыночную цену для конкретного размера;
      - возвращает строгий JSON {"rf_price": число, "verdict": "текст"}.
 3. Функция возвращает кортеж (rf_price: float, verdict: str). Если rf_price
    равен 0.0 — цену найти не удалось, и вердикт формируется с учётом фразы
    "В магазинах РФ цена неизвестна, проанализируй только выгоду рублевой
    цены с Poizon".

OAuth реализован по стандарту Сбера: Basic-авторизация (base64 от
"ClientID:ClientSecret") + обязательный заголовок RqUID через uuid.uuid4().
Рабочие эндпоинты берутся из config.py (GIGACHAT_OAUTH_URL /
GIGACHAT_CHAT_URL), все post-запросы выполняются с ssl=False.

Тест доступности API при запуске бота (test_gigachat_api): если тест прошёл —
ИИ-аналитика используется в чеках; если упал — ИИ просто НЕ используется
(флаг ai_state.AI_AVAILABLE остаётся False).
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
    # У UUID множественных '_' нет, поэтому первый '_' — почти наверняка
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


async def _get_access_token(
    session: aiohttp.ClientSession, credentials: str | None = None
) -> str:
    """
    Получает временный Access Token у OAuth-сервера Сбера.

    Автоматически перебирает список SCOPE_CANDIDATES, пока не получит
    успешный ответ (HTTP 200 + access_token). Успешно подобранный scope
    кэшируется в модульной переменной _resolved_scope.

    Args:
        session:     активная aiohttp-сессия;
        credentials: опциональные ключи "ClientID_ClientSecret"; если не
                     переданы — берём GIGACHAT_CREDENTIALS из config.py.
    """
    global _resolved_scope

    if credentials is None:
        credentials = GIGACHAT_CREDENTIALS

    candidates = SCOPE_CANDIDATES
    # Если ранее уже подобрали рабочий scope — пробуем его первым.
    if _resolved_scope and _resolved_scope in candidates:
        candidates = [_resolved_scope] + [s for s in candidates if s != _resolved_scope]

    last_error = ""
    for scope in candidates:
        status, body_text = await _request_token_with_scope(session, credentials, scope)
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


# ============================================================================
# ЧЕСТНЫЙ ПОИСК ЦЕН В МАГАЗИНАХ РФ: СБОР СЫРОЙ ПОИСКОВОЙ ВЫДАЧИ ДЛЯ АНАЛИЗА ИИ
# (без платных API-ключей, без арифметического среднего — считает нейронка)
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

# Сколько результатов выдачи максимум отдаём нейронке на анализ.
MAX_SEARCH_RESULTS = 7

# Регулярка разбивает выдачу на блоки отдельных результатов.
# html.duckduckgo.com: каждый результат — div с классом result (или
# result__body) либо li с классом result__item; внутри могут быть вложенные
# div'ы, поэтому блок захватывается до начала СЛЕДУЮЩЕГО блока того же типа.
# lite.duckduckgo.com: каждый результат — строка таблицы <tr>...</tr>.
_RESULT_BLOCK_RE = re.compile(
    r"<(?:div|li)\b[^>]*class=\"[^\"]*\bresult(?:__body|__item)?\b[^\"]*\".*?"
    r"(?=<(?:div|li)\b[^>]*class=\"[^\"]*\bresult(?:__body|__item)?\b|"
    r"\Z(?!\n))",
    re.DOTALL | re.IGNORECASE,
)

# Внутри блока: заголовок результата.
_TITLE_RES = [
    re.compile(r'<a\b[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*>(.*?)</a>', re.DOTALL),
    re.compile(r"<a\b[^>]*class=['\"]result-link['\"][^>]*>(.*?)</a>", re.DOTALL),
]
# Внутри блока: сниппет (текст с ценой). Порядок важен: сначала более
# специфичные td/span (lite), затем div/a (html-версия).
_SNIPPET_RES = [
    re.compile(r'<td\b[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    re.compile(r"<span\b[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</span>", re.DOTALL),
    re.compile(r'<(?:div|a)\b[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:div|a)>', re.DOTALL),
]


def _strip_html_tags(fragment: str) -> str:
    """Удаляет HTML-теги и лишние пробелы из фрагмента выдачи."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = (
        text.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return " ".join(text.split()).strip()


def _find_in_block(patterns: list[re.Pattern], block: str) -> str:
    """Возвращает первый непустой текст, найденный в блоке одной из регулярок."""
    for pattern in patterns:
        match = pattern.search(block)
        if match:
            cleaned = _strip_html_tags(match.group(1))
            if cleaned:
                return cleaned
    return ""


def parse_search_results(html: str) -> str:
    """
    Превращает HTML поисковой выдачи DuckDuckGo в один текстовый блок вида:
        Сайт: <домен/заголовок>
        Описание: <сниппет с ценой>
    Заголовки и сниппеты извлекаются ПАРАМИ в пределах одного результата
    (блок-за-блоком), поэтому описание всегда относится к своему сайту.
    Берутся только первые MAX_SEARCH_RESULTS (5-7) результатов — больше
    контекста нейронке не нужно, а токенов уходит меньше.
    При любой неудачной структуре страницы возвращает пустую строку.
    """
    if not html:
        return ""

    blocks = _RESULT_BLOCK_RE.findall(html)

    results: list[str] = []
    if blocks:
        # Основной путь: парим каждый блок результата отдельно.
        for block in blocks:
            title = _find_in_block(_TITLE_RES, block)
            snippet = _find_in_block(_SNIPPET_RES, block)
            if not title and not snippet:
                continue
            results.append(f"Сайт: {title}\nОписание: {snippet}\n")
            if len(results) >= MAX_SEARCH_RESULTS:
                break
    else:
        # Подстраховка: структура страницы неизвестна — собираем заголовки и
        # сниппеты общими регулярками по всей странице.
        titles_all = [
            _strip_html_tags(t)
            for pat in _TITLE_RES
            for t in pat.findall(html)
            if _strip_html_tags(t)
        ]
        snippets_all = [
            _strip_html_tags(s)
            for pat in _SNIPPET_RES
            for s in pat.findall(html)
            if _strip_html_tags(s)
        ]
        pairs = max(len(titles_all), len(snippets_all))
        for i in range(pairs):
            title = titles_all[i] if i < len(titles_all) else ""
            snippet = snippets_all[i] if i < len(snippets_all) else ""
            if not title and not snippet:
                continue
            results.append(f"Сайт: {title}\nОписание: {snippet}\n")
            if len(results) >= MAX_SEARCH_RESULTS:
                break

    return "\n".join(results).strip()


async def _fetch_ddg_page(session: aiohttp.ClientSession, url: str, query: str) -> str:
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


async def fetch_search_results(model_name: str, shoe_size: str) -> str:
    """
    Асинхронно собирает СЫРУЮ текстовую поисковую выдачу DuckDuckGo
    (первые 5-7 результатов: заголовки сайтов + сниппеты) по запросу
    "{model_name} {shoe_size} купить в россии".

    Никакой математики здесь БОЛЬШЕ НЕТ — среднее арифметическое не
    считается. Этот текст целиком отдаётся GigaChat, который сам решает,
    какие цены настоящие, а какие — оверпрайс официалов или мусор.

    Args:
        model_name: точное название модели кроссовок;
        shoe_size:  размер обуви ("42 EU", "27 см" и т.п.) — вшивается в запрос,
                    чтобы выдача была именно про нужный размер.

    Returns:
        Строка search_results_text с результатами выдачи либо "" при ошибке /
        отсутствии результатов.
    """
    model_name = (model_name or "").strip()
    shoe_size = (shoe_size or "").strip()
    if not model_name:
        return ""

    # Точный поисковый запрос с размером.
    query = f"{model_name} {shoe_size} купить в россии".strip()

    try:
        connector = aiohttp.TCPConnector(ssl=False, limit=5)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            # Пробуем обе HTML-версии DuckDuckGo, пока не получим выдачу.
            for url in DDG_SEARCH_URLS:
                page_html = await _fetch_ddg_page(session, url, query)
                results_text = parse_search_results(page_html)
                if results_text:
                    logger.info(
                        "fetch_search_results('%s %s'): собрано %d результатов "
                        "выдачи с %s",
                        model_name, shoe_size,
                        results_text.count("Сайт:"), url,
                    )
                    return results_text
                await asyncio.sleep(1.0)  # вежливая пауза, не спамим поисковик
    except Exception as exc:  # noqa: BLE001 — любая сетевая ошибка => пустая строка
        logger.warning(
            "fetch_search_results('%s'): ошибка: %s: %s",
            model_name, type(exc).__name__, exc,
        )

    logger.info(
        "fetch_search_results('%s %s'): поисковая выдача пуста",
        model_name, shoe_size,
    )
    return ""


def _parse_ai_json(content: str) -> dict:
    """
    Вытаскивает JSON-объект из ответа модели. GigaChat иногда оборачивает
    JSON в markdown-блок ```json ... ``` или добавляет поясняющий текст вокруг,
    поэтому сначала чистим обёртку, затем пробуем loads целиком, а если не
    вышло — ищем первую фигурную скобку и ближайшую закрывающую.
    """
    clean = content.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(clean)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass

    start = clean.find("{")
    end = clean.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(clean[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass

    raise ValueError(f"Ответ ИИ не является валидным JSON: {content[:300]}")


async def get_gigachat_verdict(
    model_name: str,
    shoe_size: str,
    total_poizon_rub: float,
    credentials: str | None = None,
) -> tuple[float, str]:
    """
    Гибридный анализ «поиск + нейронка»: GigaChat выступает умным аналитиком
    поисковой выдачи и сам отсекает оверпрайс и мусор.

    Алгоритм:
      1. fetch_search_results() собирает сырую текстовую выдачу DuckDuckGo по
         модели и размеру.
      2. Получаем Access Token у OAuth-сервера Сбера (Basic + RqUID uuid4,
         автоматический подбор scope).
      3. Отправляем в Chat-эндпоинт системный промпт-инструкцию + текст выдачи.
         Модель обязана вернуть СТРОГИЙ JSON:
         {"rf_price": число, "verdict": "текст мнения о выгоде на русском"}.
      4. Парсим JSON и возвращаем (rf_price, verdict).

    Args:
        model_name:       точное название модели кроссовок;
        shoe_size:        размер обуви — учитывается и в поиске, и в вердикте;
        total_poizon_rub: итоговая рублёвая стоимость заказа с Poizon;
        credentials:      опциональные ключи "ClientID_ClientSecret"; по умолчанию
                          берутся GIGACHAT_CREDENTIALS из config.py.

    Returns:
        (rf_price, verdict):
          rf_price > 0  — нейронка нашла реальную рыночную цену в РФ;
          rf_price == 0 — цену найти не удалось (пустая выдача / ИИ не нашёл
                          чисел / ошибка сети или парсинга). В этом случае
                          вердикт всё равно формируется: нейронка получает
                          инструкцию проанализировать только выгоду рублевой
                          цены с Poizon.

    Текст вердикта проходит sanitize_verdict_text(): markdown '**' заменяется
    на HTML-теги <b>/</b> поочерёдно, чтобы не ломать parse_mode="HTML".
    """
    if credentials is None:
        credentials = GIGACHAT_CREDENTIALS

    # 1. Сырая поисковая выдача для анализа нейронкой.
    search_context = await fetch_search_results(model_name, shoe_size)

    # 2. Системная инструкция для GigaChat-аналитика.
    system_instruction = (
        "Ты — умный ИИ-ассистент для анализа цен на кроссовки. Перед тобой "
        "текст поисковой выдачи по запросу покупки кроссовок в России. "
        "Твоя задача — найти среди этого текста РЕАЛЬНУЮ цену на указанную "
        "модель и размер кроссовок в РФ. "
        "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
        "1. Игнорируй цены от официальных или очень дорогих магазинов типа "
        "Street Beat, SuperStep, Brandshop (у них жесткий оверпрайс).\n"
        "2. Ищи цены на сайтах локальных Poizon-доставщиков и реселлеров "
        "(типа Poizon Shop, СДЭК.Шоппинг, Ozon/Маркет с доставкой из-за "
        "рубежа).\n"
        "3. Из текста выбери ОДНО число — самую адекватную рыночную цену в РФ "
        "для этой модели и размера. Верни ответ СТРОГО в формате JSON:\n"
        '{"rf_price": число, "verdict": "текст твоего мнения о выгоде покупки '
        'на русском языке (3-4 предложения)"}\n'
        "В тексте вердикта напиши, сколько экономит пользователь по сравнению "
        "с Poizon, и стоит ли брать. Учитывай размер обуви: редкие размеры "
        "могут стоить дороже ходовых. Если в названии модели есть слово "
        "'Custom' или 'Кастом' — обязательно подчеркни эксклюзивность такой "
        "пары. Если в выдаче нет ни одной подходящей цены, верни rf_price = 0 "
        "и в verdict отметь, что в магазинах РФ цена неизвестна, проанализировав "
        "только выгоду рублевой цены с Poizon. Не используй разметку markdown "
        "(никаких ** звездочек)."
    )

    # 3. Пользовательский промпт с контекстом поиска.
    user_prompt = (
        f"Модель: {model_name}\n"
        f"Размер: {shoe_size or 'не указан'}\n"
        f"Итоговая цена на Poizon: {total_poizon_rub:.2f} руб.\n\n"
        f"Текст поисковой выдачи для анализа:\n{search_context or '(ничего не найдено)'}"
    )

    chat_payload = {
        "model": GIGACHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "stream": False,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            # Авторизация Сбера (подбор scope + RqUID внутри _get_access_token).
            access_token = await _get_access_token(session, credentials)

            chat_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }

            async with session.post(
                GIGACHAT_CHAT_URL, json=chat_payload, headers=chat_headers, ssl=False
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "GigaChat Chat вернул HTTP %s: %s", resp.status, body[:300]
                    )
                    return 0.0, "⚠️ Ошибка генерации вердикта."
                chat_res = await resp.json()

        choices = chat_res.get("choices") or []
        if not choices:
            logger.warning("GigaChat вернул пустой choices: %s", str(chat_res)[:300])
            return 0.0, "⚠️ Ошибка генерации вердикта."

        content = (choices[0].get("message") or {}).get("content", "").strip()
        if not content:
            return 0.0, "⚠️ Ошибка генерации вердикта."

        # 4. Парсим JSON-ответ от ИИ.
        data = _parse_ai_json(content)

        try:
            rf_price = float(data.get("rf_price", 0) or 0)
        except (ValueError, TypeError):
            rf_price = 0.0
        if rf_price < 0:
            rf_price = 0.0

        verdict = str(data.get("verdict", "")).strip()
        if not verdict:
            if rf_price > 0:
                verdict = (
                    f"Реальная рыночная цена в РФ найдена: {rf_price:.2f} руб. "
                    f"Сравните с итогом по Poizon ({total_poizon_rub:.2f} руб.)."
                )
            else:
                verdict = (
                    "В магазинах РФ цена неизвестна, проанализируй только "
                    "выгоду рублевой цены с Poizon — данных для сравнения нет."
                )

        # Чистим markdown '**' -> HTML <b></b>, чтобы не ломать parse_mode="HTML".
        return rf_price, sanitize_verdict_text(verdict)

    except Exception as exc:  # noqa: BLE001 — сеть/OAuth/JSON: не роняем бота
        logger.warning(
            "get_gigachat_verdict('%s'): ошибка: %s: %s",
            model_name, type(exc).__name__, exc,
        )
        return 0.0, "⚠️ Не удалось распарсить ответ ИИ."


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
