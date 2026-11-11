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
  $("business-copy").hidden = !b;
  setStatus("business-status", "");
  setStatus("files-status", "");
  setWizard(!b);
  loadAdvice();
}

// Новый бизнес вносится в шесть шагов: что за бизнес, где показывать, деньги, оформление, фото и видео, подключения. У существующего форма целиком.
// Бизнес сохраняется на четвёртом шаге, поэтому на пятом и шестом форма уже недоступна.
const WIZARD_LABELS = ["О бизнесе", "Где показывать", "Деньги", "Оформление", "Фото и видео", "Подключения"];
const WIZARD_SAVE_STEP = 4;
let wizardStep = 1;

function setWizard(on) {
  $("business-form").classList.toggle("wizard", on);
  $("wizard-bar").hidden = $("wizard-nav").hidden = !on;
  $("business-save").textContent = on ? "Сохранить и перейти к фото" : "Сохранить";
  if (on) showWizardStep(1); else { delete $("business-form").dataset.wstep; moveFilesCard(false); }
}

// Карточка загрузки фото живёт на странице «Фото и баннеры»; на четвёртом шаге мастера она переезжает под форму и потом возвращается
function moveFilesCard(toWizard) {
  const card = $("files-card");
  if (toWizard) $("wizard-files").append(card); else if (card.parentElement !== $("media-body")) $("media-body").prepend(card);
}

function openWizardPhotos() {
  setWizard(true);
  showWizardStep(WIZARD_SAVE_STEP + 1);
  moveFilesCard(true);
  renderFiles();
  window.scrollTo(0, 0);
}

function finishWizard() {
  setWizard(false);
  showPage("home");
}

function showWizardStep(n) {
  wizardStep = n;
  $("business-form").dataset.wstep = String(n);
  $("wizard-stepper").replaceChildren(...WIZARD_LABELS.map((label, i) => el("li", { className: i + 1 < n ? "done" : i + 1 === n ? "current" : "" },
    el("span", { className: "n", textContent: i + 1 < n ? "✓" : String(i + 1) }), el("span", { className: "t", textContent: label }))));
  $("wizard-label").textContent = `Шаг ${n} из ${WIZARD_LABELS.length}: ${WIZARD_LABELS[n - 1]}`;
  $("wizard-back").hidden = n === 1 || n === WIZARD_SAVE_STEP + 1;   // бизнес уже сохранён: назад к форме нельзя
  $("wizard-next").hidden = n === WIZARD_SAVE_STEP || n === WIZARD_LABELS.length;   // на последнем шаге формы вместо «Дальше» кнопка сохранения
  $("wizard-next").textContent = n === WIZARD_SAVE_STEP + 1 ? "Дальше: подключения" : "Дальше";
  $("wizard-done").hidden = n !== WIZARD_LABELS.length;
}

function wizardNext() {
  if (wizardStep === 1 && !($("b-name").value.trim() && $("b-kind").value.trim() && $("b-description").value.trim())) { say("Укажите название, чем вы занимаетесь, и расскажите о бизнесе: по описанию сервис пишет объявления.", "err"); return; }
  if (wizardStep === 2 && !$("b-nationwide").checked && !$("b-city").value.trim()) { say("Укажите город или отметьте «Показывать по всей России».", "err"); return; }
  showWizardStep(wizardStep + 1);
  window.scrollTo(0, 0);
}

function wizardBack() {
  showWizardStep(wizardStep - 1);
  window.scrollTo(0, 0);
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
  refreshNav();
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

// Пока бизнеса нет, в меню остаются Главная, Бизнес и Подключения: остальное вело бы в пустые страницы.
// «Результаты» появляются, когда есть реклама или пример. Короткие кнопки-подсказки на главной тоже нужны только с бизнесом.
function updateNav(hasBusiness, hasResults) {
  document.body.classList.toggle("no-biz", !hasBusiness);
  document.body.classList.toggle("no-results", !hasResults);
  $("home-chips").hidden = !hasBusiness;
  $("ptile-ideas").hidden = !hasBusiness;
}

async function refreshNav() {
  if (!currentBusiness) { updateNav(false, false); return; }
  const id = currentBusiness.id;
  updateNav(true, !document.body.classList.contains("no-results"));   // пока ответ не пришёл, оставляем как было
  const r = await api("GET", `/api/businesses/${id}/overview`);
  if (currentBusiness && currentBusiness.id === id) updateNav(true, r.ok && !!(r.data.real || r.data.example));
}

function newBusiness() {
  currentBusiness = null;
  updateNav(false, false);
  $("metrika-card").hidden = $("marking-card").hidden = true;   // от прежнего бизнеса: у нового их нет, пока он не сохранён
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
  if ($("business-form").classList.contains("wizard") && wizardStep < WIZARD_SAVE_STEP) { wizardNext(); return; }   // Enter на первых шагах ведёт дальше, а не сохраняет недописанное
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
  say(isNew ? "Бизнес сохранён. Добавьте фото, логотип и видео: по ним сервис делает баннеры. Этот шаг можно пропустить." : "Сохранено.");
  if (isNew) openWizardPhotos();
  if (guessed) say(`Район определён по адресу: ${result.data.districts}. Проверьте его в поле «Районы».`);
  if (renamed) api("POST", `/api/businesses/${currentBusiness.id}/creatives/photos/check`).then(refreshAssets);   // забракованные фото ИИ посмотрит заново: вывеска с новым названием своя
}

async function copyBusiness() {
  if (!currentBusiness) return;
  const button = $("business-copy");
  button.disabled = true;
  setStatus("business-status", "Создаём копию…");
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/copy`);
  button.disabled = false;
  if (!r.ok) { setStatus("business-status", errorText(r), "err"); return; }
  currentBusiness = r.data;
  await loadBusinesses(r.data.id);
  setStatus("business-status", "");
  say("Копия создана вместе с фото и логотипом. Поменяйте название, адрес и районы и нажмите «Сохранить». Реклама, идеи и Метрика у копии свои.");
  window.scrollTo(0, 0);
}

async function deleteBusiness() {
  if (!currentBusiness) return;
  if (!(await askConfirm("Удалить бизнес?", `«${currentBusiness.name}» будет удалён вместе с загруженными файлами.`, "Удалить"))) return;
  await api("DELETE", `/api/businesses/${currentBusiness.id}`);
  currentBusiness = null;
  await loadBusinesses();
}

/* ---------- Креативы: тексты, картинки, видео ---------- */

