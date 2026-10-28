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
  if (Array.isArray(d)) {
    const own = d.map((e) => e.msg || "").find((m) => m.startsWith("Value error, "));
    return own ? own.replace("Value error, ", "") : "Проверьте, что все поля заполнены правильно.";
  }
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
  currentBusiness = null;
  $("view-auth").hidden = false;
  $("view-app").hidden = true;
  $("user").hidden = true;
  setAuthMode("login");
}

async function showApp(email) {
  currentBusiness = null; // не тащим бизнес предыдущего пользователя
  $("view-auth").hidden = true;
  $("view-app").hidden = false;
  $("user").hidden = false;
  $("user-email").textContent = email;
  $("campaigns-card").hidden = true;
  showPage("business");
  await Promise.all([loadBusinesses(), loadAccounts()]);
}

const PAGES = ["business", "stats", "history", "accounts"];

function showPage(name) {
  for (const page of PAGES) {
    $(`page-${page}`).hidden = page !== name;
    $(`nav-${page}`).classList.toggle("active", page === name);
  }
  if (name === "stats") loadStats();
  if (name === "history") { setStatus("history-status", ""); loadHistory(); }
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
  $("b-maps").value = b ? b.maps_url : "";
  $("b-tone").value = b ? b.tone : "";
  setColor("b-color-main", b ? b.color_main : "");
  setColor("b-color-extra", b ? b.color_extra : "");
  $("b-check").value = b && b.avg_check ? b.avg_check : "";
  $("b-cpa").value = b && b.target_cpa ? b.target_cpa : "";
  $("b-budget").value = b && b.weekly_budget ? b.weekly_budget : "";
  $("b-share").value = b ? b.client_share_percent : 30;
  $(b && b.mode === "auto" ? "mode-auto" : "mode-ask").checked = true;
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
  let wanted = selectId ?? (currentBusiness ? currentBusiness.id : null);
  if (!list.some((b) => b.id === wanted)) wanted = list[0] ? list[0].id : null;
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
  refreshStopbar();
  loadMetrika();
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
  $("stopbar").hidden = true;
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
    maps_url: $("b-maps").value,
    tone: $("b-tone").value,
    color_main: getColor("b-color-main"),
    color_extra: getColor("b-color-extra"),
    avg_check: num("b-check"),
    target_cpa: num("b-cpa"),
    weekly_budget: num("b-budget"),
    client_share_percent: num("b-share") || 30,
    mode: $("mode-auto").checked ? "auto" : "ask",
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

/* ---------- Яндекс Метрика ---------- */

async function loadMetrika() {
  const card = $("metrika-card");
  card.hidden = !currentBusiness;
  if (!currentBusiness) return;
  setStatus("metrika-status", "");
  $("metrika-form").hidden = true;
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/metrika/counters`);
  if (!result.ok) { $("metrika-howto").textContent = ""; setStatus("metrika-status", errorText(result), "err"); return; }
  const { counters, selected, how_to } = result.data;
  $("metrika-howto").textContent = how_to;
  if (!counters.length) {
    setStatus("metrika-status", "В вашей Метрике пока нет счётчиков. Создайте счётчик для сайта и вернитесь сюда.", "");
    return;
  }
  $("metrika-form").hidden = false;
  const counter = $("m-counter");
  counter.replaceChildren(...counters.map((c) => el("option", { value: c.id, textContent: c.site ? `${c.name} (${c.site})` : c.name })));
  if (selected.counter) counter.value = selected.counter;
  $("m-clear").hidden = !selected.counter;
  await loadGoals(selected.goal);
}

async function loadGoals(selectedGoal) {
  const goal = $("m-goal");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/metrika/counters/${$("m-counter").value}/goals`);
  if (!result.ok) { setStatus("metrika-status", errorText(result), "err"); return; }
  const goals = result.data.goals;
  goal.replaceChildren(...goals.map((g) => el("option", { value: g.id, textContent: g.name })));
  if (selectedGoal) goal.value = selectedGoal;
  if (!goals.length) setStatus("metrika-status", "В этом счётчике нет целей. Создайте цель «Обращение» в Метрике.", "");
}

async function saveMetrika() {
  const result = await api("PUT", `/api/businesses/${currentBusiness.id}/metrika`, { counter_id: $("m-counter").value, goal_id: $("m-goal").value });
  if (!result.ok) { setStatus("metrika-status", errorText(result), "err"); return; }
  setStatus("metrika-status", "Готово. Теперь сервис видит обращения с сайта.", "ok");
  $("m-clear").hidden = false;
}

async function clearMetrika() {
  await api("DELETE", `/api/businesses/${currentBusiness.id}/metrika`);
  setStatus("metrika-status", "Метрика отключена. Сервис перестанет оценивать рекламу, пока вы её не подключите снова.", "ok");
  $("m-clear").hidden = true;
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

/* ---------- Статистика и пример данных ---------- */

const money = (v) => (v == null ? "—" : `${Math.round(v).toLocaleString("ru-RU")} ₽`);

function tile(num, label) {
  return el("div", { className: "tile" }, el("div", { className: "num", textContent: num }), el("div", { className: "lbl", textContent: label }));
}

function renderChecklist(steps) {
  const box = $("checklist");
  box.replaceChildren();
  const next = steps.find((s) => !s.done && !s.soon);
  if (!next) { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("h3", { textContent: "Что нужно, чтобы реклама заработала" }));
  for (const s of steps) {
    const state = s.done ? "done" : s === next ? "next" : s.soon ? "soon" : "";
    const body = el("div", {}, el("div", { className: "title", textContent: s.title }));
    if (!s.done) {
      const hint = el("div", { className: "hint", textContent: s.hint + " " });
      if (s.page) {
        const go = el("a", { textContent: "Перейти" });
        go.addEventListener("click", () => showPage(s.page));
        hint.append(go);
      }
      body.append(hint);
    }
    box.append(el("div", { className: `step ${state}` }, el("div", { className: "mark", textContent: s.done ? "✓" : "" }), body));
  }
}

async function refreshStopbar() {
  const bar = $("stopbar");
  if (!currentBusiness) { bar.hidden = true; return; }
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/overview`);
  if (!result.ok) return;
  const data = result.data;
  bar.hidden = !data.example && !data.stopped;
  $("stop-all").hidden = data.stopped;
  $("resume-all").hidden = !data.stopped;
  $("stopbar-text").textContent = data.stopped
    ? "Вся реклама остановлена вами."
    : data.mode === "ask" ? "Сервис сначала спрашивает вас о каждом изменении." : "Сервис меняет рекламу сам и записывает всё в историю.";
}

async function stopAll() {
  if (!confirm("Остановить всю рекламу этого бизнеса? Показы прекратятся сразу. Включить обратно можно в один клик.")) return;
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/stop-all`);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); return; }
  await Promise.all([refreshStopbar(), loadStats()]);
}

async function resumeAll() {
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/resume-all`);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); return; }
  await Promise.all([refreshStopbar(), loadStats()]);
}

async function loadStats() {
  const has = !!currentBusiness;
  $("stats-empty").hidden = has;
  $("stats-body").hidden = !has;
  if (!has) return;
  $("stats-title").textContent = `Статистика: ${currentBusiness.name}`;
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/overview`);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); return; }
  const data = result.data;
  renderChecklist(data.checklist || []);
  const note = $("proposals-note");
  note.hidden = !data.proposals;
  note.replaceChildren();
  if (data.proposals) {
    const go = el("a", { textContent: "Посмотреть" });
    go.addEventListener("click", () => showPage("history"));
    note.append(`Предложений, которые ждут вашего решения: ${data.proposals}. `, go);
  }
  $("example-notice").hidden = !data.example;
  $("stats-needs").textContent = data.needs;
  $("sim-start").hidden = data.example || data.real;
  $("sim-reset").hidden = !data.example;
  $("launch-open").hidden = data.real;
  $("sync-now").hidden = !data.real;
  for (const b of document.querySelectorAll("[data-advance]")) b.hidden = !data.example;
  $("sim-day").textContent = data.example
    ? `Сегодня в примере: ${data.today}`
    : data.real
      ? (data.sync.at ? `Данные обновлены: ${new Date(data.sync.at).toLocaleString("ru-RU")}` : "Данные ещё не обновлялись.")
      : "Пока нет рекламы. Нажмите «Создать рекламу» или посмотрите, как всё работает, на примере.";
  const notices = $("notices");
  notices.replaceChildren(...(data.notices || []).map((n) => el("li", { textContent: n })));

  const tiles = $("tiles");
  tiles.hidden = !data.totals;
  tiles.replaceChildren();
  if (data.totals) {
    const t = data.totals;
    tiles.append(
      tile(money(t.spend), `Расход за ${t.days} дней`),
      tile(String(t.leads), "Обращения"),
      tile(String(t.clients), t.clients_estimated ? "Клиенты (оценка)" : "Клиенты"),
      tile(money(t.cost_per_client), t.clients_estimated ? "Цена клиента (оценка)" : "Цена клиента"),
    );
  }
  $("budget-box").hidden = !data.budget;
  if (data.budget) {
    const b = data.budget;
    $("budget-text").textContent = `Бюджет недели: потрачено ${money(b.spent_week)} из ${money(b.weekly)} (${b.percent}%).`;
    const fill = $("budget-fill");
    fill.style.width = `${Math.min(b.percent, 100)}%`;
    fill.className = "fill" + (b.percent >= 100 ? " full" : b.percent >= 90 ? " warn" : "");
  }

  const body = $("units-table").querySelector("tbody");
  body.replaceChildren();
  $("units-table").hidden = data.units.length === 0;
  $("units-hint").hidden = data.units.length === 0;
  for (const u of data.units) {
    const name = el("td", { textContent: u.title });
    if (u.note) name.append(el("span", { className: "sub", textContent: u.note }));
    if (u.in_test) name.append(el("span", { className: "sub", textContent: "Идёт сравнение двух вариантов" }));
    else if (u.variant) name.append(el("span", { className: "sub", textContent: "Второй вариант для сравнения" }));
    const cell = (v) => el("td", { textContent: v });
    const badge = el("span", { className: `badge ${u.verdict_code}`, textContent: u.verdict });
    const row = el("tr", { className: u.active ? "" : "off" }, name, el("td", {}, badge), cell(money(u.weekly_budget)),
      cell(money(u.spend)), cell(String(u.leads)), cell(String(u.clients)), cell(money(u.cost_per_client)));
    body.append(row);
  }
}

async function openLaunch() {
  if (!currentBusiness) return;
  setStatus("stats-status", "Готовим план…");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/launch/preview`);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); $("launch-card").hidden = true; return; }
  setStatus("stats-status", "");
  const p = result.data;
  $("launch-summary").textContent =
    `Кабинет: ${p.account}. Город: ${p.city}. Объявлений: ${p.count}, по ${p.per_unit} ₽ в неделю каждое (всего ${p.total} ₽). Ведёт на: ${p.link}.`;
  $("launch-variants").replaceChildren(...p.variants.map((v) =>
    el("li", {}, el("div", { className: "t", textContent: v.title }), el("div", { className: "hint", textContent: v.text }))));
  $("launch-warnings").replaceChildren(...p.warnings.map((w) => el("li", { textContent: w })));
  $("launch-card").hidden = false;
  $("launch-card").scrollIntoView({ block: "nearest" });
}

async function confirmLaunch() {
  const button = $("launch-go");
  button.disabled = true;
  setStatus("stats-status", "Создаём рекламу в Яндекс Директе…");
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/launch`, {});
  button.disabled = false;
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); return; }
  $("launch-card").hidden = true;
  setStatus("stats-status", result.data.message, "ok");
  await Promise.all([loadStats(), refreshStopbar()]);
}

async function syncNow() {
  setStatus("stats-status", "Обновляем данные из Яндекс Директа…");
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/sync`);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); await loadStats(); return; }
  setStatus("stats-status", result.data.changes ? `Готово. Новых записей в истории: ${result.data.changes}.` : "Данные обновлены.", "ok");
  await Promise.all([loadStats(), refreshStopbar()]);
}

async function simAction(path, body) {
  if (!currentBusiness) return;
  setStatus("stats-status", "Считаем…");
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/sim/${path}`, body);
  if (!result.ok) { setStatus("stats-status", errorText(result), "err"); return; }
  const made = result.data.changes_made;
  const word = currentBusiness.mode === "ask" ? "Предложений" : "Изменений";
  setStatus("stats-status", made ? `Готово. ${word} за это время: ${made}. Подробности на вкладке «История».` : "Готово.", "ok");
  await Promise.all([loadStats(), refreshStopbar()]);
}

/* ---------- История изменений ---------- */

async function loadHistory() {
  const list = $("history");
  list.replaceChildren();
  if (!currentBusiness) { $("history-empty").hidden = false; return; }
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/changes`);
  const items = result.ok ? result.data.changes : [];
  $("history-empty").hidden = items.length > 0;
  for (const c of items) {
    const info = el("div", {},
      el("div", { className: "when", textContent: `${c.day}, ${c.who}` }),
      el("div", { className: "what", textContent: c.unit ? `${c.action}: ${c.unit}` : c.action }),
      el("div", { className: "text", textContent: c.reason }),
    );
    let action;
    if (c.can_decide) {
      info.firstChild.append(el("span", { className: "pill", textContent: "Ждёт вашего решения" }));
      const yes = el("button", { className: "small", textContent: "Подтвердить" });
      yes.addEventListener("click", () => decide(c, "approve"));
      const no = el("button", { className: "small secondary", textContent: "Не делать" });
      no.addEventListener("click", () => decide(c, "reject"));
      action = el("div", { className: "btns" }, yes, no);
    } else if (c.status === "rejected") action = el("span", { className: "hint", textContent: "Отклонено" });
    else if (c.reverted) action = el("span", { className: "hint", textContent: "Отменено" });
    else if (c.can_rollback) {
      action = el("button", { className: "small secondary", textContent: "Отменить" });
      action.addEventListener("click", () => rollbackChange(c));
    } else action = el("span");
    list.append(el("li", {}, info, action));
  }
}

async function decide(change, verb) {
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/changes/${change.id}/${verb}`);
  setStatus("history-status", result.ok ? (verb === "approve" ? "Готово, сделали." : "Хорошо, ничего не меняем.") : errorText(result), result.ok ? "ok" : "err");
  await loadHistory();
}

async function rollbackChange(change, force = false) {
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/changes/${change.id}/rollback`, { force });
  if (result.status === 409 && !force) {
    if (confirm(errorText(result))) await rollbackChange(change, true);
    return;
  }
  if (!result.ok) { setStatus("history-status", errorText(result), "err"); return; }
  setStatus("history-status", "Изменение отменено.", "ok");
  await loadHistory();
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
  $("nav-stats").addEventListener("click", () => showPage("stats"));
  $("connect-direct").addEventListener("click", connectDirect);
  $("connect-cancel").addEventListener("click", stopConnect);
  $("m-counter").addEventListener("change", () => loadGoals(""));
  $("m-save").addEventListener("click", saveMetrika);
  $("m-clear").addEventListener("click", clearMetrika);
  $("launch-open").addEventListener("click", openLaunch);
  $("launch-go").addEventListener("click", confirmLaunch);
  $("launch-cancel").addEventListener("click", () => { $("launch-card").hidden = true; });
  $("sync-now").addEventListener("click", syncNow);
  $("stop-all").addEventListener("click", stopAll);
  $("resume-all").addEventListener("click", resumeAll);
  $("nav-history").addEventListener("click", () => showPage("history"));
  $("sim-start").addEventListener("click", () => simAction("start"));
  $("sim-reset").addEventListener("click", () => {
    if (confirm("Сбросить пример? Вся примерная статистика и история изменений удалятся.")) simAction("reset");
  });
  for (const button of document.querySelectorAll("[data-advance]")) {
    button.addEventListener("click", () => simAction("advance", { days: Number(button.dataset.advance) }));
  }
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
    $("yandex-box").hidden = !config.data.yandex_login;
    $("connect-direct").hidden = !config.data.direct_oauth;
    $("connect-off").hidden = !!config.data.direct_oauth;
    $("manual-card").hidden = !(config.data.dev_token_available || !config.data.direct_oauth);
    $("dev-row").hidden = !config.data.dev_token_available;
    if (config.data.token_url) {
      $("token-link").href = config.data.token_url;
      $("token-link").hidden = false;
    }
  }
  const me = await api("GET", "/api/auth/me");
  if (me.ok) await showApp(me.data.email); else showAuth();
  showLoginError();
}

const LOGIN_ERRORS = {
  denied: "Вы не разрешили вход через Яндекс. Попробуйте ещё раз или войдите по e-mail и паролю.",
  state: "Вход не удался: страница устарела. Нажмите «Войти через Яндекс ID» ещё раз.",
  failed: "Яндекс не подтвердил вход. Попробуйте ещё раз чуть позже.",
  not_configured: "Вход через Яндекс пока не настроен. Войдите по e-mail и паролю.",
};

/* ---------- Подключение Директа по коду ---------- */

let connectTimer = null;

function stopConnect() {
  clearTimeout(connectTimer);
  connectTimer = null;
  $("connect-panel").hidden = true;
  $("connect-direct").disabled = false;
}

async function connectDirect() {
  setStatus("connect-status", "");
  $("connect-direct").disabled = true;
  const started = await api("POST", "/api/direct/connect/start");
  if (!started.ok) { setStatus("connect-status", errorText(started), "err"); $("connect-direct").disabled = false; return; }
  const { pending_id, user_code, url, interval } = started.data;
  $("connect-code").textContent = user_code;
  $("connect-url").href = url;
  $("connect-url").textContent = url.replace(/^https?:\/\//, "");
  $("connect-panel").hidden = false;

  const poll = async () => {
    const result = await api("POST", "/api/direct/connect/poll", { pending_id });
    if (!result.ok) { stopConnect(); setStatus("connect-status", errorText(result), "err"); return; }
    if (result.data.status === "ok") {
      stopConnect();
      setStatus("connect-status", `Кабинет «${result.data.login}» подключён.`, "ok");
      await loadAccounts();
      loadMetrika();
      return;
    }
    if (result.data.status === "failed") { stopConnect(); setStatus("connect-status", result.data.message, "err"); return; }
    connectTimer = setTimeout(poll, Math.max(interval, 3) * 1000);
  };
  connectTimer = setTimeout(poll, Math.max(interval, 3) * 1000);
}

function showLoginError() {
  const code = new URLSearchParams(location.search).get("login_error");
  if (!code) return;
  history.replaceState(null, "", "/");
  setStatus("auth-status", LOGIN_ERRORS[code] || "Не получилось войти. Попробуйте ещё раз.", "err");
}

start();
