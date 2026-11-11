let creativeTimer = null;
let creativeLimits = { title: 56, text: 81 };
const autoIdeas = new Set();
let creativePrices = { image: 0.08, video: 0.7, strategist: 0.03 };
let usdRub = 95;   // курс приходит с сервера: генерация оплачивается в долларах, а человеку цену показываем в рублях
const usd = (v) => {   // имя прежнее, но возвращает рубли
  const rub = v * usdRub;
  return rub < 1 ? "меньше 1 ₽" : `${Math.round(rub)} ₽`;
};

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
  if (!result.ok) { setStatus("briefs-status", errorText(result), "err"); return; }
  const { creatives, caps, spend, limits, prices_usd, usd_rub } = result.data;
  creativeLimits = limits;
  if (prices_usd) creativePrices = prices_usd;
  if (usd_rub) usdRub = usd_rub;
  $("briefs-gen").textContent = `Предложить идеи · ${usd(creativePrices.strategist)}`;

  $("cr-image-note").hidden = caps.image_ai;
  $("cr-image-note").textContent = "Создание картинок и видео пока не подключено на сервере. Свои фото можно загрузить выше, в этом же разделе.";
  $("cr-image-form").hidden = !caps.image_ai;
  $("cr-spend").textContent = caps.image_ai || caps.text_ai
    ? `Потрачено на создание в этом месяце: ${usd(spend.spent_usd)} из ${usd(spend.limit_usd)}, всего с начала: ${usd(spend.total_usd ?? spend.spent_usd)}. Больше лимита сервис не потратит, 1-го числа он обнуляется. Сумма пересчитана по курсу ${usdRub} ₽ за доллар, это примерно.` : "";

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
  if (creatives.some((c) => c.status === "working")) creativeTimer = setTimeout(loadCreatives, 6000);
}

// ---------- Страница «Объявления (тест)»: одно объявление рисуют три нейросети ----------
const TEST_MODELS = [{ id: "openai/gpt-image-2/edit", label: "GPT Image 2" }, { id: "openai/gpt-image-2.6/sunburst/edit", label: "GPT Image 2.6" }, { id: "google/nano-banana-2.1/edit", label: "Nano Banana 2.1" }];
const TEST_MODELS_FREE = [{ id: "openai/gpt-image-2", label: "GPT Image 2" }, { id: "openai/gpt-image-2.6/sunburst/text-to-image", label: "GPT Image 2.6" }, { id: "google/nano-banana-2.1", label: "Nano Banana 2.1" }];
let testTimer = null, testAdKey = null, testAds = [], testRows = [], testPhotos = [], testStyles = { refs: [] };

async function loadTestPage() {
  clearTimeout(testTimer);
  const has = !!currentBusiness;
  $("test-empty").hidden = has;
  $("test-body").hidden = !has;
  if (!has) return;
  const [ads, list] = await Promise.all([creativeCall("GET", "/ads"), creativeCall("GET", "")]);
  if (!ads.ok || !list.ok) { say(errorText(ads.ok ? list : ads), "err"); return; }
  testAds = ads.data.ads;
  testPhotos = ads.data.photos || [];
  testStyles = ads.data.styles || { refs: [] };
  testRows = list.data.creatives.filter((c) => c.kind === "test_banner");
  if (!testAdKey || !testAds.some((a) => adKey(a) === testAdKey)) testAdKey = testAds.length ? adKey(testAds[0]) : null;
  renderTestPage();
  if (testRows.some((c) => c.status === "working")) testTimer = setTimeout(loadTestPage, 6000);
}

function renderTestPage() {
  $("test-ads").replaceChildren(...testAds.map((a) => {
    const rows = testRows.filter((c) => c.title === a.title && c.body === a.text);
    const item = el("button", { className: "ad-item" + (adKey(a) === testAdKey ? " sel" : ""), type: "button" },
      a.banner_url ? el("img", { className: "pic", src: a.banner_url, loading: "lazy", alt: "" }) : el("span", { className: "pic", textContent: "нет" }),
      el("span", {}, el("span", { className: "t", textContent: a.title }), el("span", { className: "st", textContent: rows.length ? `Тестов: ${rows.length}` : "Не тестировали" })));
    item.addEventListener("click", () => { testAdKey = adKey(a); renderTestPage(); });
    return item;
  }));
  const panel = $("test-panel");
  const a = testAds.find((x) => adKey(x) === testAdKey);
  if (!a) { panel.replaceChildren(el("p", { className: "hint", textContent: "Объявлений пока нет." })); return; }
  const mineAll = testRows.filter((c) => c.title === a.title && c.body === a.text);
  const working = mineAll.some((c) => c.status === "working");
  const setup = [a.photo_label && !a.no_photo ? `Фото: ${a.photo_label}` : "Без фото", a.ref_id ? `Референс №${a.ref_id}` : "Без референса (оформление по умолчанию)"].join(" · ");
  // один блок схемы: кнопка и три карточки моделей; схемы две (прямая и «умная»), строки теста помечены схемой
  const schemeBlock = (scheme, title, hint, extraCents, models = TEST_MODELS) => {
    const mine = mineAll.filter((c) => (c.meta.scheme || "direct") === scheme);
    const price = usd((creativePrices.ai_banner || 0.07) * models.length + extraCents / 100);
    const run = el("button", { textContent: `${mine.length ? "Нарисовать заново" : "Нарисовать на трёх моделях"} · ${price}` });
    run.disabled = working;
    run.addEventListener("click", async () => {
      run.disabled = true;
      const r = await creativeCall("POST", `/ads/${a.index}/model-test`, { scheme });
      if (!r.ok) { run.disabled = false; say(errorText(r), "err"); return; }
      say("Три модели рисуют одно и то же объявление, это займёт около минуты.");
      await loadTestPage();
    });
    const newest = (mine.slice().sort((x, y) => y.id - x.id)[0] || {}).meta;
    const lastRun = newest ? newest.run : "";
    const placement = newest && newest.placement ? [el("p", { className: "hint", textContent: `Арт-директор написал, как вписать фото: ${newest.placement}` })]
      : newest && newest.layout ? [el("p", { className: "hint", textContent: `Композиция: ${newest.layout}. Сцена от арт-директора: ${newest.scene || "—"}` })] : [];
    const cards = models.map((m) => {
      const c = mine.filter((x) => x.meta.model === m.id && (!lastRun || x.meta.run === lastRun)).sort((x, y) => y.id - x.id)[0];
      const card = el("div", { className: "test-card" }, el("h4", { textContent: m.label }));
      if (!c) { card.append(el("p", { className: "hint", textContent: "Ещё не рисовали." })); return card; }
      if (c.status === "working") { card.append(el("div", { className: "wait" }, el("div", { className: "spinner" }), el("div", { textContent: "Рисуем… около минуты" }))); return card; }
      if (c.status === "failed") { card.append(el("p", { className: "notice", textContent: c.error || "Не получилось." })); return card; }
      card.append(el("img", { src: c.url, alt: `${m.label}: ${a.title}`, loading: "lazy" }));
      const chk = c.meta.check || {};
      const notes = [];
      notes.push(chk.missing && chk.missing.length ? `Ошибки в тексте: ${chk.missing.join(", ")}` : (chk.read ? "Текст без ошибок" : "Текст не проверен"));
      if (chk.extra && chk.extra.length) notes.push(`Лишний текст: ${chk.extra.join(", ")}`);
      if (c.meta.score) notes.push(`Оценка проверяющего: ${c.meta.score} из 5`);
      if (c.meta.issues && c.meta.issues.length) notes.push(c.meta.issues.slice(0, 3).join("; "));
      card.append(...notes.map((t) => el("p", { className: "hint", textContent: t })));
      const use = el("button", { className: "small secondary", textContent: "Взять на объявление" });
      use.addEventListener("click", async () => {
        use.disabled = true;
        const r = await creativeCall("POST", `/${c.id}/use-test`);
        say(r.ok ? `Баннер «${m.label}» стоит на объявлении. Прежний остался в версиях.` : errorText(r), r.ok ? "ok" : "err");
        await loadTestPage();
      });
      card.append(use);
      return card;
    });
    return el("section", { className: "test-scheme" }, el("h3", { textContent: title }), el("p", { className: "hint", textContent: hint }), run, ...placement, el("div", { className: "test-grid" }, ...cards));
  };
  // настройки объявления: фото (включить, выключить, выбрать другое) и референс; они общие для всех трёх моделей и те же, что в редакторе баннера
  const change = async (payload) => {
    const r = await creativeCall("PUT", `/ads/${a.index}/settings`, payload);
    if (!r.ok) { say(errorText(r), "err"); return; }
    await loadTestPage();
  };
  const photoBox = el("div", { className: "photo-pick" });
  testPhotos.forEach((p) => {
    const b = el("button", { type: "button", className: "photo-pick-item" + (p.id === a.photo_id && !a.no_photo ? " sel" : ""), title: p.label }, el("img", { src: p.url, alt: p.label }));
    b.addEventListener("click", () => { if (p.id !== a.photo_id || a.no_photo) change({ asset_id: p.id }); });
    photoBox.append(b);
  });
  const none = el("button", { type: "button", className: "photo-pick-item none" + (a.no_photo || !testPhotos.length ? " sel" : ""), title: "Без фото: нарисовать с нуля" }, el("span", { textContent: "Без фото" }));
  none.addEventListener("click", () => { if (!a.no_photo && testPhotos.length) change({ no_photo: true }); });
  photoBox.append(none);
  const refBox = el("div", { className: "chips" });
  const refChip = (label, selected, payload) => {
    const c = el("button", { type: "button", className: "chip wrap" + (selected ? " sel" : ""), textContent: label, title: label });
    c.addEventListener("click", () => { if (!selected) change(payload); });
    refBox.append(c);
  };
  refChip("Без референса", !a.ref_id, { ref_id: 0 });
  testStyles.refs.forEach((r) => refChip(`№${r.id}: ${r.label.charAt(0).toUpperCase()}${r.label.slice(1)}`, a.ref_id === r.id, { ref_id: r.id }));
  panel.replaceChildren(
    el("h3", { textContent: a.title }), el("p", { textContent: a.text }),
    el("span", { className: "sub", textContent: "Фото на баннере (включить, выключить или выбрать другое)" }), photoBox,
    el("span", { className: "sub", textContent: "Референс" }), refBox,
    el("p", { className: "hint", textContent: `Одинаковые условия для всех моделей: ${setup}. Задание у моделей одно и то же. С фото модель строит баннер вокруг него, без фото рисует сцену с нуля по референсу.` }),
    schemeBlock("direct", "Схема 1: фото и референс прямо в задание", "Модель получает ваше фото и референс файлами и задание с описанием раскладки референса словами.", 0),
    schemeBlock("smart", "Схема 2: «умная», арт-директор вписывает фото", "Сначала арт-директор (нейросеть со зрением) смотрит на фото и референс вместе и пишет, как вписать фото в раскладку референса. Эти инструкции добавляются к заданию. Нужны фото и референс.", 3),
    schemeBlock("free", "Схема 3: без референса и без фото", "Фото и референс не используются. Арт-директор выбирает одну из проверенных композиций (огромное слово, предмет-герой, цветная плашка, игра образом и другие), берёт цвета вашего бизнеса и придумывает сцену под неё. Модели рисуют из текста.", 0, TEST_MODELS_FREE));
}

