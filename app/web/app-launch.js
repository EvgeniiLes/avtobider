async function openLaunch() {
  if (!currentBusiness) return;
  const separate = launchSeparateTouched ? `&separate=${$("launch-separate").checked}` : "";
  const result = await api("GET", `/api/businesses/${currentBusiness.id}/launch/preview?ad_format=${$("launch-responsive").checked ? "responsive" : "text"}${separate}`);
  if (!result.ok) { say(errorText(result), "err"); $("launch-card").hidden = true; return; }
  const p = result.data;
  $("launch-separate").checked = p.separate || (launchSeparateTouched && $("launch-separate").checked && !p.responsive);
  $("launch-separate-box").hidden = p.responsive;   // у комбинаторного объявления в группе одно объявление, делить нечего
  launchAdding = p.adding;
  $("launch-go").textContent = launchAdding ? "Добавить в рекламу и отправить на проверку" : "Создать и отправить на проверку";
  $("launch-summary").textContent = launchAdding
    ? `Реклама уже идёт. Новые объявления встанут в последнюю группу, пока в ней есть место (до трёх), остальные пойдут в новую кампанию: бюджет на неё ${p.weekly} ₽ в неделю${p.groups ? `, новых кампаний ${p.groups}, всего ${p.total} ₽ в неделю` : ""}. Запросы и районы те же. ${p.variants.length ? `Будет добавлено: ${adsPhrase(p.variants.length)}.` : "Новых объявлений нет: все объявления из списка «Пойдут в рекламу» уже в рекламе. Добавьте новый вариант на вкладке «Объявления» или увеличьте число объявлений в показе."}`
    : `Кабинет: ${p.account}. Где показывать: ${p.city}${p.districts.length ? (p.by_district ? `, только районы: ${p.districts.join(", ")}` : `, районы в текстах и фразах: ${p.districts.join(", ")}`) : ""}. ${p.groups === 1 ? `Одна кампания: ${adsPhrase(p.count)} с бюджетом ${p.weekly} ₽ в неделю` : `Кампаний ${p.groups} ${p.group_names && p.group_names.length ? `по услугам (${p.group_names.join(", ")}): в каждой свои фразы и объявления` : (p.separate ? "(по одному объявлению в каждой, показы идут у всех)" : "(по группам объявлений, в группе до трёх)")}, бюджет у каждой свой, ${p.weekly} ₽ в неделю, всего ${p.total} ₽ в неделю: ${adsPhrase(p.count)}`}. Ведёт на: ${p.link}${p.tagged ? " (к ссылке добавятся метки utm: в Метрике будет видно, с какого объявления пришёл человек)" : ""}.`;
  const second = (p.extras && p.extras.title2) || {};
  $("launch-variants").replaceChildren(...p.variants.map((v) =>
    el("li", {}, el("div", { className: "t", textContent: v.title }), ...(second[v.title.toLowerCase()] ? [el("div", { className: "hint", textContent: `Второй заголовок: ${second[v.title.toLowerCase()]}` })] : []),
      el("div", { className: "hint", textContent: v.text }))));
  $("launch-warnings").replaceChildren(...p.warnings.map((w) => el("li", { textContent: w })));
  const queries = [
    p.audience ? `Кому показываем: ${p.audience.summary}` : "Профиль аудитории не составлен.",
    p.keywords.length ? `Ключевые фразы (${p.keywords.length}): ${p.keywords.join("; ")}.` : "Ключевых фраз нет, запросы подберёт автотаргетинг.",
    `Автотаргетинг: ${p.autotargeting.categories.join(", ")}. Бренды: ${p.autotargeting.brands.join(", ")}.`,
    `Минус-слова (${p.negatives.length}): ${p.negatives.join(", ")}.`,
    ...(p.extras && p.extras.callouts.length ? [`Уточнения под объявлением: ${p.extras.callouts.join("; ")}.`] : []),
    ...(p.extras && p.extras.display_path ? [`Отображаемая ссылка: ${p.extras.display_path}.`] : []),
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
  $("launch-go").disabled = !confirmed || (launchAdding && !p.variants.length);
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
  button.textContent = launchAdding ? "Добавляем…" : "Создаём рекламу…";
  const result = await api("POST", `/api/businesses/${currentBusiness.id}/launch`, { marking_confirmed: $("marking-ok").checked, ad_format: $("launch-responsive").checked ? "responsive" : "text", separate: $("launch-responsive").checked ? null : $("launch-separate").checked });
  button.disabled = false;
  button.textContent = launchAdding ? "Добавить в рекламу и отправить на проверку" : "Создать и отправить на проверку";
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

