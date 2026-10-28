const $ = (id) => document.getElementById(id);

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (_) {
    return { ok: false, status: 0, data: { detail: { message: "Сайт не отвечает. Проверьте интернет и попробуйте ещё раз." } } };
  }
  let data = {};
  try { data = await response.json(); } catch (_) { /* пустой ответ */ }
  return { ok: response.ok, status: response.status, data };
}

function errorText(result) {
  const d = result.data && result.data.detail;
  if (d && d.message) return d.message;
  if (Array.isArray(d)) return "Проверьте, что все поля заполнены правильно.";
  return "Что-то пошло не так. Попробуйте ещё раз.";
}

function setStatus(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

/* ---------- Вход и регистрация ---------- */

let authMode = "login";

function setAuthMode(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("auth-submit").textContent = mode === "login" ? "Войти" : "Создать аккаунт";
  $("password-hint").textContent = mode === "register" ? "(не меньше 8 символов)" : "";
  $("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  setStatus("auth-status", "");
}

async function submitAuth(event) {
  event.preventDefault();
  const button = $("auth-submit");
  button.disabled = true;
  setStatus("auth-status", "Подождите…");
  const result = await api("POST", `/api/auth/${authMode}`, {
    email: $("email").value,
    password: $("password").value,
  });
  button.disabled = false;
  if (!result.ok) {
    setStatus("auth-status", errorText(result), "err");
    return;
  }
  $("password").value = "";
  showApp(result.data.email);
}

async function logout() {
  await api("POST", "/api/auth/logout");
  showAuth();
}

function showAuth() {
  $("view-auth").hidden = false;
  $("view-app").hidden = true;
  $("user").hidden = true;
  setAuthMode("login");
}

/* ---------- Кабинеты ---------- */

async function showApp(email) {
  $("view-auth").hidden = true;
  $("view-app").hidden = false;
  $("user").hidden = false;
  $("user-email").textContent = email;
  $("campaigns-card").hidden = true;
  await loadAccounts();
}

async function loadAccounts() {
  const result = await api("GET", "/api/accounts");
  if (result.status === 401) { showAuth(); return; }
  const list = $("accounts");
  list.replaceChildren();
  const accounts = result.ok ? result.data.accounts : [];
  $("accounts-empty").hidden = accounts.length > 0;
  for (const account of accounts) {
    const li = document.createElement("li");

    const info = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = account.title;
    const meta = document.createElement("span");
    meta.className = "hint";
    meta.textContent = ` ${account.platform}${account.sandbox ? ", тестовый режим" : ""}`;
    info.append(name, meta);

    const actions = document.createElement("div");
    const view = document.createElement("button");
    view.className = "small";
    view.textContent = "Показать кампании";
    view.addEventListener("click", () => showCampaigns(account.id));
    const remove = document.createElement("button");
    remove.className = "small secondary";
    remove.textContent = "Удалить";
    remove.addEventListener("click", () => removeAccount(account));
    actions.append(view, remove);

    li.append(info, actions);
    list.appendChild(li);
  }
}

async function showCampaigns(id) {
  const card = $("campaigns-card");
  card.hidden = false;
  $("table").hidden = true;
  $("campaigns-empty").hidden = true;
  $("units").textContent = "";
  $("mode").textContent = "Загружаем…";
  const result = await api("GET", `/api/accounts/${id}/campaigns`);
  if (!result.ok) {
    $("campaigns-title").textContent = "Кампании";
    $("mode").textContent = errorText(result);
    return;
  }
  const data = result.data;
  $("campaigns-title").textContent = `Кампании: ${data.account.title}`;
  $("mode").textContent = data.account.sandbox
    ? "Тестовый режим: данные из песочницы, не из вашего реального кабинета."
    : "Это ваш реальный кабинет.";
  $("units").textContent = data.units
    ? `Баллы Директа на сегодня: осталось ${data.units.remaining} из ${data.units.daily_limit}.`
    : "";
  const body = $("table").querySelector("tbody");
  body.replaceChildren();
  const empty = data.campaigns.length === 0;
  $("campaigns-empty").hidden = !empty;
  $("table").hidden = empty;
  for (const c of data.campaigns) {
    const tr = document.createElement("tr");
    for (const value of [c.name, c.type, c.state, c.status]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
}

async function removeAccount(account) {
  if (!confirm(`Удалить кабинет «${account.title}» из Автобидера? В самом Яндекс Директе ничего не изменится.`)) return;
  await api("DELETE", `/api/accounts/${account.id}`);
  $("campaigns-card").hidden = true;
  await loadAccounts();
}

async function submitAccount(event) {
  event.preventDefault();
  const button = $("account-submit");
  button.disabled = true;
  setStatus("account-status", "Проверяем подключение…");
  const result = await api("POST", "/api/accounts", {
    title: $("title").value,
    token: $("token").value,
    login: $("login").value,
    sandbox: $("sandbox").checked,
    use_dev_token: $("dev-token").checked,
  });
  button.disabled = false;
  if (!result.ok) {
    setStatus("account-status", errorText(result), "err");
    return;
  }
  const n = result.data.campaigns_count;
  setStatus("account-status", `Кабинет подключён. Кампаний в нём: ${n}.`, "ok");
  $("token").value = "";
  $("title").value = "";
  await loadAccounts();
}

/* ---------- Запуск ---------- */

async function start() {
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("account-form").addEventListener("submit", submitAccount);
  $("logout").addEventListener("click", logout);

  const config = await api("GET", "/api/config");
  if (config.ok) {
    $("dev-row").hidden = !config.data.dev_token_available;
    if (config.data.token_url) {
      $("token-link").href = config.data.token_url;
      $("token-link").hidden = false;
    }
  }
  const me = await api("GET", "/api/auth/me");
  if (me.ok) showApp(me.data.email); else showAuth();
}

start();
