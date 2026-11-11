function kpiBuckets() {
  if (kpi.mode === "days") return kpi.days.map((d) => ({ ...d, label: d.day.slice(8) + "." + d.day.slice(5, 7) }));
  const out = [];
  for (let end = kpi.days.length; end > 0; end -= 7) {   // недели считаем с конца, чтобы последняя была полной
    const part = kpi.days.slice(Math.max(0, end - 7), end);
    const sum = (k) => part.reduce((s, d) => s + (d[k] || 0), 0);
    out.unshift({ impressions: sum("impressions"), clicks: sum("clicks"), spend: Math.round(sum("spend") * 100) / 100, leads: sum("leads"),
                  label: `${part[0].day.slice(8)}.${part[0].day.slice(5, 7)}–${part[part.length - 1].day.slice(8)}.${part[part.length - 1].day.slice(5, 7)}` });
  }
  return out;
}

function kpiTotal(metric) {
  const all = kpi.days.reduce((s, d) => ({ impressions: s.impressions + d.impressions, clicks: s.clicks + d.clicks, spend: s.spend + d.spend, leads: s.leads + d.leads }),
                              { impressions: 0, clicks: 0, spend: 0, leads: 0 });
  return KPI_METRICS[metric].value(all);
}

function renderKpiSlots() {
  const box = $("kpi-slots");
  box.replaceChildren(...kpi.slots.map((slot, i) => {
    const check = el("input", { type: "checkbox", checked: slot.on, ariaLabel: "Показывать линию" });
    check.addEventListener("change", () => { slot.on = check.checked; saveKpi(); renderKpiChart(); });
    const pick = el("select", { ariaLabel: "Показатель" }, ...Object.entries(KPI_METRICS).map(([key, m]) => el("option", { value: key, textContent: m.label, selected: key === slot.metric })));
    pick.addEventListener("change", () => { slot.metric = pick.value; saveKpi(); renderKpiSlots(); renderKpiChart(); });
    const total = kpiTotal(slot.metric);
    return el("div", { className: `kpi-slot s${i + 1}` }, el("label", { className: "kpi-check" }, check, el("i")), pick,
              el("b", { textContent: total == null ? "—" : KPI_METRICS[slot.metric].fmt(total) }));
  }));
}

function saveKpi() {
  try { localStorage.setItem("kpi-slots", JSON.stringify(kpi.slots)); } catch (e) { /* не страшно: выбор просто не запомнится */ }
}

function renderKpiChart() {
  const svg = $("kpi-chart");
  svg.replaceChildren();
  const buckets = kpiBuckets();
  if (!buckets.length) return;
  const W = 640, H = 260, L = 12, R = 12, T = 12, B = 28;
  const slot = (W - L - R) / buckets.length;
  for (let i = 0; i <= 3; i++) {
    const y = T + ((H - T - B) * i) / 3;
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid-line" }));
  }
  const step = Math.ceil(buckets.length / 7);
  buckets.forEach((b, i) => {
    if (i % step) return;
    const t = svgEl("text", { x: L + i * slot + slot / 2, y: H - 8, "text-anchor": "middle", class: "axis" });
    t.textContent = b.label;
    svg.append(t);
  });
  kpi.slots.forEach((s, n) => {
    if (!s.on) return;
    const metric = KPI_METRICS[s.metric];
    const values = buckets.map((b) => metric.value(b));
    const max = Math.max(...values.map((v) => v || 0), 1e-9);
    const points = values.map((v, i) => (v == null ? null : [L + i * slot + slot / 2, H - B - ((H - T - B) * v) / max]));
    const drawn = points.filter(Boolean);
    if (drawn.length > 1) svg.append(svgEl("polyline", { points: drawn.map((p) => p.join(",")).join(" "), class: `kpi-line s${n + 1}` }));
    for (const p of drawn) svg.append(svgEl("circle", { cx: p[0], cy: p[1], r: 3.5, class: `kpi-dot s${n + 1}` }));
  });
  buckets.forEach((b, i) => {   // подсказка: наведите на день, и видны значения всех включённых линий
    const lines = kpi.slots.filter((s) => s.on).map((s) => { const v = KPI_METRICS[s.metric].value(b); return `${KPI_METRICS[s.metric].label}: ${v == null ? "—" : KPI_METRICS[s.metric].fmt(v)}`; });
    const zone = svgEl("rect", { x: L + i * slot, y: T, width: slot, height: H - T - B, class: "kpi-zone" });
    const tip = svgEl("title", {});
    tip.textContent = `${b.label}\n${lines.join("\n")}`;
    zone.append(tip);
    svg.append(zone);
  });
}

function renderKpi(days) {
  kpi.days = days;
  $("kpi-card").hidden = !days.length;
  if ($("kpi-card").hidden) return;
  for (const b of document.querySelectorAll("[data-kpi-mode]")) b.classList.toggle("sel", b.dataset.kpiMode === kpi.mode);
  renderKpiSlots();
  renderKpiChart();
}

for (const b of document.querySelectorAll("[data-kpi-mode]")) b.addEventListener("click", () => { kpi.mode = b.dataset.kpiMode; renderKpi(kpi.days); });

async function loadKpi() {
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/series?days=28`);
  renderKpi(result.ok ? result.data.days : []);
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

// Кампании и объявления на главной: по каждой кампании расход и по каждому объявлению расход, переходы и цена клика. Нет рекламы: подсказка
function renderHomeAds(data) {
  const list = (data && data.campaigns) || [];
  $("home-ads-empty").hidden = list.length > 0;
  $("home-ads-more").hidden = list.length === 0;
  const box = $("home-ads");
  box.replaceChildren(...list.map((g, i) => {
    const head = el("div", { className: "home-camp-head" },
      el("strong", { textContent: g.intent || (list.length > 1 ? `Группа ${i + 1}` : "Кампания") }),
      el("span", { className: `badge ${g.state}`, textContent: g.state_label }),
      el("span", { className: "hint", textContent: `Показов ${g.impressions || 0} · расход ${money(g.spend)} · обращений ${g.leads}${g.weekly ? ` · бюджет ${money(g.weekly)} в неделю` : ""}` }));
    const rows = (g.units || []).map((u) => el("li", {},
      el("span", { className: "home-ad-title", textContent: u.title }),
      el("span", { className: `badge ${u.verdict_code}`, textContent: u.verdict }),
      el("span", { className: "hint", textContent: `показов ${u.impressions || 0} · переходов ${u.clicks} · расход ${money(u.spend)} · CTR ${ctrText(u.clicks, u.impressions)} · клик ${cents(u.cost_per_click)}` })));
    return el("section", { className: "home-camp" }, head, el("ul", { className: "home-ads-list" }, ...rows));
  }));
}

async function loadStats() {
  const has = !!currentBusiness;
  $("stats-empty").hidden = has;
  $("stats-body").hidden = !has;
  if (!has) {
    renderHomeAds(null);
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
  updateNav(true, !!(data.real || data.example));
  renderHomeAds(data);
  renderChecklist(data.checklist || []);
  renderHome(data);
  renderGoal(data);
  if (data.real || data.example) { loadSeries(); loadKpi(); } else { renderChart([]); renderKpi([]); }
  if (data.real || data.example) loadInsights(); else $("insights-card").hidden = true;
  loadLeads();
  if (data.real) { loadReport(); loadVisitorGoals(); loadQueries(); loadSlices(); loadBehavior(); } else { $("behavior-card").hidden = true; $("report-card").hidden = true; $("goals-card").hidden = true; $("queries-card").hidden = true; $("slices-card").hidden = true; }
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
  notices.replaceChildren(...(data.notices || []).map((n) => el("li", {}, ...withLinks(n))));

  // Блок статистики стоит в сетке главной рядом с «Нашими работами» и «Идеями для рекламы»; пока рекламы нет, в нём прочерки
  const tiles = $("tiles");
  tiles.hidden = false;
  $("stats-tile").hidden = false;
  $("hero-tiles").classList.toggle("nodata", !data.real && !data.example);
  tiles.replaceChildren();
  if (data.totals) {
    const t = data.totals;
    $("stats-period").textContent = `За ${t.days} ${plural(t.days, "день", "дня", "дней")}`;
    tiles.append(
      tile(String(t.impressions || 0), "Показы"),
      tile(String(t.clicks || 0), "Переходы"),
      tile(ctrText(t.clicks, t.impressions), "CTR"),
      tile(t.clicks ? cents(t.spend / t.clicks) : "—", "Цена клика"),
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
  $("campaign-groups").replaceChildren(...(data.campaigns || []).map((g, i) => campaignGroup(g, (data.campaigns || []).length > 1, i + 1)));
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

// Подробности объявления: как оно выглядит, на какие фразы показывается, куда ведёт
function adDetails(d) {
  const block = (title, ...content) => el("div", { className: "ad-block" }, el("h4", { textContent: title }), ...content);
  const list = (items) => el("ul", {}, ...items.map((t) => el("li", { textContent: t })));
  const box = el("div", { className: "ad-detail-box" });
  if (d.pictures.length) box.append(el("div", { className: "ad-pics" }, ...d.pictures.map((p) => el("img", { src: p.url, alt: `Баннер «${p.title}»`, loading: "lazy" }))));
  box.append(block(d.responsive ? "Заголовки (Яндекс подбирает сочетания)" : "Заголовок", list(d.titles)));
  if (d.second_titles.length) box.append(block("Второй заголовок", list(d.second_titles)));
  box.append(block(d.responsive ? "Тексты" : "Текст", list(d.texts)));
  if (d.callouts.length) box.append(block("Уточнения под объявлением", el("p", { textContent: d.callouts.join(" · ") })));
  const where = [d.intent ? `Услуга: ${d.intent}` : "", d.campaign ? `Кампания: ${d.campaign}` : "", `Статус: ${d.state}`].filter(Boolean).join(" · ");
  box.append(block("Где показывается", el("p", { textContent: where }), ...(d.note ? [el("p", { className: "hint" }, ...withLinks(d.note))] : [])));
  box.append(block(`Ключевые фразы (${d.keywords.length})${d.keywords_live ? "" : ", из профиля"}`,
    d.keywords.length ? el("p", { textContent: d.keywords.join("; ") }) : el("p", { className: "hint", textContent: "Фраз нет: запросы подбирает автотаргетинг." })));
  box.append(block(`Минус-слова (${d.negatives.length})`, el("p", { textContent: d.negatives.join(", ") })));
  if (d.link) box.append(block("Ведёт на", el("p", { className: "ad-link", textContent: d.link + (d.display_path ? `  (показывается как …/${d.display_path})` : "") })));
  return box;
}

function campaignGroup(g, several, number) {
  const started = g.created_day ? new Date(g.created_day).toLocaleDateString("ru-RU") : "";
  const what = started ? `Запуск от ${started}` : "Кампания";
  const title = el("h3", { textContent: g.intent ? `${g.intent}: ${what.toLowerCase()}` : several ? `Группа ${number}: ${what.toLowerCase()}` : what });
  const head = el("div", { className: "camp-head" }, title, el("span", { className: `badge ${g.state}`, textContent: g.state_label }));
  const group = el("section", { className: "camp-group" }, head);
  if (several) head.append(el("span", { className: "camp-total", textContent: `Показов ${g.impressions || 0} · расход ${money(g.spend)} · обращений ${g.leads}` }));
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
    if (g.splittable) {
      const split = el("button", { type: "button", className: "small secondary", textContent: "Создать A/B-тест: каждое объявление в своей кампании" });
      split.addEventListener("click", async () => {
        if (!confirm("Каждое работающее объявление получит свою кампанию, лимит этой кампании поделится между ними поровну, а сама она остановится. "
                     + "Новые объявления уйдут на проверку в Яндекс. Через несколько дней сервис сравнит их и оставит лучшее. Продолжить?")) return;
        split.disabled = true;
        const result = await api("POST", `/api/businesses/${currentBusiness.id}/launch/split`, { campaign_id: g.id });
        split.disabled = false;
        if (!result.ok) { say(errorText(result), "err"); return; }
        say(result.data.message);
        await loadStats();
      });
      group.append(el("div", { className: "camp-budget" }, split, el("span", { className: "hint", textContent: "В одной группе Директ почти все показы отдаёт одному объявлению." })));
    }
  }

  const shared = g.editable;   // в общей кампании у объявления своего бюджета нет
  const cells = (names) => names.map((n) => el("th", { textContent: n }));
  const head2 = el("tr", {}, ...cells(["Объявление", "", "Оценка", ...(shared ? [] : ["Бюджет в неделю"]), "Показы", "Расход", "Переходы", "CTR", "Цена клика", "Обращения", "Клиенты", "Цена клиента"]));
  const body = el("tbody");
  for (const u of g.units) {
    const name = el("td", { className: "ad-name", textContent: u.title });
    if (u.note) name.append(el("span", { className: "sub" }, ...withLinks(u.note)));
    if (u.kind === "responsive" && u.variants) name.append(el("span", { className: "sub", textContent: `Комбинаторное объявление. Заголовки: ${u.variants.titles.join(" / ")}. Тексты: ${u.variants.texts.join(" / ")}` }));
    if (u.in_test) name.append(el("span", { className: "sub", textContent: "Идёт сравнение двух вариантов" }));
    else if (u.variant) name.append(el("span", { className: "sub", textContent: "Второй вариант для сравнения" }));
    // на телефоне строка превращается в карточку: подпись берётся из data-label, редкие числа спрятаны под «Подробнее»
    const cell = (v, label, more = false) => {
      const td = el("td", { textContent: v, className: more ? "ad-more" : "" });
      td.setAttribute("data-label", label);
      return td;
    };
    const badge = el("span", { className: `badge ${u.verdict_code}`, textContent: u.verdict });
    const pauseCell = el("td", { className: "ad-pause" });
    if (u.can_pause || u.can_resume) {
      const button = el("button", { type: "button", className: "small secondary", textContent: u.can_resume ? "Включить" : "Пауза" });
      button.addEventListener("click", async () => {
        button.disabled = true;
        const result = await api("PUT", `/api/businesses/${currentBusiness.id}/units/${u.id}/pause`, { paused: !u.can_resume });
        button.disabled = false;
        if (!result.ok) { say(errorText(result), "err"); return; }
        say(u.can_resume ? `«${u.title}» включено.` : `«${u.title}» на паузе.`);
        await loadStats();
      });
      pauseCell.append(button);
    }
    if (shared && u.active && u.kind !== "responsive") {   // A/B: второй вариант с тем же смыслом, другими словами и новым баннером; баннер рисуют в редакторе
      const ab = el("button", { type: "button", className: "small secondary", textContent: "Создать A/B", title: "Новый вариант с тем же смыслом: другой текст и баннер" });
      ab.addEventListener("click", async (event) => {
        event.stopPropagation();
        ab.disabled = true; ab.textContent = "Пишем…";
        const result = await creativeCall("POST", "/ads/ab-copy", { unit_id: u.id });
        ab.disabled = false; ab.textContent = "Создать A/B";
        if (!result.ok) { say(errorText(result), "err"); return; }
        say(`Новый вариант: «${result.data.title}». Отредактируйте его, нарисуйте баннер и запустите A/B.`);
        showPage("ab");
      });
      pauseCell.append(ab);
    }
    const verdict = el("td", {}, badge);
    verdict.setAttribute("data-label", "Оценка");
    const row = el("tr", { className: u.active ? "" : "off" }, name, pauseCell, verdict, ...(shared ? [] : [cell(money(u.weekly_budget), "Бюджет в неделю", true)]),
      cell(String(u.impressions || 0), "Показы"), cell(money(u.spend), "Расход"), cell(String(u.clicks), "Переходы"), cell(ctrText(u.clicks, u.impressions), "CTR"), cell(cents(u.cost_per_click), "Цена клика"),
      cell(String(u.leads), "Обращения", true), cell(String(u.clients), "Клиенты", true), cell(money(u.cost_per_client), "Цена клиента", true));
    const toggle = el("td", { className: "ad-toggle" }, el("button", { type: "button", className: "small secondary", textContent: "Подробнее" }));
    toggle.firstChild.addEventListener("click", () => {
      const open = row.classList.toggle("open");
      toggle.firstChild.textContent = open ? "Скрыть" : "Подробнее";
    });
    row.append(toggle);
    // вся строка объявления открывает подробности (кнопки и ссылки внутри работают как раньше)
    let detailRow = null, loading = false;
    row.classList.add("clickable");
    row.tabIndex = 0;
    row.setAttribute("aria-expanded", "false");
    const toggleDetails = async () => {
      if (detailRow) { detailRow.remove(); detailRow = null; row.classList.remove("expanded"); row.setAttribute("aria-expanded", "false"); return; }
      if (loading) return;
      loading = true;
      const result = await api("GET", `/api/businesses/${currentBusiness.id}/units/${u.id}/details`);
      loading = false;
      if (!result.ok) { say(errorText(result), "err"); return; }
      detailRow = el("tr", { className: "ad-details" }, el("td", { colSpan: 12 }, adDetails(result.data)));
      row.after(detailRow);
      row.classList.add("expanded");
      row.setAttribute("aria-expanded", "true");
    };
    row.addEventListener("click", (e) => { if (!e.target.closest("button, a, input")) toggleDetails(); });
    row.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === row) { e.preventDefault(); toggleDetails(); } });
    body.append(row);
  }
  group.append(el("div", { className: "table-wrap" }, el("table", {}, el("thead", {}, head2), body)));
  return group;
}

let launchAdding = false;   // реклама уже идёт: кнопка запуска добавляет в неё новые объявления, а не создаёт кампанию

let launchSeparateTouched = false;   // владелец сам переключил «каждое объявление в своей кампании»: до этого берём то, что сохранено у бизнеса

