"""
api/gigachat.py — Взаимодействие с API GigaChat от Сбера.

Здесь реализованы:
 - тест доступности API при запуске бота (test_gigachat_api);
 - получение Access Token по OAuth (с уникальным RqUID в заголовках);
 - отправка промпта в Chat-эндпоинт и получение ИИ-вердикта;
 - вспомогательная функция mock-цены для РФ-рынка.

Логика использования: если тест при запуске прошёл — нейронка используется
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
    # Умножественных '_' в UUID нет, поэтому первый '_' — почти наверняка
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
        model_name:      точное название модели кроссовок;
        total_poizon_rub: итоговая стоимость заказа с Poizon в рублях;
        rf_price_rub:    ориентировочная цена такой же пары на рынке РФ в рублях.

    Returns:
        Текст вердикта (3-4 предложения) от нейросети.

    Raises:
        Любое исключение при ошибках сети/авторизации/API пробрасывается
        наружу — вызывающий код (calculator) сам решит, что ИИ недоступен,
        отключит его флагом и сформирует чек без ИИ-вердикта.
    """
    prompt = (
        f"Ты — эксперт по покупкам на маркетплейсе Poizon (Dewu). "
        f"Дай ёмкий вердикт на русском языке (3-4 предложения): "
        f"выгодна ли покупка кроссовок {model_name} за цену {total_poizon_rub:.2f} руб "
        f"по сравнению с ценой на рынке РФ {rf_price_rub:.2f} руб? "
        f"Укажи примерную выгоду или переплату в рублях и процентах. "
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


def get_mock_rf_price(total_price: float) -> float:
    """
    Вспомогательная функция: имитация цены такой же пары на рынке РФ.
    Считаем, что локальные перекупы держат наценку ~35% поверх итога Poizon.
    """
    return total_price * 1.35
