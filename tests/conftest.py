import pytest

from app.routes import auth


@pytest.fixture(autouse=True)
def reset_auth_limits():
    """Ограничения на вход и регистрацию хранятся в памяти процесса: перед каждым тестом начинаем с нуля."""
    auth._fails.clear()
    auth._signups.clear()
    yield
    auth._fails.clear()
    auth._signups.clear()
