const $ = (id) => document.getElementById(id);

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
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

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
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

async function showApp(email) {
  $("view-auth").hidden = true;
  $("view-app").hidden = false;
  $("user").hidden = false;
  $("user-email").textContent = email;
  $("campaigns-card").hidden = true;
  showPage("business");
  await Promise.all([loadBusinesses(), loadAccounts()]);
}

function showPage(name) {
  $("page-business").hidden = name !== "business";
  $("page-accounts").hidden = name !== "accounts";
  $("nav-business").classList.toggle("active", name === "business");
  $("nav-accounts").classList.toggle("active", name === "accounts");
}

/* ---------- Бизнес ---------- */

let currentBusiness = null; // null = новый, ещё не сохранён
let options = { tones: [], photo_tags: [], limits: {} };

async function loadOptions() {
  const result = await api("GET", "/api/businesses/options");
  if (!result.ok) return;
  options = result.data;
  const tone = $("b-tone");
  for (const t of options.tones) tone.append(el("option", { value: t.value, textContent: t.label }));
  const tag = $("photo-tag");
  for (const t of options.photo_tags) tag.append(el("option", { value: t.value, textContent: t.label }));
}

function setColor(id, value) {
  const input = $(id);
  input.value = value || "#ffffff";
  input.dataset.empty = value ? "0" : "1";
}

function getColor(id) {
  const input = $(id);
  return input.dataset.empty === "1" ? "" : input.value;
}

function fillForm(b) {
  $("b-name").value = b ? b.name : "";
  $("b-kind").value = b ? b.kind : "";
  $("b-description").value = b ? b.description : "";
  $("b-city").value = b ? b.city : "";
  $("b-site").value = b ? b.site_url : "";
  $("b-tone").value = b ? b.tone : "";
  setColor("b-color-main", b ? b.color_main : "");
  setColor("b-color-extra", b ? b.color_extra : "");
  $("b-check").value = b && b.avg_check ? b.avg_check : "";
  $("b-cpa").value = b && b.target_cpa ? b.target_cpa : "";
  $("b-budget").value = b && b.weekly_budget ? b.weekly_budget : "";
  $("business-title").textContent = b ? `Бизнес: ${b.name}` : "Расскажите о бизнесе";
  $("business-delete").hidden = !b;
  setStatus("business-status", "");
  setStatus("files-status", "");
}

async function loadBusinesses(selectId) {
  const result = await api("GET", "/api/businesses");
  if (result.status === 401) { showAuth(); return; }
  const list = result.ok ? result.data.businesses : [];
  const box = $("business-list");
  box.replaceChildren();
  const wanted = selectId ?? (currentBusiness ? currentBusiness.id : (list[0] ? list[0].id : null));
  for (const b of list) {
    const chip = el("button", { className: "chip" + (b.id === wanted ? " active" : ""), textContent: b.name });
    chip.addEventListener("click", () => selectBusiness(b.id));
    box.append(chip);
  }
  if (wanted) await selectBusiness(wanted, false); else newBusiness();
}

async function selectBusiness(id, refreshList = true) {
  const result = await api("GET", `/api/businesses/${id}`);
  if (!result.ok) return;
  currentBusiness = result.data;
  fillForm(currentBusiness);
  renderFiles();
  if (refreshList) {
    for (const chip of $("business-list").children) {
      chip.classList.toggle("active", chip.textContent === currentBusiness.name);
    }
  }
}

function newBusiness() {
  currentBusiness = null;
  fillForm(null);
  renderFiles();
  for (const chip of $("business-list").children) chip.classList.remove("active");
}

function readForm() {
  const num = (id) => Number($(id).value) || 0;
  return {
    name: $("b-name").value,
    kind: $("b-kind").value,
    description: $("b-description").value,
    city: $("b-city").value,
    site_url: $("b-site").value,
    tone: $("b-tone").value,
    color_main: getColor("b-color-main"),
    color_extra: getColor("b-color-extra"),
    avg_check: num("b-check"),
    target_cpa: num("b-cpa"),
    weekly_budget: num("b-budget"),
  };
}

async function saveBusiness(event) {
  event.preventDefault();
  const button = $("business-save");
  button.disabled = true;
  setStatus("business-status", "Сохраняем…");
  const result = currentBusiness
    ? await api("PUT", `/api/businesses/${currentBusiness.id}`, readForm())
    : await api("POST", "/api/businesses", readForm());
  button.disabled = false;
  if (!result.ok) {
    setStatus("business-status", errorText(result), "err");
    return;
  }
  const isNew = !currentBusiness;
  currentBusiness = result.data;
  await loadBusinesses(currentBusiness.id);
  setStatus("business-status", isNew ? "Готово. Теперь загрузите фото и логотип ниже." : "Сохранено.", "ok");
}

async function deleteBusiness() {
  if (!currentBusiness) return;
  if (!confirm(`Удалить бизнес «${currentBusiness.name}» вместе с загруженными файлами?`)) return;
  await api("DELETE", `/api/businesses/${currentBusiness.id}`);
  currentBusiness = null;
  await loadBusinesses();
}

/* ---------- Файлы бизнеса ---------- */

function renderFiles() {
  const saved = !!currentBusiness;
  $("files-locked").hidden = saved;
  $("files").hidden = !saved;
  if (!saved) return;
  const assets = currentBusiness.assets || [];

  const logoBox = $("logo-box");
  logoBox.replaceChildren();
  const logo = assets.find((a) => a.kind === "logo");
  if (logo) {
    logoBox.append(el("img", { src: logo.url, alt: "Логотип" }), " ");
    const remove = el("button", { type: "button", className: "small secondary", textContent: "Удалить логотип" });
    remove.addEventListener("click", () => removeAsset(logo.id));
    logoBox.append(remove);
  } else {
    logoBox.append(el("span", { className: "hint", textContent: "Логотип пока не загружен." }));
  }

  fillGrid($("photos"), assets.filter((a) => a.kind === "photo"), (a) =>
    el("img", { src: a.url, alt: a.caption || a.tag_label, loading: "lazy" }));
  fillGrid($("videos"), assets.filter((a) => a.kind === "video"), (a) =>
    el("video", { src: a.url, controls: true, preload: "metadata" }));
}

function fillGrid(grid, items, makeMedia) {
  grid.replaceChildren();
  for (const a of items) {
    const remove = el("button", { type: "button", textContent: "Удалить", title: "Удалить файл" });
    remove.addEventListener("click", () => removeAsset(a.id));
    grid.append(el("div", { className: "thumb" }, makeMedia(a), remove, el("div", { className: "cap", textContent: a.tag_label || a.name })));
  }
}

async function uploadFiles(files, kind, tag) {
  if (!currentBusiness || !files.length) return;
  let done = 0;
  for (const file of files) {
    setStatus("files-status", `Загружаем ${file.name}…`);
    const form = new FormData();
    form.append("kind", kind);
    form.append("tag", tag || "");
    form.append("file", file);
    const result = await api("POST", `/api/businesses/${currentBusiness.id}/assets`, form);
    if (!result.ok) {
      setStatus("files-status", `${file.name}: ${errorText(result)}`, "err");
      break;
    }
    done += 1;
  }
  await refreshAssets();
  if (done === files.length) setStatus("files-status", done === 1 ? "Файл загружен." : `Загружено файлов: ${done}.`, "ok");
}

async function refreshAssets() {
  const result = await api("GET", `/api/businesses/${currentBusiness.id}`);
  if (result.ok) { currentBusiness = result.data; renderFiles(); }
}

async function removeAsset(assetId) {
  await api("DELETE", `/api/businesses/${currentBusiness.id}/assets/${assetId}`);
  setStatus("files-status", "");
  await refreshAssets();
}

/* ---------- Кабинеты ---------- */

async function loadAccounts() {
  const result = await api("GET", "/api/accounts");
  if (result.status === 401) { showAuth(); return; }
  const list = $("accounts");
  list.replaceChildren();
  const accounts = result.ok ? result.data.accounts : [];
  $("accounts-empty").hidden = accounts.length > 0;
  for (const account of accounts) {
    const info = el("div", {}, el("strong", { textContent: account.title }), el("span", { className: "hint", textContent: ` ${account.platform}` }));
    const view = el("button", { className: "small", textContent: "Показать кампании" });
    view.addEventListener("click", () => showCampaigns(account.id));
    const remove = el("button", { className: "small secondary", textContent: "Удалить" });
    remove.addEventListener("click", () => removeAccount(account));
    list.append(el("li", {}, info, el("div", {}, view, remove)));
  }
}

async function showCampaigns(id) {
  $("campaigns-card").hidden = false;
  $("table").hidden = true;
  $("campaigns-empty").hidden = true;
  $("units").textContent = "Загружаем…";
  const result = await api("GET", `/api/accounts/${id}/campaigns`);
  if (!result.ok) {
    $("campaigns-title").textContent = "Кампании";
    $("units").textContent = errorText(result);
    return;
  }
  const data = result.data;
  $("campaigns-title").textContent = `Кампании: ${data.account.title}`;
  $("units").textContent = data.units
    ? `Баллы Директа на сегодня: осталось ${data.units.remaining} из ${data.units.daily_limit}.`
    : "";
  const body = $("table").querySelector("tbody");
  body.replaceChildren();
  const empty = data.campaigns.length === 0;
  $("campaigns-empty").hidden = !empty;
  $("table").hidden = empty;
  for (const c of data.campaigns) {
    body.append(el("tr", {}, ...[c.name, c.type, c.state, c.status].map((v) => el("td", { textContent: v }))));
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
    use_dev_token: $("dev-token").checked,
  });
  button.disabled = false;
  if (!result.ok) {
    setStatus("account-status", errorText(result), "err");
    return;
  }
  setStatus("account-status", `Кабинет подключён. Кампаний в нём: ${result.data.campaigns_count}.`, "ok");
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

  $("nav-business").addEventListener("click", () => showPage("business"));
  $("nav-accounts").addEventListener("click", () => showPage("accounts"));
  $("business-form").addEventListener("submit", saveBusiness);
  $("business-new").addEventListener("click", newBusiness);
  $("business-delete").addEventListener("click", deleteBusiness);
  for (const id of ["b-color-main", "b-color-extra"]) {
    $(id).addEventListener("input", () => { $(id).dataset.empty = "0"; });
  }
  for (const button of document.querySelectorAll("[data-clear]")) {
    button.addEventListener("click", () => setColor(button.dataset.clear, ""));
  }
  $("logo-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "logo"); e.target.value = ""; });
  $("photo-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "photo", $("photo-tag").value); e.target.value = ""; });
  $("video-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "video"); e.target.value = ""; });

  await loadOptions();
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
