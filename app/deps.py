"""Общие зависимости запросов."""
import sqlite3

import httpx
from fastapi import Cookie, Depends, HTTPException

from app.db import get_db
from app.direct.client import DirectClient
from app.security import SESSION_COOKIE, find_user_by_session


def client_factory():
    """Создаёт клиент Директа. В тестах подменяется заглушкой."""
    return DirectClient


def creative_transport():
    """Транспорт для запросов к Claude и генератору картинок. В тестах подменяется заглушкой."""
    return None


def platform_transport():
    """Транспорт для запросов к VK Рекламе и Авито. В тестах подменяется заглушкой."""
    return None


def yandex_http():
    """HTTP-клиент для запросов к Яндекс ID. В тестах подменяется заглушкой."""
    return httpx.AsyncClient(timeout=15.0)


def current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    conn: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    user = find_user_by_session(conn, session)
    if user is None:
        raise HTTPException(401, detail={"message": "Войдите, чтобы продолжить."})
    return user
