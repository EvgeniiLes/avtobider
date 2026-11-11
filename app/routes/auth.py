"""Регистрация, вход и выход."""
import re
import sqlite3
import time

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.db import get_db
from app.deps import current_user
from app.security import (
    SESSION_COOKIE,
    SESSION_DAYS,
    create_session,
    delete_session,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_FAILS, FAIL_WINDOW = 10, 600  # 10 неудачных входов за 10 минут
_fails: dict[str, list[float]] = {}
MAX_SIGNUPS, SIGNUP_WINDOW = 10, 3600  # не больше 10 регистраций в час с одного адреса
_signups: dict[str, list[float]] = {}


class Credentials(BaseModel):
    email: str
    password: str


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def _too_many_fails(email: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(email, []) if now - t < FAIL_WINDOW]
    _fails[email] = recent
    return len(recent) >= MAX_FAILS


@router.post("/register")
def register(data: Credentials, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    email = data.email.strip().lower()
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _signups.get(ip, []) if now - t < SIGNUP_WINDOW]
    if len(recent) >= MAX_SIGNUPS:
        raise HTTPException(429, detail={"message": "С вашего адреса слишком много регистраций. Попробуйте позже."})
    _signups[ip] = recent
    if not EMAIL_RE.match(email):
        raise HTTPException(400, detail={"message": "Проверьте e-mail: он должен выглядеть как name@example.com."})
    if len(data.password) < MIN_PASSWORD:
        raise HTTPException(400, detail={"message": f"Пароль слишком короткий. Нужно не меньше {MIN_PASSWORD} символов."})
    try:
        cur = conn.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, hash_password(data.password)))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, detail={"message": "Этот e-mail уже зарегистрирован. Попробуйте войти."}) from exc
    _signups[ip].append(now)
    _set_cookie(request, response, create_session(conn, cur.lastrowid))
    return {"email": email}


@router.post("/login")
def login(data: Credentials, request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    email = data.email.strip().lower()
    if _too_many_fails(email):
        raise HTTPException(429, detail={"message": "Слишком много неверных попыток. Подождите 10 минут и попробуйте снова."})
    row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or not verify_password(data.password, row["password_hash"]):
        _fails.setdefault(email, []).append(time.time())
        raise HTTPException(401, detail={"message": "Неверный e-mail или пароль."})
    _fails.pop(email, None)
    _set_cookie(request, response, create_session(conn, row["id"]))
    return {"email": email}


@router.post("/logout")
def logout(
    response: Response,
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    conn: sqlite3.Connection = Depends(get_db),
):
    delete_session(conn, session)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: sqlite3.Row = Depends(current_user)):
    return {"email": user["email"]}
