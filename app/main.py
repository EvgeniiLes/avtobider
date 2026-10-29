"""Сайт «Автобидер»: страницы и API."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings, load_env
from app.db import connect, init_db
from app.routes import accounts, auth, businesses, creatives, direct_connect, launch, marking, metrika, stats, yandex_login

WEB_DIR = Path(__file__).resolve().parent / "web"

load_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(get_settings().db_path)
    yield


app = FastAPI(title="Автобидер", lifespan=lifespan)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Защитные заголовки: запрет вставки сайта в чужие страницы, чужих скриптов и угадывания типа файла."""
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
        # Страницы и скрипты без версии в имени: браузер должен сверяться с сервером, иначе после обновления остаётся старая версия.
        response.headers["Cache-Control"] = "no-cache"
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response
app.include_router(auth.router)
app.include_router(yandex_login.router)
app.include_router(direct_connect.router)
app.include_router(accounts.router)
app.include_router(businesses.router)
app.include_router(stats.router)
app.include_router(launch.router)
app.include_router(marking.router)
app.include_router(metrika.router)
app.include_router(creatives.router)


@app.get("/api/health")
def health():
    """Живость сервиса: заодно проверяет, что база открывается."""
    try:
        conn = connect(get_settings().db_path)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        raise HTTPException(503, detail={"ok": False, "message": "База данных недоступна"})
    return {"ok": True}


@app.get("/api/config")
def config():
    s = get_settings()
    return {
        "dev_token_available": s.dev_mode and bool(s.dev_token),
        "yandex_login": bool(s.login_client_id and s.login_client_secret),
        "direct_oauth": bool(s.client_id and s.client_secret),
        "token_url": (
            f"https://oauth.yandex.ru/authorize?response_type=token&client_id={s.client_id}" if s.client_id else ""
        ),
    }


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
