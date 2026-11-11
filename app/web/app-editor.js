// Отправка готового видео в Директ к этому объявлению и что с ним сейчас
function videoDirect(v) {
  const d = v.direct;
  const box = el("div", { className: "ed-video-direct" });
  if (d.combo) { box.append(el("p", { className: "hint", textContent: "У комбинаторного объявления видео в Директ пока не отправляется." })); return box; }
  if (!d.can) { box.append(el("p", { className: "hint", textContent: "Объявление ещё не запущено в Директе: отправить видео можно после запуска." })); return box; }
  if (d.state === "attached") { box.append(el("p", { className: "hint", textContent: "Видео привязано к объявлению в Директе, его проверяет модерация." })); return box; }
  const waiting = d.state === "converting";
  if (waiting) box.append(el("p", { className: "hint", textContent: "Директ обрабатывает видео, это занимает несколько минут." }));
  else if (d.state === "error") box.append(el("p", { className: "hint", textContent: `Не получилось: ${d.note || "Директ не принял видео."}` }));
  const send = el("button", { type: "button", className: "small", textContent: waiting ? "Проверить" : d.state === "error" ? "Отправить заново" : "Отправить в Директ" });
  send.addEventListener("click", async () => {
    send.disabled = true;
    const r = await creativeCall("POST", `/${v.id}/direct`);
    if (!r.ok) { send.disabled = false; say(errorText(r), "err"); return; }
    say(r.data.message, r.data.state === "attached" ? "ok" : undefined);
    await loadCreatives();
  });
  box.append(send);
  return box;
}

// Оживление баннера в видео: идеи от ИИ под это объявление или своя строка, видео этого объявления ниже
function motionBlock(a, ready) {
  const box = el("div", { className: "ed-motion" }, el("h3", { textContent: "Оживить баннер в видео" }));
  if (!ready) {
    box.append(el("p", { className: "hint", textContent: "Видео делается из готового баннера: сначала нарисуйте его." }));
    return box;
  }
  let chosen = "";   // готовое английское описание движения выбранной идеи; своё пожелание сервис переведёт сам
  const input = el("input", { type: "text", maxLength: 300, placeholder: "Что должно двигаться (необязательно), например: пена стекает по кузову" });
  input.addEventListener("input", () => { chosen = ""; });
  const chips = el("div", { className: "chips" });
  const ideasLabel = `Подобрать идеи · ${usd(0.01)}`;
  const ideasBtn = el("button", { type: "button", className: "small secondary", textContent: ideasLabel });
  ideasBtn.addEventListener("click", async () => {
    ideasBtn.disabled = true; ideasBtn.textContent = "Подбираю…";
    const r = await creativeCall("POST", `/ads/${a.index}/motion-ideas`);
    ideasBtn.disabled = false; ideasBtn.textContent = ideasLabel;
    if (!r.ok) { say(errorText(r), "err"); return; }
    chips.replaceChildren(...r.data.ideas.map((idea) => {
      const c = el("button", { type: "button", className: "chip", textContent: idea.label });
      c.addEventListener("click", () => {
        input.value = idea.label; chosen = idea.prompt;
        for (const x of chips.children) x.classList.remove("sel");
        c.classList.add("sel");
      });
      return c;
    }));
    if (!r.data.ai) say("ИИ сейчас недоступен: показаны общие варианты.");
  });
  const makeLabel = `Сделать видео · ${usd(creativePrices.video)}`;
  const make = el("button", { type: "button", className: "small", textContent: makeLabel });
  make.addEventListener("click", async () => {
    if (!(await askConfirm("Сделать видео?", `Это платно: около ${usd(creativePrices.video)} с баланса генерации. Готовится 5–7 минут. Текст и логотип на баннере останутся без движения.`, "Сделать"))) return;
    make.disabled = true;
    const r = await creativeCall("POST", `/ads/${a.index}/video`, { prompt: chosen || input.value });
    make.disabled = false;
    if (!r.ok) { say(errorText(r), "err"); return; }
    say("Видео готовится, оно появится здесь.");
    await loadCreatives();
  });
  box.append(el("p", { className: "hint", textContent: "Выберите готовую идею, которую ИИ подбирает именно под этот баннер, или напишите своё. Если ничего не писать, будет плавное движение камеры." }),
    el("div", { className: "upload-row" }, ideasBtn), chips, input, el("p"), make);
  const videos = lastCreatives.filter((c) => c.kind === "video" && c.title === a.title && c.body === a.text).sort((x, y) => y.id - x.id);
  for (const v of videos) {
    const card = el("div", { className: "ed-video" });
    if (v.status === "working") card.append(el("div", { className: "spinner" }), el("span", { className: "hint", textContent: "Видео готовится: 5–7 минут" }));
    else if (v.status === "failed") card.append(el("span", { className: "hint", textContent: v.error || "Видео не получилось. Попробуйте ещё раз." }));
    else if (v.url) {
      card.append(el("video", { src: v.url, controls: true, muted: true, loop: true, playsInline: true, preload: "metadata" }));
      if (v.direct) card.append(videoDirect(v));
    }
    const drop = el("button", { type: "button", className: "link", textContent: "Удалить" });
    drop.addEventListener("click", async () => { await creativeCall("DELETE", `/${v.id}`); await loadCreatives(); });
    card.append(drop);
    box.append(card);
  }
  return box;
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
  else if (a.banner_url) preview.append(el("img", { src: a.banner_url, alt: a.title }));
  else if (a.ai && a.ai.status === "failed") preview.append(el("div", { textContent: a.ai.error || "Не получилось. Нарисуйте заново." }));
  else if (!working) preview.append(el("i", { className: "hint", textContent: "Баннера пока нет" }), el("div", { textContent: "Выберите фото и подачу справа, затем нажмите «Нарисовать баннер»." }));
  if (working) preview.append(el("div", { className: "busy-layer" }, el("div", { className: "spinner" }), el("div", { textContent: "Рисуем баннер… около минуты" })));
  const why = (a.ai && a.ai.error || "").replace(/[.\s]+$/, "");   // причина уже заканчивается точкой: двойную не ставим
  const acceptBtn = el("button", { type: "button", className: "small", textContent: "Всё равно использовать этот баннер" });
  acceptBtn.addEventListener("click", async () => {   // проверка бывает придирчивой: владелец сам решает, годится ли картинка
    acceptBtn.disabled = true;
    const r = await creativeCall("POST", `/ads/${a.index}/accept-rejected`);
    if (!r.ok) { acceptBtn.disabled = false; say(errorText(r), "err"); return; }
    say("Баннер принят на объявление.", "ok");
    await loadCreatives();
  });
  const failedNote = a.ai && a.ai.status === "failed" && (a.banner_url || rejected)
    ? [el("div", { className: "notice" }, el("p", { textContent: rejected
      ? `Сервис не принял этот баннер: ${why || "не прошёл проверку"}. Он показан, чтобы вы видели, что не так. На объявлении остаётся прежний, если он был.`
      : `Новый баннер не вышел: ${why || "попробуйте ещё раз"}. Пока показан прежний.` }), ...(rejected ? [acceptBtn] : []))] : [];
  const versions = lastCreatives.filter((c) => c.kind === "ai_banner" && c.title === a.title && c.body === a.text && (c.status === "draft" || c.status === "approved" || c.status === "failed") && c.url)
    .sort((x, y) => y.id - x.id).slice(0, 8);   // и не принятые проверкой: видно всё, что нарисовано
  const strip = el("div", { className: "versions" });
  if (versions.length > 1) {
    strip.append("Версии:");
    versions.forEach((v, i) => {
      const bad = v.status === "failed";
      const img = el("img", { src: v.url, alt: `Версия ${versions.length - i}`, title: bad ? `Не принят проверкой: ${v.error || "ошибка"}` : v.current ? "Сейчас на объявлении" : "Вернуть эту версию", className: v.current ? "cur" : bad ? "bad" : "" });
      if (!v.current && !bad) img.addEventListener("click", async () => {
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
  const hookIn = el("input", { type: "text", value: a.hook || "", maxLength: 60, placeholder: "Если пусто, на баннере будет заголовок" });   // короткий текст на самом баннере
  const saveBtn = el("button", { className: "small secondary", textContent: "Сохранить текст", hidden: true });
  const same = () => titleIn.value === a.title && textIn.value === a.text && hookIn.value === (a.hook || "");
  const changed = () => { saveBtn.hidden = same(); };
  titleIn.addEventListener("input", changed);
  textIn.addEventListener("input", changed);
  hookIn.addEventListener("input", changed);
  // Сохраняет правку заголовка и текста. Возвращает номер объявления после сохранения (он может сдвинуться), null при ошибке.
  // Все кнопки редактора (нарисовать, другая подача, другой текст) вызывают это первым делом: иначе несохранённая правка теряется, и рисуется прежний текст.
  const commitText = async () => {
    if (same()) return a.index;
    saveBtn.disabled = true;
    let id = a.creative_id;
    if (!id) {   // шаблонный текст сначала сохраняем как свой
      const added = await creativeCall("POST", "/texts", { title: a.title, body: a.text });
      if (!added.ok) { say(errorText(added), "err"); saveBtn.disabled = false; return null; }
      id = added.data.id;
    }
    const r = await creativeCall("PUT", `/${id}`, { title: titleIn.value, body: textIn.value, hook: hookIn.value, status: "approved" });
    if (!r.ok) { say(errorText(r), "err"); saveBtn.disabled = false; return null; }
    selectedAd = `${r.data.title}\u0000${r.data.body}`;
    await loadAds();
    const saved = lastAds.find((x) => adKey(x) === selectedAd);
    return saved ? saved.index : null;
  };
  saveBtn.addEventListener("click", async () => {
    if (await commitText() === null) return;
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
      const index = await commitText();
      if (index === null) return;
      const r = await creativeCall("PUT", `/ads/${index}/settings`, { asset_id: p.id });
      if (!r.ok) { say(errorText(r), "err"); return; }
      await reload();
    });
    photoBox.append(b);
  });
  if (lastPhotos.length) {   // «без фото»: сервис рисует всю сцену сам по описанию бизнеса
    const none = el("button", { type: "button", className: "photo-pick-item none" + (a.no_photo ? " sel" : ""), title: "Без фото: нарисовать с нуля" }, el("span", { textContent: "Без фото" }));
    none.addEventListener("click", async () => {
      if (a.no_photo) return;
      const index = await commitText();
      if (index === null) return;
      const r = await creativeCall("PUT", `/ads/${index}/settings`, { no_photo: true });
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
  const addChip = (row, label, selected, payload, wrap = false) => {
    const c = el("button", { type: "button", className: "chip" + (selected ? " sel" : "") + (wrap ? " wrap" : ""), textContent: label, title: label });
    c.addEventListener("click", async () => {
      if (selected) return;
      const index = await commitText();   // несохранённая правка текста не должна пропасть при смене подачи
      if (index === null) return;
      const r = await creativeCall("PUT", `/ads/${index}/settings`, payload);
      if (!r.ok) { say(errorText(r), "err"); return; }
      await reload();
    });
    row.append(c);
  };
  const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  addChip(refChips, "Без референса", !a.ref_id, { ref_id: 0 });   // сброс: баннер рисуется без образца (автоподбор приёма его не вернёт)
  lastStyles.refs.forEach((r) => addChip(refChips, cap(r.label), a.ref_id === r.id, { ref_id: r.id }, true));
  if (!scratch) lastStyles.looks.forEach((l) => addChip(lookChips, cap(l.label), !a.ref_id && a.look_no === l.no, { look: l.no }));
  const chips = el("div", {}, el("span", { className: "sub", textContent: scratch ? "По мотивам (идея из рекламы вашей ниши, без фото)" : "По мотивам (вдохновение из рекламы вашей ниши)" }), refChips,
    ...(scratch ? [] : [el("span", { className: "sub", textContent: "Обычное оформление" }), lookChips]));

  // действия
  const price = adPrice(a);
  const draw = el("button", { textContent: `${ready ? "Нарисовать заново" : "Нарисовать баннер"} · ${usd(price)}` });
  draw.disabled = working;
  draw.addEventListener("click", async () => {
    draw.disabled = true;
    const index = await commitText();   // правка заголовка, которую не успели сохранить, попадает на баннер
    if (index === null) { draw.disabled = false; return; }
    const r = await creativeCall("POST", `/ads/${index}/ai-banner`);
    if (!r.ok) { draw.disabled = false; say(errorText(r), "err"); return; }
    if (r.data.varied) say(`Пробуем другую подачу: ${r.data.varied}.`);   // повтор без правок берёт следующую подачу
    await loadCreatives();
  });
  const again = el("button", { className: "secondary", textContent: "Другой текст" });
  again.addEventListener("click", async () => {
    again.disabled = true; again.textContent = "Пишем…";
    const index = await commitText();
    if (index === null) { again.disabled = false; again.textContent = "Другой текст"; return; }
    const r = await creativeCall("POST", `/ads/${index}/rewrite`);
    if (!r.ok) { say(errorText(r), "err"); again.disabled = false; again.textContent = "Другой текст"; return; }
    selectedAd = `${r.data.title}\u0000${r.data.text}`;
    say(`Новый текст: «${r.data.title}». Баннер к нему нужно нарисовать заново.`);
    await loadCreatives();
  });
  // другие тексты к этому же заголовку: ИИ предлагает четыре, владелец выбирает лучший (текст встаёт в поле, дальше «Сохранить текст»)
  const optLabel = `Другие тексты к заголовку · ${usd(0.01)}`;
  const optBtn = el("button", { type: "button", className: "small secondary", textContent: optLabel });
  const optBox = el("div", { className: "chips text-options" });
  optBtn.addEventListener("click", async () => {
    optBtn.disabled = true; optBtn.textContent = "Пишем…";
    const r = await creativeCall("POST", `/ads/${a.index}/text-options`);
    optBtn.disabled = false; optBtn.textContent = optLabel;
    if (!r.ok) { say(errorText(r), "err"); return; }
    optBox.replaceChildren(...r.data.options.map((t) => {
      const c = el("button", { type: "button", className: "chip wrap", textContent: t });
      c.addEventListener("click", () => {
        textIn.value = t;
        textIn.dispatchEvent(new Event("input"));   // появится «Сохранить текст»
        for (const x of optBox.children) x.classList.remove("sel");
        c.classList.add("sel");
      });
      return c;
    }));
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
    el("div", {}, el("span", { className: "lab", textContent: "Заголовок объявления" }), titleIn, counterEl(titleIn, creativeLimits.title)),
    el("div", {}, el("span", { className: "lab", textContent: "Текст на баннере (коротко, 2-5 слов)" }), hookIn, counterEl(hookIn, 40)),
    el("div", {}, el("span", { className: "lab", textContent: "Текст объявления" }), textIn, counterEl(textIn, creativeLimits.text), saveBtn, optBtn, optBox),
    el("div", {}, el("span", { className: "lab", textContent: "Фото" }), photoBox),
    ...(a.no_photo ? [el("p", { className: "hint ed-note", textContent: "Баннер нарисуется с нуля по описанию бизнеса: сцену придумает арт-директор, ваши фото в нём не используются." })] : []),
    el("div", {}, el("span", { className: "lab", textContent: "Подача" }), chips,
      el("p", { className: "hint ed-note", textContent: "Подача сохраняется сразу, но баннер меняется после «Нарисовать». «Нарисовать заново» оставляет прежний референс: результат получается новым, а подача та же. Другой референс выберите сами." })));
  const box = el("div", {}, top, el("div", { className: "ed-grid" }, el("div", {}, preview, strip, ...failedNote, motionBlock(a, ready)), fields), actions);
  if (a.ai && a.ai.prompt) {   // промпт для тех, кто хочет править сам
    const area = el("textarea", { rows: 7, maxLength: 4000, value: a.ai.prompt });
    const go = el("button", { className: "small", textContent: "Нарисовать по этому промпту" });
    go.disabled = working;
    go.addEventListener("click", async () => {
      const prompt = titleIn.value !== a.title ? area.value.split(a.title).join(titleIn.value) : area.value;   // промпт ещё со старым заголовком: подставляем новый
      const index = await commitText();
      if (index === null) return;
      const r = await creativeCall("POST", `/ads/${index}/ai-banner`, { prompt });
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
    const pic = a.banner_url ? el("img", { className: "pic", src: a.banner_url, loading: "lazy", alt: "" }) : el("span", { className: "pic" });
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
  // шаг можно пропустить: без идей в рекламу идут готовые тексты, и кнопка говорит об этом, а не зовёт «дальше» с пустой страницы
  $("ideas-next").textContent = used ? "Дальше: баннеры" : ideasState.length ? "Дальше без выбора: возьмём готовые тексты" : "Пропустить: возьмём готовые тексты";
  $("ideas-count").textContent = ideasState.length
    ? `Взято ${used} из ${ideasState.length}. Бюджета хватает на ${lastCount || 0} объявлений.`
    : "";
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
      ...(b.hook ? [el("div", { className: "hint", textContent: `Текст на баннере: ${b.hook}` })] : []),
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
  if (!ideasState.length && caps_text_ai && !autoIdeas.has(currentBusiness.id) && (currentBusiness.description || "").trim().length >= 40) {
    autoIdeas.add(currentBusiness.id);   // один раз на бизнес: ИИ предлагает идеи сам, чтобы вместо шаблонов были настоящие тексты
    generateBriefs(true);
  }
}

async function generateBriefs(auto = false) {
  const button = $("briefs-gen");
  button.disabled = true;
  setStatus("briefs-status", auto === true ? "Пишем первые идеи… около полминуты" : "Придумываю идеи… около полминуты");
  const r = await api("POST", `/api/businesses/${currentBusiness.id}/briefs/generate`, { count: Number($("briefs-count").value) });
  button.disabled = false;
  setStatus("briefs-status", r.ok ? `Готово, идей: ${r.data.briefs.length}. Они отобраны и уже взяты в рекламу: уберите те, что не нравятся.` : errorText(r), r.ok ? "ok" : "err");
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
      if (c.url) tile.append(el("img", { src: c.url, alt: c.title, title: `Не принят проверкой: ${why}` }));   // забракованный баннер виден, чтобы можно было сравнить
      tile.append(el("div", { className: c.url ? "hint" : "wait", title: why, textContent: why.length > 150 ? `${why.slice(0, 147)}…` : why }));
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
  if (!r.ok) { setStatus("briefs-status", errorText(r), "err"); return; }
  $("cr-own-title").value = "";
  $("cr-own-body").value = "";
  setStatus("briefs-status", "Свой вариант добавлен и пойдёт в рекламу.", "ok");
  await loadCreatives();
}

/* ---------- Яндекс Метрика ---------- */

