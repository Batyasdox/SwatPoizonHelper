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

import base64
import json
import uuid

import aiohttp

from config import (
    GIGACHAT_CREDENTIALS,
    GIGACHAT_CHAT_URL,
    GIGACHAT_MODEL,
    GIGACHAT_OAUTH_URL,
)


def _get_basic_auth_header() -> str:
    """
    Формирует заголовок Authorization: Basic ... для OAuth-запроса.
    Credentials кодируются в base64 в формате "ClientID:ClientSecret"
    (в конфиге ключ может быть указан через "_", заменим на ":").
    """
    credentials = GIGACHAT_CREDENTIALS.replace("_", ":", 1)
    raw = credentials.encode("utf-8")
    token = base64.b64encode(raw).decode("utf-8")
    return f"Basic {token}"


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    """
    Получает временный Access Token у OAuth-сервера Сбера.
    В headers обязательно передаём уникальный RqUID (uuid4).
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Accept": "application/json",
        "Authorization": _get_basic_auth_header(),
        "RqUID": str(uuid.uuid4()),
    }
    # ВНИМАНИЕ: значение scope зависит от типа ключа в кабинете Сбера
    # (developers.sber.ru -> ваш проект -> GigaAPI -> "Спецификация доступа"):
    #   - GIGACHTAPI                — стандартный scope для GigaChat;
    #   - GIGACHAT_API_PERSONAL     — персональный режим ("Индивидуальный");
    #   - GIGACHAT_API_CORP_FQBK и др. — корпоративные режимы.
    # Если OAuth вернёт "scope data format invalid" — откройте карточку ключа
    # в кабинете Сбера и скопируйте значение scope оттуда точно как написано.
    payload = {"scope": "GIGACHTAPI"}

    async with session.post(GIGACHAT_OAUTH_URL, data=payload, headers=headers, ssl=False) as resp:
        body_text = await resp.text()
        if resp.status != 200:
            raise RuntimeError(
                f"OAuth Сбера вернул HTTP {resp.status}: {body_text[:300]}"
            )
        try:
            data = json.loads(body_text)
        except ValueError:
            raise RuntimeError(f"OAuth Сбера вернул не-JSON ответ: {body_text[:300]}")
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"Не удалось получить Access Token от Сбера: {data}")
        return access_token


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
