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
  document.body.classList.add("guest");   // гость видит главную с примером без бокового меню
  $("view-auth").hidden = false;
  $("view-app").hidden = true;
  $("user").hidden = true;
  $("nav").hidden = true;
  $("biz-switch").hidden = true;
  $("biz-menu").hidden = true;
  setAuthMode("register");
}

// Кнопки главной для гостей ведут к форме внизу страницы; «Войти» переключает её на вход.
function goToAuth(mode) {
  setAuthMode(mode);
  $("start").scrollIntoView({ behavior: "smooth" });
  $("email").focus({ preventScroll: true });
}

async function showApp(email) {
  currentBusiness = null; // не тащим бизнес предыдущего пользователя
  document.body.classList.remove("guest");
  $("view-auth").hidden = true;
  $("view-app").hidden = false;
  $("user").hidden = false;
  $("nav").hidden = false;
  $("user-email").textContent = email;
  $("campaigns-card").hidden = true;
  await Promise.all([loadBusinesses(), loadAccounts()]);
  const page = pageFromHash();
  if (location.hash !== `#/${page}`) history.replaceState(null, "", `#/${page}`);
  showPage(page, false);
}

const PAGES = ["home", "business", "creatives", "media", "stats", "accounts"];
let currentPage = "home";

// Адрес страницы лежит в ссылке (#/business): перезагрузка остаётся на месте, «назад» и «вперёд» работают.
// История изменений теперь внизу «Результатов»: старая ссылка #/history ведёт туда.
function pageFromHash() {
  const name = location.hash.replace(/^#\/?/, "");
  if (name === "history") return "stats";
  return PAGES.includes(name) ? name : "home";
}

function showPage(name, push = true) {
  if (push && location.hash !== `#/${name}`) history.pushState(null, "", `#/${name}`);
  currentPage = name;
  for (const page of PAGES) {
    $(`page-${page}`).hidden = page !== name;
    $(`nav-${page}`).classList.toggle("active", page === name);
  }
  window.scrollTo(0, 0);
  if (name !== "creatives" && name !== "media") clearTimeout(creativeTimer);
  if (name === "creatives" || name === "media") loadCreatives();
  if (name === "home" || name === "stats") loadStats();
  if (name === "stats") { setStatus("history-status", ""); loadHistory(); }
  if (name === "business") loadMarking();
  if (name === "accounts") {
    loadTelegram();
    if (accountsList.length) showCampaigns(accountsList[0].id);  // кампании кабинета видны сразу
  }
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

// Рекомендация по бюджету: считается на сервере по цифрам из формы, пока человек печатает (бизнес сохранять не нужно)
let adviceTimer = 0;
function scheduleAdvice() {
  clearTimeout(adviceTimer);
  adviceTimer = setTimeout(loadAdvice, 450);
}

async function loadAdvice() {
  const box = $("b-advice");
  const f = readForm();
  const r = await api("POST", "/api/businesses/budget-advice", {
    weekly_budget: f.weekly_budget, avg_check: f.avg_check, target_cpa: f.target_cpa, client_share_percent: f.client_share_percent, description: f.description,
  });
  if (!r.ok) { box.hidden = true; return; }
  box.className = `advice ${r.data.level}`;
  box.replaceChildren(el("div", { className: "sum", textContent: r.data.summary }), ...r.data.details.map((d) => el("p", { className: "hint", textContent: d })));
  box.hidden = false;
}

// Показ по всей России: город не нужен, поле отключаем и объясняем почему
function syncNationwide() {
  const on = $("b-nationwide").checked;
  $("b-city").disabled = on;
  $("b-districts-box").hidden = on;   // по всей России района нет
  $("b-city-hint").textContent = on ? "Рекламу покажем по всей России, город не нужен." : "Только название города: по нему сервис выбирает, где показывать рекламу.";
}

function fillForm(b) {
  $("b-name").value = b ? b.name : "";
  $("b-kind").value = b ? b.kind : "";
  $("b-description").value = b ? b.description : "";
  $("b-city").value = b ? b.city : "";
  $("b-nationwide").checked = !!(b && b.nationwide);
  syncNationwide();
  $("b-address").value = b ? (b.address || "") : "";
  $("b-districts").value = b ? (b.districts || "") : "";
  $("b-promo").checked = !!(b && b.allow_promo);
  $("b-site").value = b ? b.site_url : "";
  $("b-maps").value = b ? b.maps_url : "";
  $("b-tone").value = b ? b.tone : "";
  $("b-visual").value = b ? b.visual_hint || "" : "";
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
  loadAdvice();
}

function rememberedBusiness() {
  try { return Number(localStorage.getItem("autobider.business")) || null; } catch (_) { return null; }
}

function rememberBusiness(id) {
  try { localStorage.setItem("autobider.business", String(id)); } catch (_) { /* без памяти браузера живём как раньше */ }
}

async function loadBusinesses(selectId) {
  const result = await api("GET", "/api/businesses");
  if (result.status === 401) { showAuth(); return; }
  const list = result.ok ? result.data.businesses : [];
  const box = $("business-list");
  box.replaceChildren();
  let wanted = selectId ?? (currentBusiness ? currentBusiness.id : rememberedBusiness());
  if (!list.some((b) => b.id === wanted)) wanted = list[0] ? list[0].id : null;
  for (const b of list) {
    const chip = el("button", { className: "chip" + (b.id === wanted ? " active" : ""), textContent: b.name });
    chip.addEventListener("click", () => selectBusiness(b.id));
    box.append(chip);
  }
  businessList = list;
  if (wanted) await selectBusiness(wanted, false); else newBusiness();
  renderSwitch();
}

let businessList = [];

// Переключатель бизнеса в шапке: виден на любой странице, если бизнесов несколько.
function renderSwitch() {
  const sw = $("biz-switch");
  sw.replaceChildren(...businessList.map((b) => el("option", { value: String(b.id), textContent: b.name })),
    el("option", { value: "new", textContent: "＋ Новый бизнес" }));
  sw.value = currentBusiness ? String(currentBusiness.id) : "new";
  sw.hidden = $("view-app").hidden || businessList.length < 1;
  syncBizMenu();
}

// Своё меню выбора бизнеса вместо системного списка: он рисуется браузером и выбивается из оформления.
// Настоящий <select> остаётся скрытым и хранит выбранное, меню только показывает его и меняет.
function syncBizMenu() {
  const sw = $("biz-switch");
  $("biz-menu").hidden = sw.hidden;
  const current = [...sw.options].find((o) => o.value === sw.value);
  $("biz-btn-label").textContent = current && current.value !== "new" ? current.textContent : "Новый бизнес";
  $("biz-list").replaceChildren(...[...sw.options].map((o) => {
    const isNew = o.value === "new";
    const item = el("button", { type: "button", className: "biz-item" + (o.value === sw.value ? " sel" : "") + (isNew ? " new" : "") },
      isNew ? el("span", { className: "plus", textContent: "+" }) : "", el("span", { className: "nm", textContent: isNew ? "Новый бизнес" : o.textContent }),
      !isNew && o.value === sw.value ? el("span", { className: "tick", textContent: "✓" }) : "");
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(o.value === sw.value));
    item.addEventListener("click", () => {
      setBizMenu(false);
      if (o.value === sw.value) return;
      sw.value = o.value;
      sw.dispatchEvent(new Event("change"));
    });
    return item;
  }));
}

function setBizMenu(open) {
  $("biz-list").hidden = !open;
  $("biz-btn").setAttribute("aria-expanded", String(open));
  if (open) ($("biz-list").querySelector(".sel") || $("biz-list").querySelector(".biz-item"))?.focus();
}

function bindBizMenu() {
  $("biz-btn").addEventListener("click", () => setBizMenu($("biz-list").hidden));
  document.addEventListener("click", (e) => { if (!$("biz-menu").contains(e.target)) setBizMenu(false); });
  $("biz-menu").addEventListener("keydown", (e) => {
    const items = [...$("biz-list").querySelectorAll(".biz-item")];
    const at = items.indexOf(document.activeElement);
    if (e.key === "Escape" && !$("biz-list").hidden) { setBizMenu(false); $("biz-btn").focus(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if ($("biz-list").hidden) { setBizMenu(true); return; }
      items[(at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    }
  });
}

// Просмотр картинки по клику: фоновые, баннеры из истории, фото, логотип и превью в редакторе открываются крупно.
function openLightbox(img) {
  $("lightbox-img").src = img.currentSrc;   // с меткой ?t=: адрес баннера зависит от места объявления в списке, без метки браузер отдаёт картинку, которая стояла на этом месте раньше
  $("lightbox-img").alt = img.alt || "";
  const cap = img.closest(".thumb")?.querySelector(".cap");
  $("lightbox-cap").textContent = (cap ? cap.textContent : img.alt) || "";
  $("lightbox").hidden = false;
  $("lightbox-close").focus();
}

function bindLightbox() {
  const close = () => { $("lightbox").hidden = true; $("lightbox-img").removeAttribute("src"); };
  document.addEventListener("click", (e) => {
    const img = e.target instanceof Element ? e.target.closest(".thumb img, .logo-box img, .ed-preview img") : null;
    if (img && !$("view-app").hidden) openLightbox(img);
  });
  $("lightbox").addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("lightbox").hidden) close(); });
}

async function onSwitch() {
  const value = $("biz-switch").value;
  if (value === "new") { newBusiness(); showPage("business"); return; }
  await selectBusiness(Number(value));
  showPage(currentPage, false);
}

async function selectBusiness(id, refreshList = true) {
  const result = await api("GET", `/api/businesses/${id}`);
  if (!result.ok) return;
  currentBusiness = result.data;
  rememberBusiness(currentBusiness.id);
  fillForm(currentBusiness);
  renderFiles();
  refreshStopbar();
  loadMetrika();
  loadMarking();
  if ($("biz-switch").options.length) { $("biz-switch").value = String(currentBusiness.id); syncBizMenu(); }
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
  if ($("biz-switch").options.length) { $("biz-switch").value = "new"; syncBizMenu(); }
  for (const chip of $("business-list").children) chip.classList.remove("active");
}

function readForm() {
  const num = (id) => Number($(id).value) || 0;
  return {
    name: $("b-name").value,
    kind: $("b-kind").value,
    description: $("b-description").value,
    city: $("b-nationwide").checked ? "" : $("b-city").value,
    nationwide: $("b-nationwide").checked,
    address: $("b-address").value,
    districts: $("b-nationwide").checked ? "" : $("b-districts").value,
    allow_promo: $("b-promo").checked,
    site_url: $("b-site").value,
    maps_url: $("b-maps").value,
    visual_hint: $("b-visual").value,
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
  const isNew = !currentBusiness, renamed = !isNew && currentBusiness.name !== result.data.name;
  const guessed = !$("b-districts").value.trim() && result.data.districts;   // поле было пустым, а район определили по адресу
  currentBusiness = result.data;
  await loadBusinesses(currentBusiness.id);
  setStatus("business-status", "");
  say(isNew ? "Бизнес сохранён. Ниже можно загрузить фото и логотип." : "Сохранено.");
  if (guessed) say(`Район определён по адресу: ${result.data.districts}. Проверьте его в поле «Районы».`);
  if (renamed) api("POST", `/api/businesses/${currentBusiness.id}/creatives/photos/check`).then(refreshAssets);   // забракованные фото ИИ посмотрит заново: вывеска с новым названием своя
}

async function deleteBusiness() {
  if (!currentBusiness) return;
  if (!(await askConfirm("Удалить бизнес?", `«${currentBusiness.name}» будет удалён вместе с загруженными файлами.`, "Удалить"))) return;
  await api("DELETE", `/api/businesses/${currentBusiness.id}`);
  currentBusiness = null;
  await loadBusinesses();
}

/* ---------- Креативы: тексты, картинки, видео ---------- */

let creativeTimer = null;
let creativeLimits = { title: 56, text: 81 };
const autoTexts = new Set();
let creativePrices = { image: 0.08, video: 0.7 };
const usd = (v) => `$${v.toFixed(2)}`;

function counterEl(input, limit) {
  const span = el("span", { className: "counter" });
  const update = () => {
    span.textContent = `${input.value.length} из ${limit}`;
    span.classList.toggle("over", input.value.length > limit);
  };
  input.addEventListener("input", update);
  update();
  return span;
}

async function creativeCall(method, path, body) {
  return api(method, `/api/businesses/${currentBusiness.id}/creatives${path}`, body);
}

async function loadCreatives() {
  clearTimeout(creativeTimer);
  const has = !!currentBusiness;
  $("cr-empty").hidden = has;
  $("cr-body").hidden = !has;
  $("media-empty").hidden = has;
  $("media-body").hidden = !has;
  if (!has) return;
  const result = await creativeCall("GET", "");
  if (!result.ok) { setStatus("cr-texts-status", errorText(result), "err"); return; }
  const { creatives, caps, spend, limits, prices_usd } = result.data;
  creativeLimits = limits;
  if (prices_usd) creativePrices = prices_usd;

  const note = $("cr-ai-note");
  note.hidden = caps.text_ai;
  note.textContent = "ИИ для текстов пока не подключён на сервере, поэтому варианты собираются по шаблону. Их можно править и писать свои.";
  $("cr-image-note").hidden = caps.image_ai;
  $("cr-image-note").textContent = "Создание картинок и видео пока не подключено на сервере. Свои фото можно загрузить выше, в этом же разделе.";
  $("cr-image-form").hidden = !caps.image_ai;
  $("cr-spend").textContent = caps.image_ai || caps.text_ai
    ? `Потрачено на создание в этом месяце: $${spend.spent_usd.toFixed(2)} из $${spend.limit_usd.toFixed(0)}, всего с начала: $${(spend.total_usd ?? spend.spent_usd).toFixed(2)}. Больше лимита сервис не потратит, 1-го числа он обнуляется.` : "";

  renderTexts(creatives.filter((c) => c.kind === "text").reverse());  // старые сверху, новые снизу
  const images = creatives.filter((c) => c.kind === "image");
  const videos = creatives.filter((c) => c.kind === "video");
  renderMedia($("cr-images"), images, caps.video_ai);
  renderMedia($("cr-videos"), videos, false);
  renderBannerHistory(creatives.filter((c) => c.kind === "ai_banner"));
  lastCreatives = creatives;
  caps_text_ai = caps.text_ai;
  $("briefs-gen").hidden = $("briefs-count").hidden = !caps.text_ai;
  loadBriefs();
  $("cr-videos-empty").hidden = videos.length > 0;
  loadAds();
  const hasAiText = creatives.some((c) => c.kind === "text" && c.source === "claude");
  if (caps.text_ai && !hasAiText && !autoTexts.has(currentBusiness.id) && (currentBusiness.description || "").trim().length >= 40) {
    autoTexts.add(currentBusiness.id);  // один раз на бизнес: ИИ пишет тексты сам, чтобы вместо шаблонов были настоящие варианты
    generateTexts(true);
  }
  if (creatives.some((c) => c.status === "working")) creativeTimer = setTimeout(loadCreatives, 6000);
}

// ---------- Страница «Объявления»: три шага (идеи, баннеры, запуск) ----------
let crStep = null;               // текущий шаг
const stepInit = new Set();      // для каких бизнесов шаг уже выбран автоматически
let selectedAd = null;           // выбранное объявление в редакторе (по заголовку и тексту)
let lastAds = [];
let lastCreatives = [];
let lastPhotos = [];
let lastStyles = { looks: [], refs: [] };
let lastRefExtra = 0.02;
let lastCount = 0;
let lastAdvised = 0;   // сколько объявлений подходит бюджету
const adKey = (a) => `${a.title}\u0000${a.text}`;

function setStep(step) {
  crStep = step;
  for (const s of ["ideas", "banners", "launch"]) {
    $(`step-${s}`).hidden = s !== step;
    $(`step-btn-${s}`).classList.toggle("active", s === step);
  }
  window.scrollTo({ top: 0 });
  if (step === "launch") loadAudience();
}

/* ---------- Профиль аудитории ---------- */

let audience = null;        // последний ответ /audience
let audienceRebuild = false; // повторное нажатие «Составить заново» подтверждает, что правки владельца заменятся

const CATEGORY_HINTS = {
  exact: "запросы, на которые объявление отвечает точно",
  narrow: "более узкие и конкретные запросы",
  alternative: "запросы про похожую услугу или аналог",
  accessory: "запросы про сопутствующие услуги и товары",
  broader: "широкие запросы по теме: самые дорогие и самые случайные",
};

function switches(boxId, labels, state, hints = {}) {
  $(boxId).replaceChildren(...Object.entries(labels).map(([key, title]) => {
    const input = el("input", { type: "checkbox", checked: Boolean(state[key]) });
    input.dataset.key = key;
    return el("label", { className: "check" }, input, ` ${title}${hints[key] ? `: ${hints[key]}` : ""}`);
  }));
}

function launchedText(info) {
  if (!info) return "";
  const parts = [`В Директ ушло ${info.added} ${plural(info.added, "фраза", "фразы", "фраз")}.`];
  if (info.rejected.length) parts.push(`Директ не принял: ${info.rejected.map((r) => `«${r.phrase}» (${r.reason})`).join("; ")}.`);
  parts.push(info.autotargeting ? "Автотаргетинг включён." : `Автотаргетинг не включился: ${info.autotargeting_error || "Директ не принял настройки"}.`);
  return parts.join(" ");
}

function renderAudience(data) {
  audience = data;
  audienceRebuild = false;
  const p = data.profile;
  $("aud-view").hidden = !p;
  $("aud-build").textContent = p ? "Составить заново" : "Составить профиль";
  $("aud-build").hidden = !p && !data.ai_ready;
  if (!p) return;
  const L = data.labels;
  $("aud-summary").textContent = p.summary;
  const facts = [`Пол: ${L.gender[p.gender]}`, `Возраст: ${p.ages.length ? p.ages.join(", ") : "любой"}`,
    `Платёжеспособность: ${L.income[p.income]}`, `Уверенность ИИ: ${L.confidence[p.confidence]}`];
  $("aud-facts").replaceChildren(...facts.map((t) => el("span", { className: "badge", textContent: t })));
  $("aud-note").textContent = [p.need && `С чем приходит человек: ${p.need}.`, p.note].filter(Boolean).join(" ");
  $("aud-gender").replaceChildren(...Object.entries(L.gender).map(([v, t]) => el("option", { value: v, textContent: t })));
  $("aud-gender").value = p.gender;
  switches("aud-ages", Object.fromEntries(L.ages.map((a) => [a, a])), Object.fromEntries(p.ages.map((a) => [a, true])));
  renderBids(data.bids);
  $("aud-keywords").value = data.keywords.join("\n");
  $("aud-negatives").value = (p.negatives || []).join("\n");
  $("aud-negatives-hint").textContent = `В Директ уйдёт минус-слов: ${data.negatives.length}. Общий список вашей сферы добавляется сам, слова, которые режут вашу услугу, сервис не пропустит.`;
  switches("aud-categories", L.categories, data.autotargeting.categories, CATEGORY_HINTS);
  switches("aud-brands", L.brands, data.autotargeting.brands);
  const launched = launchedText(p.launched);
  $("aud-launched").hidden = !launched;
  $("aud-launched").textContent = launched;
}

function renderBids(b) {
  const hint = [];
  if (!b.needed) hint.push("Профиль не привязан ни к полу, ни к возрасту, поэтому ставки менять не нужно.");
  else {
    hint.push(`Целевым группам ставка выше на ${b.up}%, остальным ниже на ${b.down}%. Никого не отключаем: профиль ИИ может ошибаться, а настоящие данные покажут клики. В кампании заменятся прежние корректировки по полу и возрасту.`);
    if (!b.launched) hint.push("Поставить их можно после запуска рекламы.");
    if (b.applied) hint.push(`Применено ${b.applied.day}.${b.stale ? " Профиль изменился после этого: примените заново." : ""}`);
  }
  $("aud-bids-hint").textContent = hint.join(" ");
  const shown = b.applied && !b.stale ? b.applied.slices : b.plan;
  $("aud-bids-list").replaceChildren(...shown.map((s) => el("span", { className: "badge", textContent: `${s.label}: ${s.percent}%` })));
  $("aud-bids-apply").hidden = !(b.needed && b.launched);
  $("aud-bids-apply").textContent = b.applied ? "Применить заново" : "Применить в Директе";
  $("aud-bids-clear").hidden = !b.applied;
}

async function changeBids(method) {
  const buttons = [$("aud-bids-apply"), $("aud-bids-clear")];
  buttons.forEach((b) => { b.disabled = true; });
  const r = await api(method, `/api/businesses/${currentBusiness.id}/audience/bids`);
  buttons.forEach((b) => { b.disabled = false; });
  if (!r.ok) { setStatus("aud-status", errorText(r), "err"); return; }
  renderAudience(r.data);
  setStatus("aud-status", method === "POST" ? "Корректировки ставок поставлены в Директе." : "Корректировки ставок убраны.", "ok");
}

async function loadAudience() {
  if (!currentBusiness) return;
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/audience`);
  if (r.ok) renderAudience(r.data);
}

async function buildAudience() {
  if (audience && audience.profile && audience.profile.edited && !audienceRebuild) {
    audienceRebuild = true;
    setStatus("aud-status", "Ваши правки заменятся новым профилем. Нажмите ещё раз, если согласны.", "");
    return;
  }
  const button = $("aud-build");
  button.disabled = true;
  setStatus("aud-status", "ИИ разбирает вашего клиента…", "");
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/audience`);
  button.disabled = false;
  if (!r.ok) { setStatus("aud-status", errorText(r), "err"); return; }
  renderAudience(r.data);
  setStatus("aud-status", "Профиль составлен. Проверьте фразы: если что-то поправите, нажмите «Сохранить правки».", "ok");
}

async function saveAudience() {
  const lines = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
  const flags = (boxId) => Object.fromEntries([...$(boxId).querySelectorAll("input")].map((i) => [i.dataset.key, i.checked]));
  const r = await api("PUT", `/api/businesses/${currentBusiness.id}/audience`, {
    keywords: lines("aud-keywords"), negatives: lines("aud-negatives"), categories: flags("aud-categories"), brands: flags("aud-brands"),
    gender: $("aud-gender").value, ages: Object.entries(flags("aud-ages")).filter(([, on]) => on).map(([a]) => a) });
  if (!r.ok) { setStatus("aud-status", errorText(r), "err"); return; }
  const ignored = [...r.data.ignored.keywords, ...r.data.ignored.negatives];
  renderAudience(r.data);
  setStatus("aud-status", ignored.length ? `Сохранено. Не принято (знаки, длина или слишком короткое слово): ${ignored.join(", ")}.` : "Сохранено.", ignored.length ? "" : "ok");
}

// Цена одного баннера: референс картинкой (баннер из фото) стоит чуть дороже, словами (без фото) бесплатно.
function adPrice(a) {
  return (creativePrices.ai_banner || 0.07) + (a.ref_id ? lastRefExtra : 0);
}

// «Нарисовать все недостающие» рисует все объявления списка, и кампании, и запасные
const missingAds = () => lastAds.filter((a) => a.ai && a.ai.status !== "ready" && a.ai.status !== "working");

async function makeAllBanners() {
  const need = missingAds().length;
  if (!need) { say("Все баннеры уже нарисованы или рисуются."); return; }
  const price = usd(missingAds().reduce((sum, a) => sum + adPrice(a), 0));
  if (!(await askConfirm("Нарисовать недостающие баннеры?", `Баннеров: ${need}, это платно: около ${price}. Нейросеть делает каждый целиком, текст на них сервис проверит сам. Готовятся около минуты.`, "Нарисовать"))) return;
  const r = await creativeCall("POST", "/ads/ai-banners");
  if (!r.ok) { say(errorText(r), "err"); return; }
  say(`Заказано баннеров: ${r.data.started}. Они появятся сами.`);
  await loadCreatives();
}

// «Перерисовать все»: заново рисует все объявления списка, кроме тех, что рисуются прямо сейчас
const redrawAds = () => lastAds.filter((a) => !(a.ai && a.ai.status === "working"));

async function redrawAllBanners() {
  const list = redrawAds();
  if (!list.length) { say("Сейчас все баннеры рисуются, подождите минуту."); return; }
  const price = usd(list.reduce((sum, a) => sum + adPrice(a), 0));
  if (!(await askConfirm("Перерисовать все баннеры?", `Баннеров: ${list.length}, это платно: около ${price}. Каждый объявлению нарисуется заново, с другой подачей. Прежние версии останутся: их можно вернуть в редакторе. Готовятся около минуты.`, "Перерисовать"))) return;
  const r = await creativeCall("POST", "/ads/ai-banners?redo=true");
  if (!r.ok) { say(errorText(r), "err"); return; }
  say(`Заказано баннеров: ${r.data.started}. Они появятся сами.`);
  await loadCreatives();
}

const AD_STATUS = {
  ready: ["Готов", "ok"], working: ["Рисуется…", "work"], failed: ["Не вышло, нарисуйте заново", "err"], none: ["Нужен баннер", "warn"],
};

function adStatus(a) {
  return AD_STATUS[(a.ai && a.ai.status) || "none"] || AD_STATUS.none;
}

async function loadAds() {
  const result = await creativeCall("GET", "/ads");
  if (!result.ok) return;
  const { ads, count, advised, weekly, site, has_maps, has_images, rejected_photos, photos, styles, ref_extra } = result.data;
  lastAds = ads;
  lastPhotos = photos || [];
  lastStyles = styles || { looks: [], refs: [] };
  lastRefExtra = ref_extra ?? 0.02;
  lastCount = count;
  lastAdvised = advised || 0;
  if (!selectedAd || !ads.some((a) => adKey(a) === selectedAd)) selectedAd = ads.length ? adKey(ads[0]) : null;
  if (!stepInit.has(currentBusiness.id)) {   // первый заход: с идей, а если объявления уже выбраны, то сразу к баннерам
    stepInit.add(currentBusiness.id);
    setStep(ads.some((a) => a.own) || !caps_text_ai ? "banners" : "ideas");
  } else if (!crStep) setStep("ideas");

  const target = site || (has_maps ? "вашу страницу в Яндекс Картах" : "");
  const extra = count > lastAdvised && lastAdvised ? ` Бюджету подходит ${lastAdvised}: чем больше объявлений, тем меньше кликов достанется каждому и тем дольше сравнение.` : "";
  $("ads-summary").textContent = !count
    ? "Бюджет слишком мал для запуска: увеличьте его на странице «Бизнес». Так будут выглядеть объявления."
    : target
      ? `При запуске будет создана одна кампания: ${adsPhrase(count)} и бюджет ${weekly} ₽ в неделю на все. В показ пойдут первые ${count} из списка, остальные запасные.${extra} Объявления ведут на ${target}.`
      : `При запуске будет создана одна кампания: ${adsPhrase(count)} и бюджет ${weekly} ₽ в неделю на все. Чтобы запустить, укажите сайт или ссылку на Яндекс Карты на странице «Бизнес»: объявлению нужно куда вести.`;
  const note = $("ads-note");
  note.hidden = has_images;
  note.textContent = rejected_photos
    ? "Загруженные фото не подошли для баннеров: ИИ нашёл на них свой текст, чужой бренд или водяной знак (подробности на странице «Бизнес»). Загрузите другие фото или нарисуйте фоновые картинки в библиотеке ниже."
    : "Фото пока нет. Загрузите свои на странице «Бизнес» или нарисуйте фоновые картинки в библиотеке ниже: баннеры соберутся сами.";
  renderBannerStep(true);
  renderLaunchStep();
  updateIdeasCount();
}

let caps_text_ai = true;   // можно ли просить ИИ писать тексты и придумывать идеи (приходит вместе со списком материалов)

function renderBannerStep(fromPoll = false) {
  const ready = lastAds.filter((a) => a.ai && a.ai.status === "ready").length;
  const need = missingAds().length;
  $("banners-count").textContent = lastAds.length ? `Баннеры готовы у ${ready} из ${lastAds.length} объявлений.` : "";
  const all = $("ads-ai-all");
  all.hidden = need === 0;
  all.textContent = `Нарисовать все недостающие · ${usd(missingAds().reduce((sum, a) => sum + adPrice(a), 0))}`;
  const redo = $("ads-ai-redo");
  redo.hidden = ready === 0;   // перерисовывать нечего, пока нет ни одного готового баннера
  redo.textContent = `Перерисовать все · ${usd(redrawAds().reduce((sum, a) => sum + adPrice(a), 0))}`;

  const stamp = Date.now();
  if (!adDrag) {   // пока человек тащит объявление, список не перерисовываем
    const list = $("ads");
    list.replaceChildren(...lastAds.map((a, i) => {
      const [label, tone] = adStatus(a);
      const pic = a.banner_url ? el("img", { className: "pic", src: `${a.banner_url}?t=${stamp}`, alt: "" }) : el("span", { className: "pic", textContent: a.ai && a.ai.status === "working" ? "…" : "нет" });
      const grip = el("span", { className: "grip", title: "Перетащите, чтобы поменять порядок: первые идут в рекламу, остальные запасные", textContent: "⋮⋮", tabIndex: 0 });
      grip.setAttribute("role", "button");
      grip.setAttribute("aria-label", `Поменять порядок объявления «${a.title}»`);
      const item = el("button", { className: "ad-item" + (adKey(a) === selectedAd ? " sel" : ""), type: "button" }, grip, pic,
        el("span", {}, el("span", { className: "t", textContent: a.title }), el("span", { className: `st ${tone}`, textContent: label + (a.reserve ? " · запасное" : "") }),
          ...(a.ai && a.ai.ref_sent !== undefined ? [el("span", { className: "st", textContent: a.ai.ref_sent ? `Референс №${a.ai.ref_sent}` : "Без референса" })] : [])));
      item.dataset.i = String(i);
      item.addEventListener("click", () => { selectedAd = adKey(a); renderBannerStep(); });
      enableAdDrag(grip, item, list);
      return item;
    }));
    placeAdSeparator(list);
  }
  const editor = $("ad-editor");
  const typing = editor.contains(document.activeElement) && ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (fromPoll && typing) return;   // пока человек печатает, поле не перерисовываем
  const a = lastAds.find((x) => adKey(x) === selectedAd);
  editor.replaceChildren(...(a ? [adEditor(a)] : [el("p", { className: "hint", textContent: "Объявлений пока нет. Вернитесь к идеям или напишите свой вариант." })]));
}

let adDrag = false;   // идёт перетаскивание объявления в списке

// Черта между объявлениями, которые идут в рекламу, и запасными
function placeAdSeparator(list) {
  list.querySelector(".ad-sep")?.remove();
  const items = list.querySelectorAll(".ad-item");
  if (!lastCount || !items.length) return;
  const grip = el("span", { className: "grip", tabIndex: 0, textContent: "⋮⋮", title: "Потяните, чтобы взять в рекламу больше или меньше объявлений" });
  grip.setAttribute("role", "button");
  grip.setAttribute("aria-label", "Сколько объявлений идёт в рекламу");
  const live = Math.min(lastCount, items.length);
  const sep = el("div", { className: "ad-sep" }, grip, el("span", { textContent: live < items.length ? `В рекламу: ${live}. Запасные: в показ не идут` : `В рекламу идут все: ${live}` }));
  items[live - 1].after(sep);
  enableSepDrag(grip, sep, list);
}

async function saveLiveCount(count) {
  const r = await creativeCall("PUT", "/ads/live", { count });
  if (!r.ok) say(errorText(r), "err");
  await loadAds();
}

// Черту можно тянуть вверх и вниз: сколько объявлений над ней, столько идёт в рекламу (хотя бы одно)
function enableSepDrag(grip, sep, list) {
  const items = () => [...list.querySelectorAll(".ad-item")];
  grip.addEventListener("keydown", async (e) => {
    const step = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const to = Math.min(items().length, Math.max(1, lastCount + step));
    if (to !== lastCount) await saveLiveCount(to);
  });
  grip.addEventListener("pointerdown", (e) => {
    if (e.button) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    adDrag = true;
    sep.classList.add("dragging");
    const horizontal = getComputedStyle(list).flexDirection === "row";
    const marker = el("div", { className: "ad-drop" });
    let count = Math.min(lastCount, items().length);
    const move = (ev) => {
      const all = items();
      count = 0;
      for (const o of all) {
        const r = o.getBoundingClientRect();
        if ((horizontal ? ev.clientX : ev.clientY) > (horizontal ? r.left + r.width / 2 : r.top + r.height / 2)) count += 1;
      }
      count = Math.max(1, count);
      if (all[count]) all[count].before(marker); else list.append(marker);
    };
    const done = async (ev) => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", done);
      grip.removeEventListener("pointercancel", done);
      marker.remove();
      sep.classList.remove("dragging");
      adDrag = false;
      if (ev.type === "pointercancel" || count === lastCount) return;
      await saveLiveCount(count);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", done);
    grip.addEventListener("pointercancel", done);
    move(e);
  });
}

async function saveAdOrder(ads, count) {
  const r = await creativeCall("PUT", "/ads/order", { ads: ads.map((a) => ({ title: a.title, text: a.text })) });
  if (!r.ok) say(errorText(r), "err");
  else if (count !== undefined) {
    const c = await creativeCall("PUT", "/ads/live", { count });
    if (!c.ok) say(errorText(c), "err");
  }
  await loadAds();
}

// Объявление перенесли на место `at` среди остальных; live: по какую сторону черты, если место на самой черте.
// Черта остаётся между теми же объявлениями, поэтому перешедшее её объявление меняет число идущих в рекламу.
async function moveAd(from, at, live) {
  const total = lastAds.length, now = Math.min(lastCount, total);
  const rest = lastAds.filter((_, i) => i !== from);
  const liveRest = now - (from < now ? 1 : 0);
  const inLive = at < liveRest || (at === liveRest && live);
  const count = now ? Math.max(1, Math.min(total, liveRest + (inLive ? 1 : 0))) : 0;
  const next = rest.slice();
  next.splice(at, 0, lastAds[from]);
  const same = next.every((a, i) => a === lastAds[i]);
  if (same && count === now) return;
  await saveAdOrder(next, now && count !== now ? count : undefined);
}

// Перетаскивание за ручку: работает мышью и пальцем. Пока тянем, показываем линию вставки, список меняется при отпускании.
function enableAdDrag(grip, item, list) {
  grip.addEventListener("click", (e) => e.stopPropagation());
  grip.addEventListener("keydown", async (e) => {   // то же самое с клавиатуры: стрелки двигают объявление на одно место
    const step = { ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1 }[e.key];
    if (!step) return;
    e.preventDefault();
    const from = Number(item.dataset.i), to = from + step;
    if (to < 0 || to >= lastAds.length) return;
    await moveAd(from, to, false);
  });
  grip.addEventListener("pointerdown", (e) => {
    if (e.button) return;
    e.preventDefault();
    e.stopPropagation();
    grip.setPointerCapture(e.pointerId);
    adDrag = true;
    item.classList.add("dragging");
    const horizontal = getComputedStyle(list).flexDirection === "row";
    const marker = el("div", { className: "ad-drop" });
    let at = 0, live = false;   // место среди остальных объявлений и сторона черты, если место на самой черте
    const others = () => [...list.querySelectorAll(".ad-item")].filter((x) => x !== item);
    const centre = (node, pos) => { const r = node.getBoundingClientRect(); return pos < (horizontal ? r.left + r.width / 2 : r.top + r.height / 2); };
    const move = (ev) => {
      const pos = horizontal ? ev.clientX : ev.clientY, rest = others();
      const before = rest.find((o) => centre(o, pos)) || null;
      at = before ? rest.indexOf(before) : rest.length;
      const sep = list.querySelector(".ad-sep");
      const liveRest = Math.min(lastCount, lastAds.length) - (Number(item.dataset.i) < lastCount ? 1 : 0);
      live = Boolean(sep) && centre(sep, pos);
      if (sep && at === liveRest && live) sep.before(marker);   // у самой черты: сторону выбирает положение курсора
      else if (before) before.before(marker);
      else list.append(marker);
    };
    const done = async (ev) => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", done);
      grip.removeEventListener("pointercancel", done);
      marker.remove();
      item.classList.remove("dragging");
      adDrag = false;
      if (ev.type === "pointercancel") return;
      await moveAd(Number(item.dataset.i), at, live);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", done);
    grip.addEventListener("pointercancel", done);
    move(e);
  });
}

function adEditor(a) {
  const working = a.ai && a.ai.status === "working";
  const stamp = Date.now();
  const ready = a.ai && a.ai.status === "ready";
  const reload = async () => { await loadAds(); };

  // превью и версии баннера
  const preview = el("div", { className: "ed-preview" + (working ? " busy" : "") });
  const rejected = a.ai && a.ai.status === "failed" && a.ai.rejected_url;   // не принятый проверкой баннер показываем, чтобы было видно, что не так
  if (rejected) preview.append(el("img", { src: `${a.ai.rejected_url}?t=${stamp}`, alt: `${a.title}: не принят` }));
  else if (a.banner_url) preview.append(el("img", { src: `${a.banner_url}?t=${stamp}`, alt: a.title }));
  else if (a.ai && a.ai.status === "failed") preview.append(el("div", { textContent: a.ai.error || "Не получилось. Нарисуйте заново." }));
  else if (!working) preview.append(el("i", { className: "hint", textContent: "Баннера пока нет" }), el("div", { textContent: "Выберите фото и подачу справа, затем нажмите «Нарисовать баннер»." }));
  if (working) preview.append(el("div", { className: "busy-layer" }, el("div", { className: "spinner" }), el("div", { textContent: "Рисуем баннер… около минуты" })));
  const failedNote = a.ai && a.ai.status === "failed" && (a.banner_url || rejected)
    ? [el("p", { className: "notice", textContent: rejected
      ? `Сервис не принял этот баннер: ${a.ai.error || "не прошёл проверку"}. Он показан, чтобы вы видели, что не так. На объявлении остаётся прежний, если он был.`
      : `Новый баннер не вышел: ${a.ai.error || "попробуйте ещё раз"}. Пока показан прежний.` })] : [];
  const versions = lastCreatives.filter((c) => c.kind === "ai_banner" && c.title === a.title && c.body === a.text && (c.status === "draft" || c.status === "approved") && c.url)
    .sort((x, y) => y.id - x.id).slice(0, 8);
  const strip = el("div", { className: "versions" });
  if (versions.length > 1) {
    strip.append("Версии:");
    versions.forEach((v, i) => {
      const img = el("img", { src: v.url, alt: `Версия ${versions.length - i}`, title: v.current ? "Сейчас на объявлении" : "Вернуть эту версию", className: v.current ? "cur" : "" });
      if (!v.current) img.addEventListener("click", async () => {
        const r = await creativeCall("POST", `/${v.id}/restore`);
        say(r.ok ? "Версия возвращена." : errorText(r), r.ok ? "ok" : "err");
        await loadCreatives();
      });
      strip.append(img);
    });
  }

  // текст
  const titleIn = el("input", { type: "text", value: a.title, maxLength: 80 });
  const textIn = el("input", { type: "text", value: a.text, maxLength: 120 });
  const saveBtn = el("button", { className: "small secondary", textContent: "Сохранить текст", hidden: true });
  const changed = () => { saveBtn.hidden = titleIn.value === a.title && textIn.value === a.text; };
  titleIn.addEventListener("input", changed);
  textIn.addEventListener("input", changed);
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    let id = a.creative_id;
    if (!id) {   // шаблонный текст сначала сохраняем как свой
      const added = await creativeCall("POST", "/texts", { title: a.title, body: a.text });
      if (!added.ok) { say(errorText(added), "err"); saveBtn.disabled = false; return; }
      id = added.data.id;
    }
    const r = await creativeCall("PUT", `/${id}`, { title: titleIn.value, body: textIn.value, status: "approved" });
    if (!r.ok) { say(errorText(r), "err"); saveBtn.disabled = false; return; }
    selectedAd = `${r.data.title}\u0000${r.data.body}`;
    say("Текст сохранён. Баннер к новому тексту нужно нарисовать заново.");
    await loadCreatives();
  });

  // фото
  const photoBox = el("div", { className: "photo-pick" });
  const scratch = !!a.no_photo || !lastPhotos.length;   // баннер рисуется с нуля, без готового фото
  lastPhotos.forEach((p) => {
    const b = el("button", { type: "button", className: "photo-pick-item" + (p.id === a.photo_id && !a.no_photo ? " sel" : ""), title: p.label }, el("img", { src: p.url, alt: p.label }));
    b.addEventListener("click", async () => {
      if (p.id === a.photo_id) return;
      const r = await creativeCall("PUT", `/ads/${a.index}/settings`, { asset_id: p.id });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await reload();
    });
    photoBox.append(b);
  });
  if (lastPhotos.length) {   // «без фото»: сервис рисует всю сцену сам по описанию бизнеса
    const none = el("button", { type: "button", className: "photo-pick-item none" + (a.no_photo ? " sel" : ""), title: "Без фото: нарисовать с нуля" }, el("span", { textContent: "Без фото" }));
    none.addEventListener("click", async () => {
      if (a.no_photo) return;
      const r = await creativeCall("PUT", `/ads/${a.index}/settings`, { no_photo: true });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await reload();
    });
    photoBox.append(none);
  } else {
    photoBox.append(el("span", { className: "hint", textContent: "Фото пока нет: загрузите их в разделе «Фото и баннеры». Без фото баннер нарисуется с нуля." }));
  }

  // подача
  const refChips = el("div", { className: "chips" });
  const lookChips = el("div", { className: "chips" });
  const addChip = (row, label, selected, payload) => {
    const c = el("button", { type: "button", className: "chip" + (selected ? " sel" : ""), textContent: label, title: label });
    c.addEventListener("click", async () => {
      if (selected) return;
      const r = await creativeCall("PUT", `/ads/${a.index}/settings`, payload);
      if (!r.ok) { say(errorText(r), "err"); return; }
      await reload();
    });
    row.append(c);
  };
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  addChip(refChips, "Без референса", !a.ref_id, { ref_id: 0 });   // сброс: баннер рисуется без образца (автоподбор приёма его не вернёт)
  lastStyles.refs.forEach((r) => addChip(refChips, cap(r.label.length > 34 ? `${r.label.slice(0, 32)}…` : r.label), a.ref_id === r.id, { ref_id: r.id }));
  if (!scratch) lastStyles.looks.forEach((l) => addChip(lookChips, cap(l.label), !a.ref_id && a.look_no === l.no, { look: l.no }));
  const chips = el("div", {}, el("span", { className: "sub", textContent: scratch ? "По мотивам (идея из рекламы вашей ниши, без фото)" : "По мотивам (вдохновение из рекламы вашей ниши)" }), refChips,
    ...(scratch ? [] : [el("span", { className: "sub", textContent: "Обычное оформление" }), lookChips]));

  // действия
  const price = adPrice(a);
  const draw = el("button", { textContent: `${ready ? "Нарисовать заново" : "Нарисовать баннер"} · ${usd(price)}` });
  draw.disabled = working;
  draw.addEventListener("click", async () => {
    const r = await creativeCall("POST", `/ads/${a.index}/ai-banner`);
    if (!r.ok) { say(errorText(r), "err"); return; }
    if (r.data.varied) say(`Пробуем другую подачу: ${r.data.varied}.`);   // повтор без правок берёт следующую подачу
    await loadCreatives();
  });
  const again = el("button", { className: "secondary", textContent: "Другой текст" });
  again.addEventListener("click", async () => {
    again.disabled = true; again.textContent = "Пишем…";
    const r = await creativeCall("POST", `/ads/${a.index}/rewrite`);
    if (!r.ok) { say(errorText(r), "err"); again.disabled = false; again.textContent = "Другой текст"; return; }
    selectedAd = `${r.data.title}\u0000${r.data.text}`;
    say(`Новый текст: «${r.data.title}». Баннер к нему нужно нарисовать заново.`);
    await loadCreatives();
  });
  const actions = el("div", { className: "ed-actions" }, draw, again);
  if (a.creative_id && a.own) {
    const drop = el("button", { className: "link", textContent: "Убрать из рекламы" });
    drop.addEventListener("click", async () => {
      const r = await creativeCall("PUT", `/${a.creative_id}`, { status: "rejected" });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await loadCreatives();
    });
    actions.append(drop);
  }
  actions.append(el("span", { className: "hint", textContent: "Текст на баннере проверяется автоматически" }));

  const top = el("div", { className: "ed-top" }, el("h2", { textContent: "Объявление" }),
    ...(a.ai && a.ai.hypothesis ? [el("span", { className: "tag", textContent: `Проверяем: ${a.ai.hypothesis}` })] : []),
    ...(a.ai && a.ai.inspiration ? [el("span", { className: "hint", textContent: `Вдохновение: ${a.ai.inspiration}` })] : []),
    ...(a.ai && a.ai.ref_sent !== undefined ? [el("span", { className: "hint", textContent: a.ai.ref_sent ? `В последний заказ ушёл референс №${a.ai.ref_sent}: ${a.ai.ref_note}` : "В последний заказ референс не ушёл: баннер нарисован без него" })] : []));
  const fields = el("div", { className: "ed-fields" },
    el("div", {}, el("span", { className: "lab", textContent: "Заголовок на баннере" }), titleIn, counterEl(titleIn, creativeLimits.title)),
    el("div", {}, el("span", { className: "lab", textContent: "Текст объявления" }), textIn, counterEl(textIn, creativeLimits.text), saveBtn),
    el("div", {}, el("span", { className: "lab", textContent: "Фото" }), photoBox),
    ...(a.no_photo ? [el("p", { className: "hint ed-note", textContent: "Баннер нарисуется с нуля по описанию бизнеса: сцену придумает арт-директор, ваши фото в нём не используются." })] : []),
    el("div", {}, el("span", { className: "lab", textContent: "Подача" }), chips,
      el("p", { className: "hint ed-note", textContent: "Подача сохраняется сразу, но баннер меняется после «Нарисовать». Если подачу не менять, «Нарисовать заново» сам берёт следующую подачу из ниши, чтобы вариант отличался." })));
  const box = el("div", {}, top, el("div", { className: "ed-grid" }, el("div", {}, preview, strip, ...failedNote), fields), actions);
  if (a.ai && a.ai.prompt) {   // промпт для тех, кто хочет править сам
    const area = el("textarea", { rows: 7, maxLength: 4000, value: a.ai.prompt });
    const go = el("button", { className: "small", textContent: "Нарисовать по этому промпту" });
    go.disabled = working;
    go.addEventListener("click", async () => {
      const r = await creativeCall("POST", `/ads/${a.index}/ai-banner`, { prompt: area.value });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await loadCreatives();
    });
    box.append(el("details", { className: "own-text" }, el("summary", { textContent: "Промпт для картинки (для тех, кто хочет править)" }),
      el("p", { className: "hint", textContent: "Так сервис просит нейросеть сделать баннер из вашего фото. Поправьте текст (оформление, цвета, что убрать) и нажмите кнопку. Заголовок и кнопку оставьте в кавычках, их потом проверит сервис." }),
      area, el("p"), go));
  }
  return box;
}

function renderLaunchStep() {
  const live = lastAds.filter((a) => !a.reserve), spare = lastAds.filter((a) => a.reserve);
  const missing = live.filter((a) => !(a.ai && a.ai.status === "ready")).length;
  $("launch-warn").hidden = missing === 0;
  $("launch-warn").textContent = `У объявлений без ИИ-баннера (${missing}) сервис соберёт обычную шаблонную картинку. Вернитесь к баннерам, если хотите нарисовать им баннеры.`;
  const row = (a, n) => {
    const [label, tone] = adStatus(a);
    const pic = a.banner_url ? el("img", { className: "pic", src: `${a.banner_url}?t=${Date.now()}`, alt: "" }) : el("span", { className: "pic" });
    const status = a.reserve ? el("span", { className: "st spare", textContent: "Запасное" }) : el("span", { className: `st ${tone}`, textContent: label });
    const edit = el("button", { className: "small secondary", type: "button", textContent: "Изменить" });
    edit.addEventListener("click", () => { selectedAd = adKey(a); renderBannerStep(); setStep("banners"); });
    return el("div", { className: "launch-row" + (a.reserve ? " spare" : " live") }, el("span", { className: "num", textContent: a.reserve ? "·" : String(n) }), pic,
      el("div", {}, el("div", { className: "t", textContent: a.title }), el("div", { className: "hint", textContent: a.text })), status, edit);
  };
  const parts = [];
  if (live.length) parts.push(el("div", { className: "launch-head live", textContent: `Пойдут в рекламу: ${live.length}` }), ...live.map((a, i) => row(a, i + 1)));
  if (spare.length) {
    parts.push(el("div", { className: "launch-head", textContent: `Запасные: ${spare.length}` }),
      el("div", { className: "hint", textContent: "В показ не идут. Нужны, чтобы заменить слабое объявление, когда сервис его остановит." }), ...spare.map((a) => row(a, 0)));
  }
  $("launch-list").replaceChildren(...parts);
  $("ads-launch").textContent = live.length ? `Создать рекламу из ${live.length} ${plural(live.length, "объявления", "объявлений", "объявлений")}` : "Создать рекламу из этих объявлений";
}

function updateIdeasCount() {
  const used = ideasState.filter((b) => b.status === "used").length;
  $("ideas-count").textContent = ideasState.length
    ? `Взято ${used} из ${ideasState.length}. Бюджета хватает на ${lastCount || 0} объявлений.`
    : "";
}

const SOURCE_LABEL = { claude: "ИИ", template: "шаблон", user: "ваш" };
const STATUS_LABEL = { approved: "Одобрено", rejected: "Не используем", draft: "Ждёт решения" };

function renderTexts(items) {
  const list = $("cr-texts");
  list.replaceChildren();
  if (!items.length) list.append(el("li", { className: "hint", textContent: "Вариантов пока нет. Нажмите «Придумать тексты»." }));
  for (const c of items) {
    const title = el("input", { type: "text", value: c.title, maxLength: 80 });
    const body = el("input", { type: "text", value: c.body, maxLength: 120 });
    const meta = el("div", { className: "meta" }, counterEl(title, creativeLimits.title), counterEl(body, creativeLimits.text),
      el("span", { className: "pill", textContent: `${STATUS_LABEL[c.status] || c.status}, ${SOURCE_LABEL[c.source] || c.source}` }),
      ...(c.angle_label ? [el("span", { className: "pill", textContent: `Угол: ${c.angle_label}` })] : []));
    const save = async (status) => {
      const r = await creativeCall("PUT", `/${c.id}`, { title: title.value, body: body.value, status });
      if (!r.ok) { setStatus("cr-texts-status", errorText(r), "err"); return; }
      setStatus("cr-texts-status", "");
      await loadCreatives();
    };
    const approve = el("button", { className: "small", textContent: c.status === "approved" ? "Сохранить" : "Использовать" });
    approve.addEventListener("click", () => save("approved"));
    const skip = el("button", { className: "small secondary", textContent: "Не использовать" });
    skip.addEventListener("click", () => save("rejected"));
    const del = el("button", { className: "small secondary", textContent: "Удалить" });
    del.addEventListener("click", async () => { await creativeCall("DELETE", `/${c.id}`); await loadCreatives(); });
    list.append(el("li", { className: c.status }, title, body, meta, el("div", { className: "btns" }, approve, skip, del)));
  }
}

// Стратег: несколько разных идей для A/B-теста; взятая в рекламу идея становится объявлением со своими фото и подачей.
let ideasState = [];

async function loadBriefs() {
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/briefs`);
  if (!r.ok) return;
  ideasState = r.data.briefs;
  $("briefs-empty").hidden = ideasState.length > 0;
  $("briefs").replaceChildren(...ideasState.map((b) => {
    const used = b.status === "used";
    const tags = [b.angle_label && `Угол: ${b.angle_label}`, b.inspiration ? `Вдохновение: ${b.inspiration}` : `Оформление: ${b.look}`].filter(Boolean);
    const body = el("div", { className: "brief-body" },
      el("div", { className: "hint", textContent: b.audience ? `Для кого: ${b.audience}` : "Идея для проверки" }),
      el("div", { className: "brief-title", textContent: b.title }),
      el("div", { textContent: b.text }),
      ...(b.hypothesis ? [el("div", { className: "hint", textContent: `Проверяем: ${b.hypothesis}` })] : []),
      ...tags.map((t) => el("div", { className: "hint", textContent: t })));
    const toggle = el("button", { className: "small" + (used ? "" : " secondary"), textContent: used ? "Берём в рекламу ✓" : "Взять в рекламу" });
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      const res = await api("POST", `/api/businesses/${currentBusiness.id}/briefs/${b.id}/${used ? "unuse" : "use"}`);
      if (!res.ok) { say(errorText(res), "err"); toggle.disabled = false; return; }
      if (!used && res.data.ad_index === null) say("Идея выбрана, но в бюджет сейчас помещается меньше объявлений.");
      await loadCreatives();
    });
    const drop = el("button", { className: "link", textContent: "Убрать" });
    drop.addEventListener("click", async () => { await api("DELETE", `/api/businesses/${currentBusiness.id}/briefs/${b.id}`); await loadCreatives(); });
    const acts = el("div", { className: "brief-acts" }, toggle, drop);
    return el("div", { className: "brief" + (used ? " used" : "") }, b.photo_url ? el("img", { src: b.photo_url, alt: b.title }) : "", body, acts);
  }));
  updateIdeasCount();
}

async function generateBriefs() {
  const button = $("briefs-gen");
  button.disabled = true;
  setStatus("briefs-status", "Стратег думает над идеями… около полминуты");
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/briefs/generate`, { count: Number($("briefs-count").value) });
  button.disabled = false;
  setStatus("briefs-status", r.ok ? `Готово, идей: ${r.data.briefs.length}. Выберите подходящие.` : errorText(r), r.ok ? "ok" : "err");
  await loadBriefs();
  await loadCreatives();
}

// История баннеров: все, что рисовала нейросеть. Готовый можно вернуть на карточку без новой оплаты.
function renderBannerHistory(banners) {
  const box = $("banner-history-box");
  box.hidden = banners.length === 0;
  if (!banners.length) return;
  const ready = banners.filter((c) => c.status === "draft" || c.status === "approved").length;
  $("banner-history-title").textContent = `История баннеров (${banners.length}, готовых ${ready})`;
  const grid = $("banner-history");
  grid.replaceChildren();
  for (const c of banners.slice().sort((a, b) => b.id - a.id)) {
    const tile = el("div", { className: `thumb ${c.status}` });
    if (c.status === "working") tile.append(el("div", { className: "wait" }, el("div", { className: "spinner" }), el("div", { textContent: "Рисуем… около минуты" })));
    else if (c.status === "failed") {
      const why = c.error || "Не получилось.";
      tile.append(el("div", { className: "wait", title: why, textContent: why.length > 150 ? `${why.slice(0, 147)}…` : why }));
    } else tile.append(el("img", { src: c.url, alt: c.title }));
    const m = c.meta || {};
    const current = !!c.current;
    const when = (c.created_at || "").slice(0, 16).replace("T", " ");
    const detail = [m.scene, m.audience && `для: ${m.audience}`, m.logo ? "с логотипом" : ""].filter(Boolean).join(", ");
    tile.append(el("div", { className: "cap" }, el("b", { textContent: c.title }), el("span", { textContent: [detail, when].filter(Boolean).join(" · ") })));
    const acts = el("div", { className: "acts" });
    if (current) acts.append(el("span", { className: "badge good", textContent: "Сейчас на карточке" }));
    else if (c.status === "draft" || c.status === "approved") {
      const back = el("button", { className: "secondary", textContent: "Вернуть этот" });
      back.addEventListener("click", async () => {
        back.disabled = true;
        const r = await creativeCall("POST", `/${c.id}/restore`);
        say(r.ok ? "Баннер возвращён на карточку." : errorText(r), r.ok ? "ok" : "err");
        await loadCreatives();
      });
      acts.append(back);
    }
    const del = el("button", { className: "secondary", textContent: "Удалить" });
    del.addEventListener("click", async () => { await creativeCall("DELETE", `/${c.id}`); await loadCreatives(); });
    acts.append(del);
    tile.append(acts);
    grid.append(tile);
  }
}

function renderMedia(grid, items, canMakeVideo) {
  grid.replaceChildren();
  for (const c of items) {
    const tile = el("div", { className: `thumb ${c.status}` });
    if (c.status === "working") tile.append(el("div", { className: "wait" }, el("div", { className: "spinner" }),
      el("div", { textContent: c.kind === "video" ? "Готовим видео… это займёт несколько минут" : "Рисуем… около минуты" })));
    else if (c.status === "failed") tile.append(el("div", { className: "wait", textContent: c.error || "Не получилось. Попробуйте ещё раз." }));
    else tile.append(c.kind === "video" ? el("video", { src: c.url, controls: true, preload: "metadata" }) : el("img", { src: c.url, alt: "Картинка", loading: "lazy" }));
    const acts = el("div", { className: "acts" });
    if (c.status === "draft" || c.status === "approved") {
      const like = el("button", { className: c.status === "approved" ? "" : "secondary", textContent: c.status === "approved" ? "Выбрана" : "Выбрать" });
      like.addEventListener("click", async () => { await creativeCall("PUT", `/${c.id}`, { status: c.status === "approved" ? "draft" : "approved" }); await loadCreatives(); });
      acts.append(like);
      if (canMakeVideo && c.kind === "image") {
        const video = el("button", { className: "secondary", textContent: "Сделать видео" });
        video.addEventListener("click", async () => {
          if (!(await askConfirm("Сделать видео?", `Это платно: около ${usd(creativePrices.video)} с баланса генерации. Готовится 5–7 минут.`, "Сделать"))) return;
          const r = await creativeCall("POST", "/videos/generate", { image_creative_id: c.id });
          setStatus("cr-images-status", r.ok ? "Видео готовится, оно появится ниже." : errorText(r), r.ok ? "ok" : "err");
          await loadCreatives();
        });
        acts.append(video);
      }
    }
    const del = el("button", { className: "secondary", textContent: "Удалить" });
    del.addEventListener("click", async () => { await creativeCall("DELETE", `/${c.id}`); await loadCreatives(); });
    acts.append(del);
    tile.append(acts);
    grid.append(tile);
  }
}

async function generateTexts(auto = false) {
  const button = $("cr-gen-texts");
  button.disabled = true;
  setStatus("cr-texts-status", auto === true ? "Пишем первые варианты…" : "Придумываем…");
  const r = await creativeCall("POST", "/texts/generate", { count: 6, hint: $("cr-hint").value });
  button.disabled = false;
  setStatus("cr-texts-status", r.ok ? r.data.message : errorText(r), r.ok ? "ok" : "err");
  await loadCreatives();
}

async function generateImages() {
  const button = $("cr-gen-images");
  const count = Number($("cr-image-count").value);
  if (!(await askConfirm("Нарисовать картинки?", `Это платно: около ${usd(count * creativePrices.image)} за ${count} шт.`, "Нарисовать"))) return;
  button.disabled = true;
  const r = await creativeCall("POST", "/images/generate", { count, idea: $("cr-idea").value });
  button.disabled = false;
  setStatus("cr-images-status", r.ok ? "Рисуем. Картинки появятся ниже сами." : errorText(r), r.ok ? "ok" : "err");
  await loadCreatives();
}

async function addOwnText() {
  const r = await creativeCall("POST", "/texts", { title: $("cr-own-title").value, body: $("cr-own-body").value });
  if (!r.ok) { setStatus("cr-texts-status", errorText(r), "err"); return; }
  $("cr-own-title").value = "";
  $("cr-own-body").value = "";
  setStatus("cr-texts-status", "Вариант добавлен. Нажмите «Использовать», чтобы он попал в рекламу.", "ok");
  await loadCreatives();
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
  $("m-auto").hidden = Boolean(selected.counter && selected.goal);   // счётчик уже выбран: создавать второй не нужно
  const counter = $("m-counter");
  // пока ничего не сохранено, первый счётчик из списка не должен выглядеть выбранным: иначе кажется, что Метрика уже подключена
  counter.replaceChildren(choosePlaceholder(), ...counters.map((c) => el("option", { value: c.id, textContent: c.site ? `${c.name} (${c.site})` : c.name })));
  counter.value = selected.counter || "";
  $("m-clear").hidden = !selected.counter;
  const connected = Boolean(selected.counter && selected.goal);
  setStatus("metrika-status", connected ? "Подключено: сервис видит обращения по выбранной цели." : "Метрика не подключена. Выберите счётчик и цель, потом нажмите «Сохранить».", connected ? "ok" : "");
  await loadGoals(selected.goal);
  await loadSnippet();
  await loadHealth(false);
}

const HEALTH_CLASS = { ok: " good", warn: "", bad: " bad", unknown: " proposals" };

async function loadHealth(refresh) {
  const box = $("m-health-box");
  if (!$("m-clear") || $("m-clear").hidden) { box.hidden = true; return; }   // счётчик ещё не сохранён: проверять нечего
  const button = $("m-health-check");
  button.disabled = true;
  if (refresh) setStatus("metrika-status", "Спрашиваем Метрику…", "");
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/metrika/health${refresh ? "?refresh=true" : ""}`);
  button.disabled = false;
  if (refresh) setStatus("metrika-status", "", "");
  if (!r.ok) { if (refresh) setStatus("metrika-status", errorText(r), "err"); return; }
  const h = r.data.health;
  box.hidden = false;
  $("m-health").className = "notice" + (h ? HEALTH_CLASS[h.level] || "" : " proposals");
  $("m-health").textContent = h ? `${h.title}. ${h.message}` : "Счётчик ещё не проверяли. Нажмите «Проверить счётчик»: сервис спросит Метрику, стоит ли код на сайте и приходят ли визиты.";
}

async function loadSnippet() {
  const box = $("m-snippet");
  const counterId = $("m-counter").value;
  box.hidden = !counterId;
  if (!counterId) return;
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/metrika/snippet?counter_id=${encodeURIComponent(counterId)}`);
  if (!r.ok) { box.hidden = true; return; }
  $("m-snippet-code").value = r.data.code;
  $("m-snippet-steps").replaceChildren(...r.data.steps.map((t) => el("li", { textContent: t })));
  $("m-snippet-goals").replaceChildren(...r.data.goals.map((g) => el("li", { textContent: `${g.identifier}: ${g.title}` })));
}

async function copySnippet() {
  const field = $("m-snippet-code");
  try {
    await navigator.clipboard.writeText(field.value);
    setStatus("metrika-status", "Код скопирован.", "ok");
  } catch {
    field.select();
    setStatus("metrika-status", "Код выделен: нажмите Ctrl+C.", "");
  }
}

async function createMetrikaAuto() {
  const button = $("m-auto");
  button.disabled = true;
  setStatus("metrika-status", "Создаём счётчик и цели в вашей Метрике…", "");
  const made = await api("POST", `/api/businesses/${currentBusiness.id}/metrika/auto`);
  if (!made.ok) { button.disabled = false; setStatus("metrika-status", errorText(made), "err"); return; }
  const d = made.data;
  let saved = false;
  if (d.main_goal) {   // сразу сохраняем выбор: счётчик и первая подходящая цель
    saved = (await api("PUT", `/api/businesses/${currentBusiness.id}/metrika`, { counter_id: d.counter_id, goal_id: d.main_goal.id })).ok;
  }
  button.disabled = false;
  $("m-auto-next").hidden = false;
  $("m-auto-next").textContent = `${d.next}${d.skipped.length ? ` Метрика не приняла цели: ${d.skipped.join(", ")}.` : ""}${saved ? ` Обращением выбрана цель «${d.main_goal.name}».` : " Выберите счётчик и цель ниже и нажмите «Сохранить»."}`;
  setStatus("metrika-status", `Счётчик ${d.counter_id} создан.`, "ok");
  await loadMetrika();
}

const choosePlaceholder = () => el("option", { value: "", textContent: "— выберите —" });

async function loadGoals(selectedGoal) {
  const goal = $("m-goal");
  if (!$("m-counter").value) { goal.replaceChildren(choosePlaceholder()); return; }
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/metrika/counters/${$("m-counter").value}/goals`);
  if (!result.ok) { setStatus("metrika-status", errorText(result), "err"); return; }
  const goals = result.data.goals;
  goal.replaceChildren(choosePlaceholder(), ...goals.map((g) => el("option", { value: g.id, textContent: g.name })));
  goal.value = selectedGoal || "";
  if (!goals.length) setStatus("metrika-status", "В этом счётчике нет целей. Создайте цель «Обращение» в Метрике.", "");
}

async function saveMetrika() {
  if (!$("m-counter").value || !$("m-goal").value) { setStatus("metrika-status", "Выберите счётчик и цель из списка.", "err"); return; }
  const result = await api("PUT", `/api/businesses/${currentBusiness.id}/metrika`, { counter_id: $("m-counter").value, goal_id: $("m-goal").value });
  if (!result.ok) { setStatus("metrika-status", errorText(result), "err"); return; }
  $("m-clear").hidden = false;
  await loadHealth(true);   // сразу проверяем, что счётчик на сайте работает: сохранить можно и тот, код которого ещё не поставили
  setStatus("metrika-status", "Готово: счётчик сохранён. Работает ли он на сайте, написано ниже.", "ok");
}

async function clearMetrika() {
  await api("DELETE", `/api/businesses/${currentBusiness.id}/metrika`);
  setStatus("metrika-status", "Метрика отключена. Сервис перестанет оценивать рекламу, пока вы её не подключите снова.", "ok");
  $("m-clear").hidden = true;
  $("m-health-box").hidden = true;
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
    if (logo.ai_score) logoBox.append(el("div", { className: "hint", textContent: `ИИ: оценка ${logo.ai_score}/5. ${logo.ai_note}${logo.ai_score <= 2 ? " Такой логотип на баннеры не ставим." : ""}` }));
  } else {
    logoBox.append(el("span", { className: "hint", textContent: "Логотип пока не загружен." }));
  }

  fillGrid($("screens"), assets.filter((a) => a.kind === "photo" && a.tag === "screen"), (a) =>
    el("img", { src: a.url, alt: "Скриншот", loading: "lazy" }));
  fillGrid($("photos"), assets.filter((a) => a.kind === "photo" && a.tag !== "screen"), (a) =>
    el("img", { src: a.url, alt: a.caption || a.tag_label, loading: "lazy" }));
  fillGrid($("videos"), assets.filter((a) => a.kind === "video"), (a) =>
    el("video", { src: a.url, controls: true, preload: "metadata" }));
}

function fillGrid(grid, items, makeMedia) {
  grid.replaceChildren();
  for (const a of items) {
    const remove = el("button", { type: "button", textContent: "Удалить", title: "Удалить файл" });
    remove.addEventListener("click", () => removeAsset(a.id));
    const ai = a.ai_score ? `${a.ai_label || "Кадр"}, оценка ${a.ai_score}/5${a.ai_note ? ". " + a.ai_note : ""}` : "";
    grid.append(el("div", { className: "thumb" }, makeMedia(a), remove, el("div", { className: "cap", textContent: a.tag_label || a.name }),
      ai ? el("div", { className: "cap ai-note" + (a.ai_score <= 2 ? " bad" : ""), textContent: ai }) : ""));
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
    if (kind === "logo" && $("logo-remove-bg").checked) form.append("remove_bg", "true");
    form.append("file", file);
    const result = await api("POST", `/api/businesses/${currentBusiness.id}/assets`, form);
    if (!result.ok) {
      setStatus("files-status", `${file.name}: ${errorText(result)}`, "err");
      break;
    }
    done += 1;
    if (kind === "logo" && result.data && result.data.bg_removed && result.data.bg_removed !== "none") toast("Фон логотипа убран.");
  }
  await refreshAssets();
  if (kind !== "video") api("POST", `/api/businesses/${currentBusiness.id}/creatives/photos/check`).then(refreshAssets);  // ИИ сам проверит загруженное (около 1 цента за файл)
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

function nextStep(steps) {
  return steps.find((s) => !s.done && !s.optional) || steps.find((s) => !s.done);
}

function renderChecklist(steps) {
  const box = $("checklist");
  box.replaceChildren();
  const next = nextStep(steps);
  $("checklist-card").hidden = !next;
  if (!next) return;
  for (const s of steps) {
    const state = s.done ? "done" : s === next ? "next" : s.optional ? "optional" : "";
    // пока шаг не сделан, название с частицей «не» («Объявления не созданы»), после выполнения без неё
    const body = el("div", {}, el("div", { className: "title", textContent: (s.done ? s.title : s.title_todo || s.title) + (s.optional && !s.done ? " (желательно)" : "") }));
    if (!s.done) {
      const hint = el("div", { className: "hint", textContent: s.hint + " " });
      if (s.page && s.page !== currentPage) {
        const go = el("button", { className: "link", textContent: "Перейти" });
        go.addEventListener("click", () => showPage(s.page));
        hint.append(go);
      }
      body.append(hint);
    }
    box.append(el("div", { className: `step ${state}` }, el("div", { className: "mark", textContent: s.done ? "✓" : "" }), body));
  }
}

let homeAction = null; // что делает большая кнопка на главной

function renderHome(data) {
  const steps = data.checklist || [];
  const required = steps.filter((s) => !s.optional);
  const doneCount = required.filter((s) => s.done).length;
  const next = nextStep(steps);
  const noAds = !data.real && !data.example;
  const eyebrow = $("home-eyebrow"), title = $("home-title"), text = $("home-text"), cta = $("home-cta");
  $("sim-start").hidden = !noAds;
  $("home-progress").hidden = !(noAds && next);
  $("home-progress-fill").style.width = `${Math.round((doneCount / required.length) * 100)}%`;
  $("home-progress-text").textContent = `Готово шагов: ${doneCount} из ${required.length}`;
  cta.hidden = false;
  if (data.stopped) {
    eyebrow.textContent = "Пауза";
    title.textContent = "Реклама остановлена";
    text.textContent = "Показов нет, деньги не тратятся. Когда будете готовы, включите рекламу обратно.";
    cta.textContent = "Включить обратно";
    homeAction = resumeAll;
  } else if (data.real || data.example) {
    const t = data.totals;
    eyebrow.textContent = data.example ? "Пример" : "Реклама работает";
    title.textContent = t && t.leads ? `${t.leads} ${plural(t.leads, "обращение", "обращения", "обращений")} за ${t.days} дней` : "Реклама запущена";
    text.textContent = data.proposals
      ? `Есть предложения, которые ждут вашего решения: ${data.proposals}.`
      : data.real ? "Сервис следит за результатом и сам сообщит, если нужно ваше решение." : "Посмотрите, как сервис принимает решения, в разделе «Демонстрация» на странице «Результаты».";
    if (data.real) { cta.textContent = "Обновить данные"; homeAction = syncNow; } else { cta.hidden = true; homeAction = null; }
  } else if (next && next.key !== "ads") {
    eyebrow.textContent = "Следующий шаг";
    title.textContent = STEP_ACTION[next.key] || next.title;
    text.textContent = next.hint;
    cta.textContent = STEP_BUTTON[next.key] || "Перейти";
    homeAction = () => showPage(next.page || "home");
  } else {
    eyebrow.textContent = "Всё готово";
    title.textContent = "Можно запускать рекламу";
    text.textContent = "Сервис покажет план: какие объявления будут созданы и сколько денег на них уйдёт. Ничего не запустится без вашего подтверждения.";
    cta.textContent = "Создать рекламу";
    homeAction = openLaunch;
  }
}

// Плитка с фото на главной: самый удачный кадр из загруженных (по оценке ИИ), а если оценок нет, первый.
function renderPhotoTile() {
  const photos = ((currentBusiness && currentBusiness.assets) || []).filter((a) => a.kind === "photo" && a.tag !== "screen");
  const tileEl = $("ptile-photo");
  tileEl.hidden = !photos.length;
  if (!photos.length) return;
  const best = photos.reduce((a, b) => ((b.ai_score || 0) > (a.ai_score || 0) ? b : a));
  $("ptile-img").src = best.url;
  $("ptile-title").textContent = best.ai_label || "Ваше фото";
  $("ptile-cap").textContent = `Всего фото: ${photos.length}`;
}

const STEP_ACTION = {
  described: "Расскажите о своём бизнесе",
  money: "Задайте бюджет",
  cabinet: "Подключите рекламный кабинет",
  marking: "Внесите данные рекламодателя",
  metrika: "Подключите учёт обращений",
};
const STEP_BUTTON = {
  described: "Описать бизнес", money: "Указать бюджет", cabinet: "Подключить кабинет", marking: "Понятно, сделаю", metrika: "Подключить",
};

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const adsPhrase = (n) => `${n} ${plural(n, "объявление", "объявления", "объявлений")}`;   // «3 объявления»

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function renderChart(days) {
  const card = $("chart-card");
  const total = days.reduce((s, d) => s + d.spend + d.leads, 0);
  card.hidden = !days.length || !total;
  const svg = $("chart");
  svg.replaceChildren();
  if (card.hidden) return;
  const W = 640, H = 260, L = 12, R = 12, T = 12, B = 28;
  const maxSpend = Math.max(...days.map((d) => d.spend), 1);
  const maxLeads = Math.max(...days.map((d) => d.leads), 1);
  const slot = (W - L - R) / days.length;
  for (let i = 0; i <= 3; i++) {
    const y = T + ((H - T - B) * i) / 3;
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid-line" }));
    const label = svgEl("text", { x: L - 6, y: y + 4, "text-anchor": "end", class: "axis" });
    label.textContent = Math.round(maxSpend * (1 - i / 3)).toLocaleString("ru-RU");
    svg.append(label);
  }
  const points = [];
  days.forEach((d, i) => {
    const h = ((H - T - B) * d.spend) / maxSpend;
    const x = L + i * slot + slot * 0.18;
    svg.append(svgEl("rect", { x, y: H - B - h, width: slot * 0.64, height: Math.max(h, 0), rx: 3, class: "bar-spend" }));
    points.push([L + i * slot + slot / 2, H - B - ((H - T - B) * d.leads) / maxLeads]);
    if (i % Math.ceil(days.length / 7) === 0) {
      const t = svgEl("text", { x: L + i * slot + slot / 2, y: H - 8, "text-anchor": "middle", class: "axis" });
      t.textContent = d.day.slice(8) + "." + d.day.slice(5, 7);
      svg.append(t);
    }
  });
  if (days.some((d) => d.leads)) {
    svg.append(svgEl("polyline", { points: points.map((p) => p.join(",")).join(" "), class: "line-leads" }));
    for (const [x, y] of points) svg.append(svgEl("circle", { cx: x, cy: y, r: 3.5, class: "dot-leads" }));
  }
}

async function checkPhotos() {
  const btn = $("photos-check");
  btn.disabled = true;
  $("photos-check-status").textContent = "ИИ смотрит файлы…";
  const r = await creativeCall("POST", "/photos/check");
  btn.disabled = false;
  $("photos-check-status").textContent = "";
  if (!r.ok) { say(errorText(r), "err"); return; }
  say(r.data.checked ? `Проверено файлов: ${r.data.checked}.` : "Новых файлов для проверки нет.");
  await refreshAssets();
}

async function analyzeResults() {
  const btn = $("insights-ai");
  btn.disabled = true;
  btn.textContent = "ИИ разбирает…";
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/insights/analyze`);
  btn.disabled = false;
  btn.textContent = "Разобрать результаты ИИ";
  if (!r.ok) { say(errorText(r), "err"); return; }
  const box = $("insights-ai-text");
  box.hidden = false;
  box.textContent = r.data.text;
}

async function loadInsights() {
  const card = $("insights-card");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/insights`);
  const groups = result.ok ? result.data.groups : [];
  card.hidden = !groups.length;
  if (!groups.length) return;
  $("insights-notes").replaceChildren(...result.data.conclusions.map((t) => el("li", { textContent: t })));
  const row = (g) => el("tr", {},
    ...[g.label, String(g.units), String(g.impressions), String(g.clicks), g.ctr == null ? "—" : `${g.ctr}%`, String(g.leads), money(g.cost_per_lead)]
      .map((v) => el("td", { textContent: v })));
  $("insights-table").querySelector("tbody").replaceChildren(...groups.map(row));
  $("insights-types").querySelector("tbody").replaceChildren(...(result.data.type_groups || []).map(row));
  $("insights-angles").querySelector("tbody").replaceChildren(...(result.data.angle_groups || []).map(row));
}

async function loadVisitorGoals() {
  const card = $("goals-card");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/goals`);
  const goals = result.ok ? result.data.goals : [];
  card.hidden = !goals.length;
  if (!goals.length) return;
  const d = result.data;
  $("goals-hint").textContent = `За последние ${d.days} дн.: ${d.clicks} ${plural(d.clicks, "клик", "клика", "кликов")}. «Обращение» отмечено, по нему сервис принимает решения; остальные цели показывают путь посетителя до него.`;
  const row = (g) => el("tr", {},
    ...[g.main ? `${g.name} (обращение)` : g.name, String(g.count), g.per_100_clicks == null ? "—" : String(g.per_100_clicks), money(g.cost)]
      .map((v) => el("td", { textContent: v })));
  $("goals-table").querySelector("tbody").replaceChildren(...goals.map(row));
}

/* ---------- Итоги недели ---------- */

async function loadReport() {
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/report`);
  const report = result.ok ? result.data.report : null;
  $("report-card").hidden = !report;
  if (report) $("report-text").textContent = `${report.day_text}\n\n———\n\n${report.text}`;
}

/* ---------- Заявки с сайта ---------- */

function renderLeads(d) {
  $("leads-card").hidden = false;
  $("leads-snippet").value = d.snippet;
  const n = d.leads.length;
  $("leads-hint").textContent = n
    ? `Последние заявки с формы вашего сайта.${d.telegram ? "" : " Telegram не подключён: заявки видны только здесь. Подключите его на странице «Подключения»."}`
    : `Заявок пока нет. Когда посетитель отправит форму на сайте, она появится здесь${d.telegram ? " и придёт вам в Telegram" : ""}. Чтобы форма заработала, вставьте код ниже в страницу.`;
  const when = (s) => new Date(`${s.replace(" ", "T")}Z`).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  $("leads-table").hidden = !n;
  $("leads-table").querySelector("tbody").replaceChildren(...d.leads.map((l) => el("tr", {},
    ...[when(l.created_at), l.name || "—", l.contact, l.task || "—", l.delivered ? "в Telegram" : "только здесь"].map((v) => el("td", { textContent: v })))));
}

async function loadLeads() {
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/leads`);
  if (r.ok) renderLeads(r.data); else $("leads-card").hidden = true;
}

async function newLeadsKey() {
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/leads/key`);
  if (r.ok) { renderLeads(r.data); say("Сделан новый адрес приёма. Обновите код на сайте: прежний адрес больше не работает.", ""); }
}

async function copyLeadsSnippet() {
  const field = $("leads-snippet");
  try { await navigator.clipboard.writeText(field.value); say("Код скопирован.", ""); } catch { field.select(); }
}

/* ---------- Telegram ---------- */

function renderTelegram(s) {
  $("tg-unlinked").hidden = !s.configured || s.linked;
  $("tg-linked").hidden = !s.linked;
  $("tg-note").hidden = s.configured;
  $("tg-note").textContent = s.configured ? "" : "Telegram на сервере пока не настроен: администратору нужно создать бота и записать его токен (см. docs/deployment.md).";
  $("tg-notify").checked = s.notify;
  $("tg-steps").hidden = !s.code;
  $("tg-connect").textContent = s.code ? "Получить новый код" : "Подключить Telegram";
  if (s.code) { $("tg-code").textContent = s.code; $("tg-link").href = s.link; }
}

async function loadTelegram() {
  const r = await api("GET", "/api/telegram");
  if (r.ok) renderTelegram(r.data);
}

async function connectTelegram() {
  const r = await api("POST", "/api/telegram/connect");
  if (!r.ok) { setStatus("tg-status", errorText(r), "err"); return; }
  setStatus("tg-status", "", "");
  renderTelegram(r.data);
}

async function checkTelegram() {
  setStatus("tg-status", "Ищем ваше сообщение боту…", "");
  const r = await api("POST", "/api/telegram/check");
  if (!r.ok) { setStatus("tg-status", errorText(r), "err"); return; }
  renderTelegram(r.data);
  setStatus("tg-status", r.data.found ? `Готово: подключён чат «${r.data.name}». Бот прислал приветствие.` : "Пока не вижу вашего сообщения. Нажмите «Start» в боте и повторите.", r.data.found ? "ok" : "");
}

async function testTelegram() {
  setStatus("tg-status", "Отправляем…", "");
  const r = await api("POST", "/api/telegram/test");
  setStatus("tg-status", r.ok ? "Отправили, проверьте Telegram." : errorText(r), r.ok ? "ok" : "err");
}

async function setTelegramNotify() {
  const r = await api("PUT", "/api/telegram", { notify: $("tg-notify").checked });
  if (r.ok) { renderTelegram(r.data); setStatus("tg-status", r.data.notify ? "Уведомления включены." : "Уведомления выключены.", "ok"); }
}

async function disconnectTelegram() {
  const r = await api("DELETE", "/api/telegram");
  if (r.ok) { renderTelegram(r.data); setStatus("tg-status", "Telegram отключён.", "ok"); }
}

/* ---------- Поисковые запросы ---------- */

function queryChecks(boxId, title, items, key, text) {
  $(boxId).replaceChildren(...(items.length ? [el("div", { className: "launch-head", textContent: title })] : []), ...items.map((item) => {
    const input = el("input", { type: "checkbox", checked: true });
    input.dataset.value = item[key];
    return el("label", { className: "check" }, input, ` ${text(item)}`);
  }));
}

function renderQueryProposals(minus, keywords) {
  queryChecks("q-minus", "Добавить в минус-слова", minus, "word",
    (g) => `«${g.word}»: ${g.queries.length} ${plural(g.queries.length, "запрос", "запроса", "запросов")}, ${g.clicks} ${plural(g.clicks, "клик", "клика", "кликов")}, ${money(g.spend)}. Например: ${g.queries.slice(0, 2).join("; ")}${g.why ? ` (${g.why})` : ""}`);
  queryChecks("q-keys", "Добавить как ключевые фразы (приводили обращения)", keywords, "phrase",
    (k) => `«${k.phrase}»: ${k.leads} ${plural(k.leads, "обращение", "обращения", "обращений")} при ${k.clicks} ${plural(k.clicks, "клике", "кликах", "кликах")}`);
  $("queries-proposals").hidden = !(minus.length || keywords.length);
}

async function loadQueries() {
  const card = $("queries-card");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/queries`);
  if (!result.ok || !result.data.launched) { card.hidden = true; return; }
  const d = result.data;
  card.hidden = false;
  $("queries-hint").textContent = d.queries.length
    ? `Реальные запросы, по которым показывалась реклама за последние 30 дней (обновлено ${d.synced}). Здесь видно, что приводит обращения, а что только тратит деньги.`
    : "Запросы появятся после первых показов: сервис забирает их у Директа раз в день.";
  const row = (g) => el("tr", {}, ...[g.query, String(g.clicks), money(g.spend), String(g.leads)].map((v) => el("td", { textContent: v })));
  $("queries-table").querySelector("tbody").replaceChildren(...d.queries.map(row));
  $("queries-table").hidden = !d.queries.length;
  $("queries-waste").hidden = !d.waste.length;
  $("queries-waste").textContent = d.waste.length
    ? `Потратили больше ${d.waste_from} ₽ и не принесли обращений: ${d.waste.map((w) => `«${w.query}» (${money(w.spend)})`).join(", ")}. Нажмите «Разобрать запросы ИИ», чтобы получить слова для минуса.` : "";
  $("q-analyze").hidden = !d.ai_ready || !d.queries.some((x) => x.clicks > 0);
  renderQueryProposals([], d.keywords);
  if (!d.has_profile && d.queries.length) setStatus("q-status", "Чтобы применять правки, составьте профиль аудитории на шаге «Запуск» вкладки «Объявления».", "");
}

const SLICE_FLAGS = { dear: "дорого", cheap: "дёшево", none: "нет обращений" };

async function loadSlices() {
  const card = $("slices-card");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/slices`);
  if (!result.ok || !result.data.launched) { card.hidden = true; return; }
  const d = result.data;
  card.hidden = false;
  const failed = Object.entries(d.failed);
  $("slices-failed").hidden = !failed.length;
  $("slices-failed").textContent = failed.length ? `Не удалось получить: ${failed.map(([name]) => name).join(", ")}. Сервис попробует ещё раз завтра.` : "";
  $("slices-hint").textContent = d.dimensions.length
    ? `За последние ${d.days} дн. (обновлено ${d.synced}). Сервис копит эти данные каждый день: по ним настраиваются ставки, расписание и площадки.${d.has_goal ? " Пометки «дорого» и «дёшево» сравнивают цену обращения с вашей средней, когда кликов достаточно." : " Чтобы видеть цену обращения, выберите цель «обращение» на странице «Бизнес»."}`
    : "Данные появятся после первых показов: сервис забирает их у Директа и Метрики раз в день.";
  $("slices-list").replaceChildren(...d.dimensions.map((dim) => {
    const head = ["Сегмент", "Доля", dim.visits ? "Визиты" : "Клики", "Расход", "Обращения", "Цена обращения", ""];
    const body = dim.rows.map((r) => el("tr", {}, ...[r.label, `${r.share}%`, String(r.clicks), dim.visits ? "—" : money(r.spend), String(r.leads), money(r.cost_per_lead), SLICE_FLAGS[r.flag] || ""]
      .map((v) => el("td", { textContent: v }))));
    return el("details", { className: "slice" },
      el("summary", { textContent: dim.title }),
      dim.visits ? el("p", { className: "hint", textContent: d.visits_note }) : el("span"),
      el("div", { className: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ...head.map((t) => el("th", { textContent: t })))), el("tbody", {}, ...body))));
  }));
}

async function analyzeQueries() {
  const button = $("q-analyze");
  button.disabled = true;
  setStatus("q-status", "ИИ разбирает запросы…", "");
  const [found, current] = await Promise.all([
    api("POST", `/api/businesses/${currentBusiness.id}/queries/analyze`), api("GET", `/api/businesses/${currentBusiness.id}/queries`)]);
  button.disabled = false;
  if (!found.ok) { setStatus("q-status", errorText(found), "err"); return; }
  renderQueryProposals(found.data.minus, current.ok ? current.data.keywords : []);
  setStatus("q-status", found.data.minus.length
    ? `Проверено запросов: ${found.data.checked}. Нецелевые слова ниже: уберите галочки с лишних и примените.`
    : `Проверено запросов: ${found.data.checked}. Нецелевых запросов ИИ не нашёл.`, "ok");
}

async function applyQueries() {
  const picked = (boxId) => [...$(boxId).querySelectorAll("input:checked")].map((i) => i.dataset.value);
  const button = $("q-apply");
  button.disabled = true;
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/queries/apply`, { minus: picked("q-minus"), keywords: picked("q-keys") });
  button.disabled = false;
  if (!r.ok) { setStatus("q-status", errorText(r), "err"); return; }
  const skipped = r.data.skipped.length ? ` Не принято: ${r.data.skipped.map((s) => `«${s.item}» (${s.reason})`).join("; ")}.` : "";
  setStatus("q-status", `Готово. В Директ добавлено минус-слов: ${r.data.minus.length}, фраз: ${r.data.keywords.length}.${skipped}`, "ok");
  await loadQueries();
}

async function loadSeries() {
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/series?days=14`);
  renderChart(result.ok ? result.data.days : []);
}

function renderGoal(data) {
  const line = $("goal-line");
  const t = data.totals;
  if (!t || !t.cost_per_client || !data.target_cpa) { line.hidden = true; return; }
  const ok = t.cost_per_client <= data.target_cpa;
  line.hidden = false;
  line.className = "goal " + (ok ? "good" : "bad");
  line.textContent = ok
    ? `Клиент обходится в ${money(t.cost_per_client)}, это дешевле вашей цели (${money(data.target_cpa)}). Так держать.`
    : `Клиент обходится в ${money(t.cost_per_client)}, это дороже вашей цели (${money(data.target_cpa)}). Сервис ищет, что можно улучшить.`;
}

async function renderDecisions() {
  const card = $("decisions-card");
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/changes`);
  const items = result.ok ? result.data.changes.filter((c) => c.can_decide) : [];
  card.hidden = !items.length;
  const dot = $("nav-home-dot");   // решения ждут на главной
  dot.hidden = !items.length;
  dot.textContent = String(items.length);
  $("decisions").replaceChildren(...items.map((c) => {
    const yes = el("button", { className: "small", textContent: "Подтвердить" });
    yes.addEventListener("click", () => decide(c, "approve"));
    const no = el("button", { className: "small secondary", textContent: "Не делать" });
    no.addEventListener("click", () => decide(c, "reject"));
    return el("li", {},
      el("div", { className: "text" }, el("div", { className: "what", textContent: c.unit ? `${c.action}: ${c.unit}` : c.action }), el("div", { textContent: c.reason })),
      el("div", { className: "btns" }, yes, no));
  }));
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
  renderDecisions();
}

async function stopAll() {
  if (!(await askConfirm("Остановить всю рекламу?", "Показы прекратятся сразу. Включить обратно можно в один клик.", "Остановить"))) return;
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/stop-all`);
  if (!result.ok) { say(errorText(result), "err"); return; }
  say("Реклама остановлена.");
  await Promise.all([refreshStopbar(), loadStats()]);
}

async function resumeAll() {
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/resume-all`);
  if (!result.ok) { say(errorText(result), "err"); return; }
  say("Реклама включена обратно.");
  await Promise.all([refreshStopbar(), loadStats()]);
}

async function loadStats() {
  const has = !!currentBusiness;
  $("stats-empty").hidden = has;
  $("stats-body").hidden = !has;
  if (!has) {
    homeAction = () => showPage("business");
    $("home-eyebrow").textContent = "Добро пожаловать";
    $("home-title").textContent = "Расскажите о своём бизнесе";
    $("home-text").textContent = "Это займёт пару минут. Остальное сервис сделает сам.";
    $("home-cta").textContent = "Начать";
    $("home-cta").hidden = false;
    for (const id of ["sim-start", "home-progress", "tiles", "goal-line", "chart-card", "budget-box", "checklist-card", "decisions-card", "hero-tiles", "stats-tile"]) $(id).hidden = true;
    return;
  }
  $("hero-tiles").hidden = false;
  renderPhotoTile();
  $("stats-title").textContent = `Результаты: ${currentBusiness.name}`;
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/overview`);
  if (!result.ok) { say(errorText(result), "err"); return; }
  const data = result.data;
  renderChecklist(data.checklist || []);
  renderHome(data);
  renderGoal(data);
  if (data.real || data.example) loadSeries(); else renderChart([]);
  if (data.real || data.example) loadInsights(); else $("insights-card").hidden = true;
  loadLeads();
  if (data.real) { loadReport(); loadVisitorGoals(); loadQueries(); loadSlices(); } else { $("report-card").hidden = true; $("goals-card").hidden = true; $("queries-card").hidden = true; $("slices-card").hidden = true; }
  renderDecisions();
  $("example-notice").hidden = !data.example;
  $("stats-needs").textContent = data.real || data.example ? data.needs : "";
  $("demo-box").hidden = !data.example;
  $("sync-now").hidden = !data.real;
  for (const b of document.querySelectorAll("[data-advance]")) b.hidden = !data.example;
  $("sim-reset").hidden = !data.example;
  const syncText = data.real
    ? (data.sync.at ? `Данные обновлены: ${new Date(data.sync.at).toLocaleString("ru-RU")}` : "Данные ещё не обновлялись.") : "";
  $("stats-sync").textContent = syncText;
  $("home-sim-day").textContent = data.real ? syncText : "";
  $("sim-day").textContent = data.example ? `Сегодня в примере: ${data.today}` : "";
  $("units-empty").hidden = data.units.length > 0;
  $("clients-box").hidden = !(data.real && data.conversions);
  if (!$("clients-day").value) $("clients-day").value = new Date().toISOString().slice(0, 10);
  const notices = $("notices");
  notices.replaceChildren(...(data.notices || []).map((n) => el("li", { textContent: n })));

  // Блок статистики стоит в сетке главной рядом с «Нашими работами» и «Идеями стратега»; пока рекламы нет, в нём прочерки
  const tiles = $("tiles");
  tiles.hidden = false;
  $("stats-tile").hidden = false;
  $("hero-tiles").classList.toggle("nodata", !data.real && !data.example);
  tiles.replaceChildren();
  if (data.totals) {
    const t = data.totals;
    $("stats-period").textContent = `За ${t.days} ${plural(t.days, "день", "дня", "дней")}`;
    tiles.append(
      tile(money(t.spend), "Расход"),
      tile(String(t.leads), "Обращения"),
      tile(String(t.clients), t.clients_estimated ? "Клиенты (оценка)" : "Клиенты"),
      tile(money(t.cost_per_client), t.clients_estimated ? "Цена клиента (оценка)" : "Цена клиента"),
    );
  } else {
    $("stats-period").textContent = "Пока нет данных";
    tiles.append(tile("—", "Расход"), tile("—", "Обращения"), tile("—", "Клиенты"), tile("—", "Цена клиента"));
  }
  $("budget-box").hidden = !data.budget || !(data.real || data.example);
  if (data.budget) {
    const b = data.budget;
    $("budget-text").textContent = `Потрачено ${money(b.spent_week)} из ${money(b.weekly)} (${b.percent}%).`;
    const fill = $("budget-fill");
    fill.style.width = `${Math.min(b.percent, 100)}%`;
    fill.className = "fill" + (b.percent >= 100 ? " full" : b.percent >= 90 ? " warn" : "");
  }

  $("units-hint").hidden = data.units.length === 0;
  $("campaign-groups").replaceChildren(...(data.campaigns || []).map((g) => campaignGroup(g, (data.campaigns || []).length > 1)));
}

// Один запуск рекламы = одна кампания в Директе: у неё общий бюджет, стратегия и свои объявления
function campaignHint(g) {
  const parts = ["Этот бюджет делят все объявления кампании."];
  if (g.strategy === "conversions") parts.push("Работает на «Максимум конверсий»: Яндекс подбирает ставки под обращения.");
  else if (!g.conversions) parts.push("Сейчас оплата за клики («Максимум кликов»). Подключите Метрику на странице «Бизнес»: когда обращений станет 10 в неделю, сервис переведёт кампанию на «Максимум конверсий».");
  else parts.push(`Сейчас оплата за клики («Максимум кликов»). Обращений за неделю: ${g.leads_week} из ${g.switch_at}. Когда их станет ${g.switch_at}, сервис переведёт кампанию на «Максимум конверсий»: Яндекс начнёт подбирать ставки под обращения.`);
  if (g.warmup_left > 0) parts.push(`Ещё ${g.warmup_left} ${plural(g.warmup_left, "день", "дня", "дней")} сервис кампанию не трогает: Яндексу нужно время, чтобы собрать данные.`);
  return parts.join(" ");
}

function campaignGroup(g, several) {
  const started = g.created_day ? new Date(g.created_day).toLocaleDateString("ru-RU") : "";
  const title = el("h3", { textContent: started ? `Запуск от ${started}` : "Кампания" });
  const head = el("div", { className: "camp-head" }, title, el("span", { className: `badge ${g.state}`, textContent: g.state_label }));
  const group = el("section", { className: "camp-group" }, head);
  if (several) head.append(el("span", { className: "camp-total", textContent: `Расход ${money(g.spend)} · обращений ${g.leads}` }));
  if (g.editable) {
    const budget = el("div", { className: "camp-budget" });
    const show = () => {
      const edit = el("button", { type: "button", className: "small secondary", textContent: "Изменить бюджет" });
      edit.addEventListener("click", form);
      budget.replaceChildren(el("span", { className: "camp-sum", textContent: `Бюджет: ${money(g.weekly)} в неделю` }), edit);
    };
    const form = () => {
      const input = el("input", { type: "number", min: String(g.min_weekly), step: "100", value: String(Math.round(g.weekly)) });
      input.setAttribute("aria-label", "Бюджет кампании в неделю, рублей");
      const save = el("button", { type: "button", className: "small", textContent: "Сохранить" });
      const cancel = el("button", { type: "button", className: "small secondary", textContent: "Отмена" });
      cancel.addEventListener("click", show);
      save.addEventListener("click", async () => {
        const weekly = Number(input.value);
        if (!Number.isInteger(weekly) || weekly < g.min_weekly) { say(`Введите целое число рублей, не меньше ${g.min_weekly} в неделю.`, "err"); return; }
        if (weekly === Math.round(g.weekly)) { show(); return; }
        save.disabled = true;
        const result = await api("PUT", `/api/businesses/${currentBusiness.id}/campaigns/${g.id}/budget`, { weekly });
        save.disabled = false;
        if (!result.ok) { say(errorText(result), "err"); return; }
        say(`Бюджет кампании изменён: ${money(weekly)} в неделю. Новое значение уже в Яндекс Директе.`);
        await loadStats();
      });
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") save.click(); else if (e.key === "Escape") show(); });
      budget.replaceChildren(el("label", { className: "camp-sum", textContent: "Бюджет в неделю, ₽" }), input, save, cancel);
      input.focus();
    };
    show();
    group.append(budget, el("p", { className: "hint", textContent: campaignHint(g) }));
  }

  const shared = g.editable;   // в общей кампании у объявления своего бюджета нет
  const cells = (names) => names.map((n) => el("th", { textContent: n }));
  const head2 = el("tr", {}, ...cells(["Объявление", "Оценка", ...(shared ? [] : ["Бюджет в неделю"]), "Расход", "Обращения", "Клиенты", "Цена клиента"]));
  const body = el("tbody");
  for (const u of g.units) {
    const name = el("td", { textContent: u.title });
    if (u.note) name.append(el("span", { className: "sub", textContent: u.note }));
    if (u.in_test) name.append(el("span", { className: "sub", textContent: "Идёт сравнение двух вариантов" }));
    else if (u.variant) name.append(el("span", { className: "sub", textContent: "Второй вариант для сравнения" }));
    const cell = (v) => el("td", { textContent: v });
    const badge = el("span", { className: `badge ${u.verdict_code}`, textContent: u.verdict });
    body.append(el("tr", { className: u.active ? "" : "off" }, name, el("td", {}, badge), ...(shared ? [] : [cell(money(u.weekly_budget))]),
      cell(money(u.spend)), cell(String(u.leads)), cell(String(u.clients)), cell(money(u.cost_per_client))));
  }
  group.append(el("div", { className: "table-wrap" }, el("table", {}, el("thead", {}, head2), body)));
  return group;
}

async function openLaunch() {
  if (!currentBusiness) return;
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/launch/preview`);
  if (!result.ok) { say(errorText(result), "err"); $("launch-card").hidden = true; return; }
  const p = result.data;
  $("launch-summary").textContent =
    `Кабинет: ${p.account}. Где показывать: ${p.city}${p.districts.length ? (p.by_district ? `, только районы: ${p.districts.join(", ")}` : `, районы в текстах и фразах: ${p.districts.join(", ")}`) : ""}. Одна кампания: ${adsPhrase(p.count)} с общим бюджетом ${p.weekly} ₽ в неделю. Ведёт на: ${p.link}${p.tagged ? " (к ссылке добавятся метки utm: в Метрике будет видно, с какого объявления пришёл человек)" : ""}.`;
  $("launch-variants").replaceChildren(...p.variants.map((v) =>
    el("li", {}, el("div", { className: "t", textContent: v.title }), el("div", { className: "hint", textContent: v.text }))));
  $("launch-warnings").replaceChildren(...p.warnings.map((w) => el("li", { textContent: w })));
  const queries = [
    p.audience ? `Кому показываем: ${p.audience.summary}` : "Профиль аудитории не составлен.",
    p.keywords.length ? `Ключевые фразы (${p.keywords.length}): ${p.keywords.join("; ")}.` : "Ключевых фраз нет, запросы подберёт автотаргетинг.",
    `Автотаргетинг: ${p.autotargeting.categories.join(", ")}. Бренды: ${p.autotargeting.brands.join(", ")}.`,
    `Минус-слова (${p.negatives.length}): ${p.negatives.join(", ")}.`,
  ];
  $("launch-queries").replaceChildren(...queries.map((t) => el("p", { className: "hint", textContent: t })));
  $("marking-help").textContent = p.marking.help;
  let confirmed = p.marking.confirmed;
  if (!confirmed) {  // Директ сам скажет, внесены ли данные: тогда подтверждать вручную не нужно
    const mk = await api("GET", `/api/businesses/${currentBusiness.id}/marking`);
    if (mk.ok) {
      confirmed = mk.data.filled;
    }
  }
  $("marking-ok").checked = confirmed;
  $("marking-box").hidden = confirmed;
  $("launch-go").disabled = !confirmed;
  $("launch-card").hidden = false;
  $("launch-card").scrollIntoView({ block: "nearest" });
}

async function loadMarking() {
  const state = $("mk-state");
  $("marking-card").hidden = !currentBusiness;   // как и Метрика: пока бизнес не сохранён, внести данные некуда
  if (!currentBusiness) return;
  if (!accountsList.length) await loadAccounts();   // при первом открытии страницы список кабинетов мог ещё не прийти
  if (!accountsList.length) {
    state.hidden = false;
    state.className = "notice";
    state.textContent = "Сначала подключите рекламный кабинет Яндекс Директа на странице «Подключения»: данные рекламодателя вносятся в него.";
    $("mk-form").hidden = true;
    return;
  }
  $("mk-form").hidden = false;
  const mk = await api("GET", `/api/businesses/${currentBusiness.id}/marking`);
  if (!mk.ok) { $("mk-state").hidden = false; $("mk-state").textContent = errorText(mk); return; }
  if (!$("mk-kind").options.length) {
    $("mk-kind").replaceChildren(...Object.entries(mk.data.kinds).map(([v, t]) => el("option", { value: v, textContent: t })));
  }
  state.hidden = false;
  state.className = "notice " + (mk.data.filled ? "proposals" : "");
  state.textContent = mk.data.filled
    ? `Данные внесены: ${mk.data.name}. Можно запускать рекламу. Чтобы изменить, заполните форму ниже ещё раз.`
    : "Данные ещё не внесены. Без них Директ не сможет промаркировать рекламу, и запустить её не получится.";
}

async function saveMarking() {
  const btn = $("mk-save");
  btn.disabled = true;
  $("mk-status").textContent = "Передаём в Директ…";
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/marking`, {
    kind: $("mk-kind").value, tin: $("mk-tin").value, name: $("mk-name").value, phone: $("mk-phone").value });
  btn.disabled = false;
  $("mk-status").textContent = "";
  if (!r.ok) {   // ошибка Директа остаётся в карточке: так её можно прочитать и показать в поддержке
    $("mk-state").hidden = false;
    $("mk-state").className = "notice bad";
    $("mk-state").textContent = errorText(r);
    say(errorText(r), "err");
    return;
  }
  $("mk-tin").value = "";  // ИНН на странице не оставляем
  say(r.data.message, r.data.filled ? "" : "err");
  $("mk-name").value = "";
  if (r.data.filled) { $("marking-ok").checked = true; $("marking-box").hidden = true; $("launch-go").disabled = false; }
  await loadMarking();
}

async function confirmLaunch() {
  const button = $("launch-go");
  button.disabled = true;
  button.textContent = "Создаём рекламу…";
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/launch`, { marking_confirmed: $("marking-ok").checked });
  button.disabled = false;
  button.textContent = "Создать и отправить на проверку";
  $("launch-error").hidden = true;
  if (!result.ok) {   // ответ Директа остаётся на экране, а не пропадает вместе со всплывающей подсказкой
    $("launch-error").textContent = errorText(result);
    $("launch-error").hidden = false;
    say(errorText(result), "err");
    return;
  }
  $("launch-card").hidden = true;
  say(result.data.message);
  await Promise.all([loadStats(), refreshStopbar()]);
}

async function syncNow() {
  toast("Обновляем данные из Яндекс Директа…");
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/sync`);
  if (!result.ok) { say(errorText(result), "err"); await loadStats(); return; }
  say(result.data.changes ? `Готово. Новых записей в истории: ${result.data.changes}.` : "Данные обновлены.");
  await Promise.all([loadStats(), refreshStopbar()]);
}

async function saveClients() {
  const count = $("clients-count").value;
  if (count === "" || !$("clients-day").value) { setStatus("clients-status", "Укажите дату и число клиентов.", "err"); return; }
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/clients`, { day: $("clients-day").value, count: Number(count) });
  setStatus("clients-status", "");
  say(r.ok ? "Записали." : errorText(r), r.ok ? "" : "err");
  if (r.ok) { $("clients-count").value = ""; await loadStats(); }
}

async function simAction(path, body) {
  if (!currentBusiness) return;
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/sim/${path}`, body);
  if (!result.ok) { say(errorText(result), "err"); return; }
  const made = result.data.changes_made;
  const word = currentBusiness.mode === "ask" ? "Предложений" : "Изменений";
  say(made ? `Готово. ${word} за это время: ${made}. Подробности внизу страницы «Результаты».` : "Готово.");
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
  say(result.ok ? (verb === "approve" ? "Готово, сделали." : "Хорошо, ничего не меняем.") : errorText(result), result.ok ? "" : "err");
  await Promise.all([currentPage === "history" ? loadHistory() : Promise.resolve(), loadStats(), refreshStopbar()]);
}

async function rollbackChange(change, force = false) {
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/changes/${change.id}/rollback`, { force });
  if (result.status === 409 && !force) {
    if (await askConfirm("Всё равно отменить?", errorText(result), "Отменить изменение")) await rollbackChange(change, true);
    return;
  }
  if (!result.ok) { say(errorText(result), "err"); return; }
  say("Изменение отменено.");
  await loadHistory();
}

/* ---------- Кабинеты ---------- */

let accountsList = [];

async function loadAccounts() {
  const result = await api("GET", "/api/accounts");
  if (result.status === 401) { showAuth(); return; }
  const list = $("accounts");
  list.replaceChildren();
  const accounts = result.ok ? result.data.accounts : [];
  accountsList = accounts;
  $("connect-title").textContent = accounts.length ? "Подключить ещё один кабинет" : "Подключить кабинет";
  if (!$("platforms-list").children.length) loadPlatforms();
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

// VK Реклама и Авито: форма с ключами API под каждой площадкой, список и подсказки приходят с сервера
async function loadPlatforms() {
  const result = await api("GET", "/api/accounts/platforms");
  if (!result.ok) return;
  const list = result.data.platforms.filter((p) => p.keys);
  $("platforms-card").hidden = list.length === 0;
  $("platforms-list").replaceChildren(...list.map((p) => {
    const id = el("input", { type: "text", autocomplete: "off", placeholder: "client_id" });
    const secret = el("input", { type: "password", autocomplete: "off", placeholder: "client_secret" });
    const title = el("input", { type: "text", placeholder: "Название кабинета (для себя, необязательно)" });
    const status = el("p", { className: "status", role: "status" });
    const go = el("button", { type: "submit", textContent: `Подключить ${p.label}` });
    const form = el("form", {}, el("p", { className: "hint", textContent: p.can }), el("p", { className: "hint", textContent: p.where }),
      title, id, secret, el("p"), go, status);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      go.disabled = true;
      status.className = "status";
      status.textContent = "Проверяем ключи…";
      const r = await api("POST", "/api/accounts/keys", { platform: p.id, title: title.value, client_id: id.value, client_secret: secret.value });
      go.disabled = false;
      if (!r.ok) { status.className = "status err"; status.textContent = errorText(r); return; }
      status.className = "status ok";
      status.textContent = `Подключено. Найдено: ${r.data.campaigns_count}.`;
      id.value = ""; secret.value = ""; title.value = "";
      await loadAccounts();
    });
    return el("details", { className: "own-text" }, el("summary", { textContent: `${p.label}${p.ready ? "" : " (подключение и просмотр)"}` }), form);
  }));
}

async function removeAccount(account) {
  if (!(await askConfirm("Удалить кабинет?", `«${account.title}» исчезнет из Автобидера. В самом кабинете (${account.platform}) ничего не изменится.`, "Удалить"))) return;
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

  window.addEventListener("popstate", () => { if (!$("view-app").hidden) showPage(pageFromHash(), false); });
  $("example-off").addEventListener("click", () => $("sim-reset").click());
  $("biz-switch").addEventListener("change", onSwitch);
  bindBizMenu();
  bindLightbox();
  $("nav-home").addEventListener("click", () => showPage("home"));
  $("home-cta").addEventListener("click", () => { if (homeAction) homeAction(); });
  $("nav-business").addEventListener("click", () => showPage("business"));
  $("nav-accounts").addEventListener("click", () => showPage("accounts"));
  $("nav-stats").addEventListener("click", () => showPage("stats"));
  $("nav-media").addEventListener("click", () => showPage("media"));
  $("nav-creatives").addEventListener("click", () => showPage("creatives"));
  $("land-login").addEventListener("click", () => goToAuth("login"));
  $("land-start").addEventListener("click", () => goToAuth("register"));
  $("land-cta").addEventListener("click", () => goToAuth("register"));
  const toAds = (step) => () => { showPage("creatives"); setStep(step); };
  $("chip-banner").addEventListener("click", toAds("banners"));
  $("chip-ideas").addEventListener("click", toAds("ideas"));
  $("ptile-ideas-go").addEventListener("click", toAds("ideas"));
  $("chip-photo").addEventListener("click", () => showPage("media"));
  $("chip-budget").addEventListener("click", () => showPage("business"));
  $("units-empty-go").addEventListener("click", () => showPage("creatives"));
  $("briefs-gen").addEventListener("click", generateBriefs);
  for (const btn of document.querySelectorAll("#cr-steps .step")) btn.addEventListener("click", () => setStep(btn.dataset.step));
  $("ideas-next").addEventListener("click", () => setStep("banners"));
  $("banners-next").addEventListener("click", () => setStep("launch"));
  $("launch-back").addEventListener("click", () => setStep("banners"));
  $("leads-copy").addEventListener("click", copyLeadsSnippet);
  $("leads-newkey").addEventListener("click", newLeadsKey);
  $("tg-connect").addEventListener("click", connectTelegram);
  $("tg-check").addEventListener("click", checkTelegram);
  $("tg-test").addEventListener("click", testTelegram);
  $("tg-notify").addEventListener("change", setTelegramNotify);
  $("tg-disconnect").addEventListener("click", disconnectTelegram);
  $("q-analyze").addEventListener("click", analyzeQueries);
  $("q-apply").addEventListener("click", applyQueries);
  $("aud-bids-apply").addEventListener("click", () => changeBids("POST"));
  $("aud-bids-clear").addEventListener("click", () => changeBids("DELETE"));
  $("aud-build").addEventListener("click", buildAudience);
  $("aud-save").addEventListener("click", saveAudience);
  $("ads-launch").addEventListener("click", () => { showPage("home"); openLaunch(); });
  $("ads-ai-all").addEventListener("click", makeAllBanners);
  $("ads-ai-redo").addEventListener("click", redrawAllBanners);
  $("cr-gen-texts").addEventListener("click", () => generateTexts());
  $("photos-check").addEventListener("click", checkPhotos);
  $("insights-ai").addEventListener("click", analyzeResults);
  $("cr-gen-images").addEventListener("click", generateImages);
  $("cr-add-own").addEventListener("click", addOwnText);
  $("cr-own-title-n").replaceWith(counterEl($("cr-own-title"), 56));
  $("cr-own-body-n").replaceWith(counterEl($("cr-own-body"), 81));
  $("connect-direct").addEventListener("click", connectDirect);
  $("connect-cancel").addEventListener("click", stopConnect);
  $("m-counter").addEventListener("change", () => { loadGoals(""); loadSnippet(); });
  $("m-snippet-copy").addEventListener("click", copySnippet);
  $("m-auto").addEventListener("click", createMetrikaAuto);
  $("m-health-check").addEventListener("click", () => loadHealth(true));
  $("m-save").addEventListener("click", saveMetrika);
  $("m-clear").addEventListener("click", clearMetrika);
  $("marking-ok").addEventListener("change", () => { $("launch-go").disabled = !$("marking-ok").checked; });
  $("b-nationwide").addEventListener("change", syncNationwide);
  for (const id of ["b-budget", "b-cpa", "b-check", "b-share", "b-description"]) $(id).addEventListener("input", scheduleAdvice);
  $("launch-go").addEventListener("click", confirmLaunch);
  $("mk-save").addEventListener("click", saveMarking);
  $("mk-open").addEventListener("click", async () => { showPage("business"); await loadMarking(); $("marking-card").scrollIntoView({ block: "start" }); });
  $("launch-cancel").addEventListener("click", () => { $("launch-card").hidden = true; });
  $("sync-now").addEventListener("click", syncNow);
  $("clients-save").addEventListener("click", saveClients);
  $("stop-all").addEventListener("click", stopAll);
  $("resume-all").addEventListener("click", resumeAll);
  $("sim-start").addEventListener("click", () => simAction("start"));
  $("sim-reset").addEventListener("click", async () => {
    if (await askConfirm("Выключить пример?", "Примерная статистика и история примера удалятся. Вашей настоящей рекламы это не касается.", "Выключить")) simAction("reset");
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
  $("screen-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "photo", "screen"); e.target.value = ""; });
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

let connectCountdown = null;

function stopConnect() {
  clearTimeout(connectTimer);
  clearInterval(connectCountdown);
  connectTimer = null;
  $("connect-panel").hidden = true;
  $("connect-direct").disabled = false;
}

async function connectDirect() {
  setStatus("connect-status", "");
  $("connect-direct").disabled = true;
  const started = await api("POST", "/api/direct/connect/start");
  if (!started.ok) { setStatus("connect-status", errorText(started), "err"); $("connect-direct").disabled = false; return; }
  const { pending_id, user_code, url, interval, expires_in } = started.data;
  $("connect-code").textContent = user_code;
  $("connect-url").href = url;
  $("connect-url").textContent = url.replace(/^https?:\/\//, "");
  $("connect-panel").hidden = false;
  // Код живёт ограниченное время: показываем остаток и сами выдаём новый, когда он истёк.
  const until = Date.now() + (expires_in || 300) * 1000;
  clearInterval(connectCountdown);
  connectCountdown = setInterval(() => {
    const left = Math.round((until - Date.now()) / 1000);
    if (left <= 0) { stopConnect(); connectDirect(); return; }
    $("connect-wait").textContent = `Ждём подтверждения от Яндекса… Код действует ещё ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  }, 1000);

  const poll = async () => {
    const result = await api("POST", "/api/direct/connect/poll", { pending_id });
    if (!result.ok) { stopConnect(); setStatus("connect-status", errorText(result), "err"); return; }
    if (result.data.status === "ok") {
      stopConnect();
      setStatus("connect-status", `Кабинет «${result.data.login}» подключён.`, "ok");
      await loadAccounts();
      loadMetrika();
      loadMarking();
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
