"""Telegram: отправка сообщений владельцу и привязка его чата к сервису по одноразовому коду.

Привязка без публичного адреса для Telegram: владелец открывает бота по ссылке с кодом и нажимает «Start», потом в сервисе нажимает
«Проверить», и сервис один раз забирает у Telegram последние сообщения боту (getUpdates) и ищет в них свой код.

Важно: с серверов в России api.telegram.org часто недоступен. Тогда в TELEGRAM_API_BASE нужно указать промежуточный адрес
(обратный прокси на сервере за границей), всё остальное работает так же.
"""
import secrets
from datetime import datetime, timedelta, timezone

import httpx

CODE_LIFETIME = timedelta(minutes=30)
UNREACHABLE = ("Не удалось связаться с Telegram. С серверов в России его адрес часто закрыт: нужен промежуточный адрес "
               "(настройка TELEGRAM_API_BASE), подробности в docs/deployment.md.")


class TelegramError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def call(token: str, api: str, method: str, payload: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict | list:
    """Вызывает метод Bot API и возвращает result. Любой сбой превращается в TelegramError с понятным текстом (токен в текст не попадает)."""
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as http:
            r = await http.post(f"{api}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise TelegramError(UNREACHABLE) from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise TelegramError("Telegram ответил непонятно. Попробуйте позже.") from exc
    if not data.get("ok"):
        raise TelegramError(data.get("description") or "Telegram не принял запрос.")
    return data.get("result")


STATS_BUTTON = "Статистика"
MENU = {"keyboard": [[{"text": STATS_BUTTON}]], "resize_keyboard": True, "is_persistent": True}   # кнопка под полем ввода: остаётся, пока её не уберут


async def send_message(token: str, api: str, chat_id: str, text: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    await call(token, api, "sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "reply_markup": MENU}, transport)


async def send_buttons(token: str, api: str, chat_id: str, text: str, buttons: list[tuple[str, str]],
                       transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Сообщение с кнопками в один ряд: buttons = [(надпись, callback_data)]. Возвращает номер сообщения (по нему его потом правят)."""
    keyboard = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
    sent = await call(token, api, "sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True, "reply_markup": keyboard}, transport)
    return int(sent["message_id"])


async def finish_buttons(token: str, api: str, chat_id: str, message_id: int, text: str, callback_id: str = "", note: str = "",
                         transport: httpx.AsyncBaseTransport | None = None) -> None:
    """После ответа: кнопки убираются, под текстом пишется итог; нажатие в Telegram подтверждается (иначе кнопка «крутится»)."""
    if callback_id:
        await call(token, api, "answerCallbackQuery", {"callback_query_id": callback_id, "text": note[:190]}, transport)
    await call(token, api, "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000], "disable_web_page_preview": True}, transport)


async def set_webhook(token: str, api: str, url: str, secret: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Просит Telegram присылать ответы владельца (нажатия кнопок, «/start код») на этот адрес. secret возвращается в заголовке каждого запроса."""
    await call(token, api, "setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]}, transport)


async def webhook_url(token: str, api: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    info = await call(token, api, "getWebhookInfo", {}, transport)
    return (info or {}).get("url", "") if isinstance(info, dict) else ""


async def find_start(token: str, api: str, code: str, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Чат, из которого боту прислали «/start <код>». None, если такого сообщения пока нет."""
    updates = await call(token, api, "getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message"]}, transport)
    for update in reversed(updates if isinstance(updates, list) else []):
        message = update.get("message") or {}
        if (message.get("text") or "").strip() == f"/start {code}" and (message.get("chat") or {}).get("type") == "private":
            chat = message["chat"]
            name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p) or chat.get("username") or "чат"
            return {"chat_id": str(chat["id"]), "title": name}
    return None


def new_code() -> str:
    return secrets.token_hex(5).upper()


def code_valid(code: str, issued_at: str, now: datetime | None = None) -> bool:
    if not code or not issued_at:
        return False
    try:
        issued = datetime.fromisoformat(issued_at)
    except ValueError:
        return False
    return (now or datetime.now(timezone.utc)) - issued < CODE_LIFETIME
