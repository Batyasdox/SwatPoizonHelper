"""
api/gigachat.py — Взаимодействие с API GigaChat от Сбера.

Здесь реализованы:
 - получение Access Token по OAuth (с уникальным RqUID в заголовках);
 - отправка промпта в Chat-эндпоинт и получение ИИ-вердикта;
 - вспомогательная функция mock-цены для РФ-рынка.

ВАЖНО: реальные эндпоинты Сбера указаны как https://sberbank.ru
(см. config.py). Все post-запросы выполняются с ssl=False, как требуется проектом.
"""

import base64
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
    payload = {"scope": "GIGACHTAPI"}

    async with session.post(GIGACHAT_OAUTH_URL, data=payload, headers=headers, ssl=False) as resp:
        data = await resp.json()
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
        Текст вердикта (3-4 предложения). При любой ошибке API возвращает
        аккуратное сообщение-заглушку, чтобы бот не падал.
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

    try:
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
                data = await resp.json()

            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"GigaChat вернул пустой ответ: {data}")

            verdict = choices[0]["message"]["content"].strip()
            return verdict

    except Exception as exc:  # noqa: BLE001
        # Сеть/ключи/Enderpoint могут быть недоступны — бот должен жить дальше.
        diff = rf_price_rub - total_poizon_rub
        saving_pct = (diff / rf_price_rub * 100) if rf_price_rub > 0 else 0.0
        if diff > 0:
            fallback = (
                f"⚠️ ИИ-аналитика временно недоступна ({type(exc).__name__}). "
                f"Расчёт вручную: покупка {model_name} на Poizon обойдётся в "
                f"{total_poizon_rub:.2f} руб против ~{rf_price_rub:.2f} руб в РФ. "
                f"Ваша ориентировочная выгода составляет {diff:.2f} руб ({saving_pct:.1f}%)."
            )
        else:
            fallback = (
                f"⚠️ ИИ-аналитика временно недоступна ({type(exc).__name__}). "
                f"Расчёт вручную: покупка {model_name} на Poizon обойдётся в "
                f"{total_poizon_rub:.2f} руб против ~{rf_price_rub:.2f} руб в РФ. "
                f"Переплата составит {-diff:.2f} руб ({-saving_pct:.1f}%), "
                f"возможно выгоднее купить пару локально."
            )
        return fallback


def get_mock_rf_price(total_price: float) -> float:
    """
    Вспомогательная функция: имитация цены такой же пары на рынке РФ.
    Считаем, что локальные перекупы держат наценку ~35% поверх итога Poizon.
    """
    return total_price * 1.35
