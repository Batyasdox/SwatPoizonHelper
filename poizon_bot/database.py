"""
database.py — Инициализация и функции работы с SQLite.

Храним минимально необходимую информацию: user_id и персональный курс юаня.
"""

import sqlite3

from config import DB_FILENAME, DEFAULT_YUAN_RATE


def _get_connection() -> sqlite3.Connection:
    """Возвращает соединение с базой данных users_v2.db."""
    conn = sqlite3.connect(DB_FILENAME)
    return conn


def init_db() -> None:
    """Создаёт таблицу users, если она ещё не существует."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            rate    REAL
        )
        """
    )
    conn.commit()
    conn.close()


def is_user_exists(user_id: int) -> bool:
    """Проверяет, есть ли пользователь в базе данных. Возвращает True/False."""
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_user_rate(user_id: int) -> float:
    """
    Возвращает сохранённый курс пользователя.
    Если пользователя нет в базе — возвращает дефолтный курс 13.35.
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT rate FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None or row[0] is None:
        return DEFAULT_YUAN_RATE
    return float(row[0])


def update_user_rate(user_id: int, new_rate: float) -> None:
    """
    Вставляет нового пользователя или обновляет курс существующего
    через механизм ON CONFLICT (upsert).
    """
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO users (user_id, rate)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET rate = excluded.rate
        """,
        (user_id, float(new_rate)),
    )
    conn.commit()
    conn.close()
