"""Сайт «Автобидер»: страницы и API."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings, load_env
from app.db import init_db
from app.routes import accounts, auth, businesses

WEB_DIR = Path(__file__).resolve().parent / "web"

load_env()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(get_settings().db_path)
    yield


app = FastAPI(title="Автобидер", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(businesses.router)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def config():
    s = get_settings()
    return {
        "dev_token_available": s.dev_mode and bool(s.dev_token),
        "token_url": (
            f"https://oauth.yandex.ru/authorize?response_type=token&client_id={s.client_id}" if s.client_id else ""
        ),
    }


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
