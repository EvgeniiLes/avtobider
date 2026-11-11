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
  const autoWhy = $("aud-auto-why");   // сервис подбирает категории сам: объясняем выбор и показываем, если Директ не принял изменение
  const pr = data.profile || {};
  autoWhy.textContent = [pr.auto_why && !pr.auto_owner ? `Категории подобрал сервис: ${pr.auto_why}` : pr.auto_owner ? "Категории выбраны вами, сервис их не меняет." : "",
    pr.auto_error ? `Не удалось применить в Директе: ${pr.auto_error}` : ""].filter(Boolean).join(" ");
  switches("aud-brands", L.brands, data.autotargeting.brands);
  const launched = launchedText(p.launched);
  $("aud-launched").hidden = !launched;
  $("aud-launched").textContent = launched;
}

function renderBids(b) {
  const hint = [];
  if (!b.needed) hint.push("Профиль не привязан ни к полу, ни к возрасту, поэтому ставки менять не нужно.");
  else {
    hint.push(`Целевым группам ставка выше на ${b.up}%, остальным ниже на ${b.down}%.`);
    if (b.plan.some((s) => s.percent === 0)) hint.push("Возрастные группы моложе целевых отключены (0%): им реклама не показывается. Остальных не отключаем: профиль ИИ может ошибаться, а настоящие данные покажут клики.");
    else hint.push("Никого не отключаем: профиль ИИ может ошибаться, а настоящие данные покажут клики.");
    if (b.off) hint.push("Вы убрали корректировки, поэтому сервис их не возвращает. Кнопка «Применить в Директе» включит их снова.");
    else if (!b.launched) hint.push("Сервис поставит их сам сразу после запуска рекламы.");
    else if (b.applied) hint.push(`Сервис поставил их сам ${b.applied.day} и обновит, если изменится профиль.`);
    else if (!b.error) hint.push("Сервис поставит их при ближайшем обновлении данных из Директа.");
    if (b.error) hint.push(`Директ не принял корректировки: ${b.error} Сервис повторит попытку.`);
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
  return (creativePrices.ai_banner || 0.07) + (a.ref_id ? lastRefExtra : 0) + (a.ref_id && a.photo_id && !a.no_photo ? 0.03 : 0);   // с фото и референсом арт-директор вписывает фото: ещё 3 цента
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

async function saveBudget() {
  const value = Number($("budget-input").value);
  if (!(value > 0)) { setStatus("budget-status", "Укажите бюджет на неделю в рублях.", "err"); return; }
  const button = $("budget-save");
  button.disabled = true;
  const r = await api("PUT", `/api/businesses/${currentBusiness.id}/budget`, { weekly_budget: value });
  button.disabled = false;
  if (!r.ok) { setStatus("budget-status", errorText(r), "err"); return; }
  currentBusiness = r.data;
  $("b-budget").value = r.data.weekly_budget || "";
  setStatus("budget-status", "");
  say("Бюджет сохранён.");
  await loadAds();   // число объявлений и сумма на кампанию зависят от бюджета
}

async function loadAds() {
  const result = await creativeCall("GET", "/ads");
  if (!result.ok) return;
  const { ads, count, advised, per_group, groups, weekly, site, has_maps, has_images, rejected_photos, photos, styles, ref_extra } = result.data;
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
  const extra = per_group > lastAdvised && lastAdvised ? ` Бюджету группы подходит ${lastAdvised}: чем больше объявлений в группе, тем меньше кликов достанется каждому и тем дольше сравнение.` : "";
  const plan = groups > 1
    ? `будет создано ${groups} кампании, по одной на группу объявлений (в группе до трёх): ${adsPhrase(count)}, бюджет ${weekly} ₽ в неделю на каждую группу, всего ${weekly * groups} ₽`
    : `будет создана одна кампания: ${adsPhrase(count)} и бюджет ${weekly} ₽ в неделю`;
  $("ads-summary").textContent = !count
    ? "Бюджет слишком мал для запуска: увеличьте его на странице «Бизнес». Так будут выглядеть объявления."
    : target
      ? `При запуске ${plan}. ${count === 1 ? "В показ пойдёт первое объявление из списка" : `В показ пойдут первые ${count} из списка`}, остальные запасные: они нужны, чтобы сравнить и заменить слабое.${extra} Объявления ведут на ${target}.`
      : `При запуске ${plan}. Чтобы запустить, укажите сайт или ссылку на Яндекс Карты на странице «Бизнес»: объявлению нужно куда вести.`;
  const input = $("budget-input");
  if (document.activeElement !== input) input.value = result.data.budget || "";   // пока человек печатает, поле не трогаем
  $("budget-hint").textContent = result.data.launched
    ? `Это бюджет одной группы объявлений (до трёх объявлений): у каждой группы своя кампания и свой лимит, больше сервис в неделю на неё не потратит. Новая группа получит ${weekly} ₽ в неделю. Лимит уже идущей кампании меняется на странице «Результаты», кнопкой «Изменить бюджет».`
    : count ? `Часть денег остаётся в запасе для роста: на группу уйдёт ${weekly} ₽ в неделю. Изменить можно и после запуска.`
      : "Этого мало для запуска: Яндекс требует минимум на кампанию. Увеличьте бюджет.";
  const note = $("ads-note");
  const lacking = ads.slice(0, count || ads.length).filter((a) => !a.banner_url).length;
  note.hidden = has_images && !lacking;
  note.textContent = !has_images
    ? (rejected_photos
      ? "Загруженные фото не подошли для баннеров: ИИ нашёл на них свой текст, чужой бренд или водяной знак (подробности на странице «Фото и баннеры»). Загрузите другие фото или нарисуйте фоновые картинки на странице «Фото и баннеры»."
      : "Фото пока нет. Загрузите свои на странице «Фото и баннеры» или нарисуйте фоновые картинки там же. Потом нажмите «Нарисовать» у объявления.")
    : `Баннеры сами не рисуются: у ${lacking} ${lacking === 1 ? "объявления" : "объявлений"} баннера пока нет. Выберите объявление и нажмите «Нарисовать баннер» или «Нарисовать все недостающие». Без баннера объявление уйдёт в рекламу без картинки.`;
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
      const pic = a.banner_url ? el("img", { className: "pic", src: a.banner_url, loading: "lazy", alt: "" }) : el("span", { className: "pic", textContent: a.ai && a.ai.status === "working" ? "…" : "нет" });
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

