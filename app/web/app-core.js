const $ = (id) => document.getElementById(id);

// Полоса сверху: пока сайт отвечает дольше трети секунды, видно, что запрос идёт
let inflight = 0, busyTimer = null;
function busy(delta) {
  inflight = Math.max(0, inflight + delta);
  clearTimeout(busyTimer);
  const bar = document.getElementById("net-bar");
  if (!bar) return;
  if (inflight > 0) busyTimer = setTimeout(() => { bar.hidden = false; }, 300); else bar.hidden = true;
}

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  busy(1);
  try {
    response = await fetch(url, options);
  } catch (_) {
    return { ok: false, status: 0, data: { detail: { message: "Сайт не отвечает. Проверьте интернет и попробуйте ещё раз." } } };
  } finally {
    busy(-1);
  }
  let data = {};
  try { data = await response.json(); } catch (_) { /* пустой ответ */ }
  return { ok: response.ok, status: response.status, data };
}

function errorText(result) {
  const d = result.data && result.data.detail;
  if (d && d.message) return d.message;
  if (Array.isArray(d)) {
    const own = d.map((e) => e.msg || "").find((m) => m.startsWith("Value error, "));
    return own ? own.replace("Value error, ", "") : "Проверьте, что все поля заполнены правильно.";
  }
  return "Что-то пошло не так. Попробуйте ещё раз.";
}

const DIRECT_URL = "https://direct.yandex.ru/dna/grid/campaigns/";

// «direct.yandex.ru» в тексте подсказки становится ссылкой на кабинет Директа: оттуда пополняют счёт
function withLinks(text) {
  const parts = String(text).split("direct.yandex.ru");
  const out = [];
  parts.forEach((part, i) => {
    if (i) out.push(Object.assign(document.createElement("a"), { href: DIRECT_URL, target: "_blank", rel: "noopener", textContent: "direct.yandex.ru" }));
    if (part) out.push(document.createTextNode(part));
  });
  return out;
}

function setStatus(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

function toast(text, kind) {
  const node = el("div", { className: "toast" + (kind === "err" ? " err" : ""), textContent: text });
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "err" ? 7000 : 4000);
}

// Результат действия: успех — всплывающая подсказка, ошибка — тоже, но дольше.
function say(text, kind) {
  if (text) toast(text, kind);
}

function askConfirm(title, text, yesLabel = "Да") {
  return new Promise((resolve) => {
    $("modal-title").textContent = title;
    $("modal-text").textContent = text;
    $("modal-yes").textContent = yesLabel;
    $("modal").hidden = false;
    $("modal-yes").focus();
    const done = (value) => {
      $("modal").hidden = true;
      $("modal-yes").onclick = $("modal-no").onclick = $("modal").onclick = document.onkeydown = null;
      resolve(value);
    };
    $("modal-yes").onclick = () => done(true);
    $("modal-no").onclick = () => done(false);
    $("modal").onclick = (e) => { if (e.target === $("modal")) done(false); };
    document.onkeydown = (e) => { if (e.key === "Escape") done(false); };
  });
}

/* ---------- Вход и регистрация ---------- */

let authMode = "login";

