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
  $("view-auth").hidden = false;
  $("view-app").hidden = true;
  $("user").hidden = true;
  $("nav").hidden = true;
  $("biz-switch").hidden = true;
  setAuthMode("login");
}

async function showApp(email) {
  currentBusiness = null; // не тащим бизнес предыдущего пользователя
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

const PAGES = ["home", "business", "creatives", "stats", "history", "accounts"];
let currentPage = "home";

// Адрес страницы лежит в ссылке (#/business): перезагрузка остаётся на месте, «назад» и «вперёд» работают.
function pageFromHash() {
  const name = location.hash.replace(/^#\/?/, "");
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
  if (name !== "creatives") clearTimeout(creativeTimer);
  if (name === "creatives") loadCreatives();
  if (name === "home" || name === "stats") loadStats();
  if (name === "history") { setStatus("history-status", ""); loadHistory(); }
  if (name === "accounts" && accountsList.length) showCampaigns(accountsList[0].id);  // кампании кабинета видны сразу
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
  fillForm(currentBusiness);
  renderFiles();
  refreshStopbar();
  loadMetrika();
  if ($("biz-switch").options.length) $("biz-switch").value = String(currentBusiness.id);
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
  if ($("biz-switch").options.length) $("biz-switch").value = "new";
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
  setStatus("business-status", "");
  say(isNew ? "Бизнес сохранён. Ниже можно загрузить фото и логотип." : "Сохранено.");
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
  $("cr-image-note").textContent = "Создание картинок и видео пока не подключено на сервере. Свои фото можно загрузить на вкладке «Мой бизнес».";
  $("cr-image-form").hidden = !caps.image_ai;
  $("cr-spend").textContent = caps.image_ai || caps.text_ai
    ? `Потрачено на создание: $${spend.spent_usd.toFixed(2)} из $${spend.limit_usd.toFixed(0)}. Больше лимита сервис не потратит.` : "";

  renderTexts(creatives.filter((c) => c.kind === "text").reverse());  // старые сверху, новые снизу
  const images = creatives.filter((c) => c.kind === "image");
  const videos = creatives.filter((c) => c.kind === "video");
  renderMedia($("cr-images"), images, caps.video_ai);
  renderMedia($("cr-videos"), videos, false);
  $("cr-videos-empty").hidden = videos.length > 0;
  loadAds();
  if (creatives.some((c) => c.status === "working")) creativeTimer = setTimeout(loadCreatives, 6000);
}

// Готовые объявления: текст вместе с картинкой, как они уйдут в Директ.
async function loadAds() {
  const result = await creativeCall("GET", "/ads");
  if (!result.ok) return;
  const { ads, count, per_unit, site, has_maps, has_images } = result.data;
  const target = site || (has_maps ? "вашу страницу в Яндекс Картах" : "");
  $("ads-summary").textContent = !count
    ? "Бюджет слишком мал для запуска: увеличьте его на странице «Бизнес». Так будут выглядеть объявления."
    : target
      ? `При запуске будет создано объявлений: ${count}, по ${per_unit} ₽ в неделю каждое. Каждое ведёт на ${target}.`
      : `При запуске будет создано объявлений: ${count}, по ${per_unit} ₽ в неделю каждое. Чтобы запустить, укажите сайт или ссылку на Яндекс Карты на странице «Бизнес»: объявлению нужно куда вести.`;
  const note = $("ads-note");
  note.hidden = has_images;
  note.textContent = "Картинок пока нет. Нарисуйте и выберите их ниже или загрузите свои фото на странице «Бизнес»: баннеры соберутся сами.";
  const stamp = Date.now();
  $("ads").replaceChildren(...ads.map((a) => {
    const pic = a.banner_url
      ? el("img", { src: `${a.banner_url}?t=${stamp}`, alt: a.title, loading: "lazy" })
      : el("div", { className: "no-pic", textContent: "Без картинки" });
    const host = (site || (has_maps ? "yandex.ru/maps" : "ваш-сайт.ru")).replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    const body = el("div", { className: "ad-body" },
      el("div", { className: "ad-title", textContent: a.title }),
      el("div", { className: "ad-text", textContent: a.text }),
      el("div", { className: "ad-site" }, el("b", { textContent: "Реклама" }), host));
    const action = a.own
      ? el("button", { className: "small secondary", textContent: "Не использовать" })
      : el("button", { className: "small", textContent: "Использовать этот" });
    action.addEventListener("click", async () => {
      let id = a.creative_id;
      if (!a.own) {
        const added = await creativeCall("POST", "/texts", { title: a.title, body: a.text });
        if (!added.ok) { say(errorText(added), "err"); return; }
        id = added.data.id;
      }
      const r = await creativeCall("PUT", `/${id}`, { status: a.own ? "rejected" : "approved" });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await loadCreatives();
    });
    const badge = el("span", { className: "badge " + (a.own ? "good" : ""), textContent: a.own ? "Ваш выбор" : "Подставит сервис" });
    return el("div", { className: "ad" }, pic, body, el("div", { className: "ad-foot" }, badge, action));
  }));
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
      el("span", { className: "pill", textContent: `${STATUS_LABEL[c.status] || c.status}, ${SOURCE_LABEL[c.source] || c.source}` }));
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

function renderMedia(grid, items, canMakeVideo) {
  grid.replaceChildren();
  for (const c of items) {
    const tile = el("div", { className: `thumb ${c.status}` });
    if (c.status === "working") tile.append(el("div", { className: "wait", textContent: c.kind === "video" ? "Готовим видео… это займёт несколько минут" : "Рисуем… около минуты" }));
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

async function generateTexts() {
  const button = $("cr-gen-texts");
  button.disabled = true;
  setStatus("cr-texts-status", "Придумываем…");
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
    const body = el("div", {}, el("div", { className: "title", textContent: s.title + (s.optional && !s.done ? " (желательно)" : "") }));
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
  const W = 640, H = 220, L = 40, R = 12, T = 12, B = 28;
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
  const dot = $("nav-history-dot");
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
    for (const id of ["sim-start", "home-progress", "tiles", "goal-line", "chart-card", "budget-box", "checklist-card", "decisions-card"]) $(id).hidden = true;
    return;
  }
  $("stats-title").textContent = `Результаты: ${currentBusiness.name}`;
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/overview`);
  if (!result.ok) { say(errorText(result), "err"); return; }
  const data = result.data;
  renderChecklist(data.checklist || []);
  renderHome(data);
  renderGoal(data);
  if (data.real || data.example) loadSeries(); else renderChart([]);
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
  $("budget-box").hidden = !data.budget || !(data.real || data.example);
  if (data.budget) {
    const b = data.budget;
    $("budget-text").textContent = `Потрачено ${money(b.spent_week)} из ${money(b.weekly)} (${b.percent}%).`;
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
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/launch/preview`);
  if (!result.ok) { say(errorText(result), "err"); $("launch-card").hidden = true; return; }
  const p = result.data;
  $("launch-summary").textContent =
    `Кабинет: ${p.account}. Город: ${p.city}. Объявлений: ${p.count}, по ${p.per_unit} ₽ в неделю каждое (всего ${p.total} ₽). Ведёт на: ${p.link}.`;
  $("launch-variants").replaceChildren(...p.variants.map((v) =>
    el("li", {}, el("div", { className: "t", textContent: v.title }), el("div", { className: "hint", textContent: v.text }))));
  $("launch-warnings").replaceChildren(...p.warnings.map((w) => el("li", { textContent: w })));
  $("marking-help").textContent = p.marking.help;
  $("marking-ok").checked = p.marking.confirmed;
  $("marking-box").hidden = p.marking.confirmed;
  $("launch-go").disabled = !p.marking.confirmed;
  $("launch-card").hidden = false;
  $("launch-card").scrollIntoView({ block: "nearest" });
}

async function confirmLaunch() {
  const button = $("launch-go");
  button.disabled = true;
  button.textContent = "Создаём рекламу…";
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/launch`, { marking_confirmed: $("marking-ok").checked });
  button.disabled = false;
  button.textContent = "Создать и отправить на проверку";
  if (!result.ok) { say(errorText(result), "err"); return; }
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
  say(made ? `Готово. ${word} за это время: ${made}. Подробности на странице «История».` : "Готово.");
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
  if (!(await askConfirm("Удалить кабинет?", `«${account.title}» исчезнет из Автобидера. В самом Яндекс Директе ничего не изменится.`, "Удалить"))) return;
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
  $("nav-home").addEventListener("click", () => showPage("home"));
  $("home-cta").addEventListener("click", () => { if (homeAction) homeAction(); });
  $("nav-business").addEventListener("click", () => showPage("business"));
  $("nav-accounts").addEventListener("click", () => showPage("accounts"));
  $("nav-stats").addEventListener("click", () => showPage("stats"));
  $("nav-creatives").addEventListener("click", () => showPage("creatives"));
  $("ads-launch").addEventListener("click", () => { showPage("home"); openLaunch(); });
  $("cr-gen-texts").addEventListener("click", generateTexts);
  $("cr-gen-images").addEventListener("click", generateImages);
  $("cr-add-own").addEventListener("click", addOwnText);
  $("cr-own-title-n").replaceWith(counterEl($("cr-own-title"), 56));
  $("cr-own-body-n").replaceWith(counterEl($("cr-own-body"), 81));
  $("connect-direct").addEventListener("click", connectDirect);
  $("connect-cancel").addEventListener("click", stopConnect);
  $("m-counter").addEventListener("change", () => loadGoals(""));
  $("m-save").addEventListener("click", saveMetrika);
  $("m-clear").addEventListener("click", clearMetrika);
  $("marking-ok").addEventListener("change", () => { $("launch-go").disabled = !$("marking-ok").checked; });
  $("launch-go").addEventListener("click", confirmLaunch);
  $("launch-cancel").addEventListener("click", () => { $("launch-card").hidden = true; });
  $("sync-now").addEventListener("click", syncNow);
  $("clients-save").addEventListener("click", saveClients);
  $("stop-all").addEventListener("click", stopAll);
  $("resume-all").addEventListener("click", resumeAll);
  $("nav-history").addEventListener("click", () => showPage("history"));
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
