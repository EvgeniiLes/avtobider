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
    title.textContent = t && t.leads ? `${t.leads} ${plural(t.leads, "обращение", "обращения", "обращений")} за ${t.days} дней` : t && t.impressions ? `Показов за ${t.days} дней: ${t.impressions}` : "Реклама запущена";
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
  $("tg-card").hidden = !s.configured;   // бот не заведён: владельцу подключать нечего, а инструкция для администратора в docs/deployment.md
  $("tg-note").hidden = true;
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

async function loadBehavior() {   // поведение посетителей по объявлениям и фразам (Метрика)
  const card = $("behavior-card");
  const r = await api("GET", `/api/businesses/${currentBusiness.id}/slices/behavior`);
  if (!r.ok) { card.hidden = true; return; }
  const d = r.data;
  card.hidden = false;
  const empty = $("behavior-empty");
  empty.hidden = d.ads.length > 0;
  empty.textContent = !d.has_counter ? "Подключите счётчик Метрики на странице «Бизнес»: тогда здесь будет видно, что делают люди после клика."
    : d.synced ? "Данных пока нет: переходов с меток объявлений ещё не было." : "Данные появятся после ближайшего обновления (раз в день).";
  $("behavior-table").hidden = d.ads.length === 0;
  const secs = (n) => (n >= 60 ? `${Math.floor(n / 60)} мин ${n % 60} с` : `${n} с`);
  $("behavior-ads").replaceChildren(...d.ads.map((a) => el("tr", {}, el("td", { textContent: a.title }), el("td", { textContent: String(a.visits) }),
    el("td", { textContent: `${Math.round(a.bounce)}%` }), el("td", { textContent: String(a.depth) }), el("td", { textContent: secs(a.seconds) }),
    el("td", {}, el("span", { className: `badge ${a.verdict_code}`, textContent: a.verdict })))));
  $("behavior-terms-box").hidden = d.terms.length === 0;
  $("behavior-terms").replaceChildren(...d.terms.map((t) => el("tr", {}, el("td", { textContent: t.term }), el("td", { textContent: String(t.visits) }),
    el("td", { textContent: `${Math.round(t.bounce)}%` }), el("td", { textContent: secs(t.seconds) }),
    el("td", {}, el("span", { className: `badge ${t.verdict_code}`, textContent: t.verdict })))));
}

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

// «Ключевые показатели» на странице «Результаты»: до четырёх линий, у каждой показатель выбирается из списка, шаг дни или недели.
const KPI_METRICS = {
  impressions: { label: "Показы", value: (b) => b.impressions, fmt: (v) => Math.round(v).toLocaleString("ru-RU") },
  clicks: { label: "Клики", value: (b) => b.clicks, fmt: (v) => Math.round(v).toLocaleString("ru-RU") },
  spend: { label: "Расходы, ₽", value: (b) => b.spend, fmt: (v) => `${v.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽` },
  leads: { label: "Обращения", value: (b) => b.leads, fmt: (v) => Math.round(v).toLocaleString("ru-RU") },
  cpc: { label: "Цена клика, ₽", value: (b) => (b.clicks ? b.spend / b.clicks : null), fmt: (v) => `${v.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽` },
  cpl: { label: "Цена обращения, ₽", value: (b) => (b.leads ? b.spend / b.leads : null), fmt: (v) => `${Math.round(v).toLocaleString("ru-RU")} ₽` },
  ctr: { label: "CTR, %", value: (b) => (b.impressions ? (b.clicks * 100) / b.impressions : null), fmt: (v) => `${v.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} %` },
};
const KPI_DEFAULT = ["clicks", "leads", "spend", "impressions"];
const kpi = { days: [], mode: "days", slots: KPI_DEFAULT.map((metric) => ({ metric, on: true })) };

try {
  const saved = JSON.parse(localStorage.getItem("kpi-slots") || "null");
  if (Array.isArray(saved) && saved.length === 4 && saved.every((s) => KPI_METRICS[s.metric])) kpi.slots = saved.map((s) => ({ metric: s.metric, on: s.on !== false }));
} catch (e) { /* без сохранённого выбора работает с настройками по умолчанию */ }

