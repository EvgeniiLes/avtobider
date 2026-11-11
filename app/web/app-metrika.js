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

async function addMissingGoals() {
  const button = $("m-goals-add");
  if (!$("m-counter").value) { setStatus("metrika-status", "Сначала выберите счётчик.", "err"); return; }
  button.disabled = true;
  setStatus("metrika-status", "Добавляем цели в вашей Метрике…", "");
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/metrika/counters/${$("m-counter").value}/goals/auto`);
  button.disabled = false;
  if (!result.ok) { setStatus("metrika-status", errorText(result), "err"); return; }
  const d = result.data;
  await loadGoals($("m-goal").value);
  setStatus("metrika-status", `${d.added.length ? `Добавлено: ${d.added.join(", ")}.` : "Ничего нового не добавлено."}${d.present.length ? ` Уже были: ${d.present.join(", ")}.` : ""}${d.skipped.length ? ` Метрика не приняла: ${d.skipped.join(", ")}.` : ""}`, d.skipped.length ? "err" : "ok");
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
const cents = (v) => (v == null ? "—" : `${v.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽`);   // цена клика в рублях и копейках: «2,41 ₽», округление до рубля её исказило бы
const ctrText = (clicks, impressions) => (impressions ? `${((clicks * 100) / impressions).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} %` : "—");

