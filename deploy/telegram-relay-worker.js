// Ретранслятор Telegram Bot API для Cloudflare Workers (бесплатно, свой сервер за границей не нужен).
// Зачем: с нашего сервера в России api.telegram.org не открывается, а Cloudflare открывается. Сервис пишет сюда, ретранслятор
// пересылает запрос в Telegram и возвращает ответ.
// Защита: пропускает только запросы с токеном вашего бота (он хранится в секрете BOT_TOKEN воркера) и только три метода,
// поэтому чужие не смогут использовать адрес как открытый прокси. Методы: отправка сообщений, привязка чата и кнопки «Да / Нет» под
// предложениями (setWebhook, getWebhookInfo, editMessageText, answerCallbackQuery).
// Установка: docs/deployment.md, раздел «Отчёты и тревоги в Telegram».
const ALLOWED_METHODS = new Set(["getMe", "getUpdates", "sendMessage", "setWebhook", "getWebhookInfo", "editMessageText", "answerCallbackQuery"]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/bot([^/]+)\/([A-Za-z]+)$/);
    if (!env.BOT_TOKEN || !match || match[1] !== env.BOT_TOKEN || !ALLOWED_METHODS.has(match[2])) {
      return new Response("forbidden", { status: 403 });
    }
    return fetch(`https://api.telegram.org${url.pathname}${url.search}`, {
      method: request.method,
      headers: { "content-type": request.headers.get("content-type") || "application/json" },
      body: request.method === "POST" ? request.body : undefined,
    });
  },
};
