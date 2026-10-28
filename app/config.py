"""Настройки из переменных окружения и файла .env."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Читает .env и дополняет им os.environ. Уже заданные переменные не трогает."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    dev_mode: bool
    dev_token: str
    client_id: str
    client_secret: str
    direct_redirect_uri: str
    login_client_id: str
    login_client_secret: str
    redirect_uri: str
    direct_api_version: str
    direct_client_login: str
    db_path: Path
    secret_key: str

    @property
    def uploads_dir(self) -> Path:
        """Папка с файлами бизнесов (логотипы, фото, видео). Лежит рядом с базой."""
        return self.db_path.parent / "uploads"


def _secret_key(db_path: Path) -> str:
    """SECRET_KEY из окружения; если не задан, создаётся и хранится в файле рядом с базой."""
    from_env = os.environ.get("SECRET_KEY", "").strip()
    if from_env:
        return from_env
    key_file = db_path.parent / "secret.key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    key_file.write_text(key, encoding="utf-8")
    return key


def get_settings() -> Settings:
    db_path = Path(os.environ.get("DB_PATH") or "data/app.db")
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return Settings(
        dev_mode=os.environ.get("APP_ENV", "").strip().lower() == "development",
        dev_token=os.environ.get("YANDEX_DEV_TOKEN", "").strip(),
        client_id=os.environ.get("YANDEX_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("YANDEX_CLIENT_SECRET", "").strip(),
        direct_redirect_uri=os.environ.get("DIRECT_REDIRECT_URI", "").strip(),
        login_client_id=os.environ.get("YANDEX_LOGIN_CLIENT_ID", "").strip(),
        login_client_secret=os.environ.get("YANDEX_LOGIN_CLIENT_SECRET", "").strip(),
        redirect_uri=os.environ.get("YANDEX_REDIRECT_URI", "").strip(),
        direct_api_version=os.environ.get("DIRECT_API_VERSION", "").strip() or "v501",
        direct_client_login=os.environ.get("DIRECT_CLIENT_LOGIN", "").strip(),
        db_path=db_path,
        secret_key=_secret_key(db_path),
    )
