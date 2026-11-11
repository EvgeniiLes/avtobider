/* ---------- Страница «Новое A/B-тестирование»: новый вариант против идущего объявления ---------- */

let abTimer = null;

async function loadAbPage() {
  clearTimeout(abTimer);
  if (!currentBusiness) return;
  const result = await creativeCall("GET", "/ab");
  if (!result.ok) { say(errorText(result), "err"); return; }
  const drafts = result.data.drafts;
  $("ab-empty").hidden = drafts.length > 0;
  $("ab-list").replaceChildren(...drafts.map(abDraft));
  if (drafts.some((d) => d.banner === "working")) abTimer = setTimeout(loadAbPage, 5000);   // баннер рисуется: страница сама подхватит готовый
}

function abStats(o) {
  const ctr = ctrText(o.clicks, o.impressions);
  return `показов ${o.impressions} · переходов ${o.clicks} · CTR ${ctr} · расход ${money(o.spend)} · обращений ${o.leads}`;
}

function abDraft(d) {
  // слева то же объявление, что справа, но только для чтения: баннер, заголовок и текст как идут в рекламе
  const oTitle = el("input", { type: "text", value: d.original.title, readOnly: true });
  const oText = el("textarea", { rows: 3, value: d.original.text, readOnly: true });
  oTitle.setAttribute("aria-label", "Заголовок идущего объявления");
  oText.setAttribute("aria-label", "Текст идущего объявления");
  const oPicture = d.original.banner_url
    ? el("img", { src: d.original.banner_url, alt: "Баннер идущего объявления", className: "ab-banner" })
    : el("div", { className: "ab-banner empty", textContent: "Баннера у идущего объявления нет" });
  const original = el("div", { className: "ab-side readonly" },
    el("h3", { textContent: "A: идущее объявление" }), oPicture,
    el("label", { className: "sub", textContent: "Заголовок" }), oTitle,
    el("label", { className: "sub", textContent: "Текст" }), oText,
    el("p", { className: "hint", textContent: abStats(d.original) }));

  const title = el("input", { type: "text", value: d.title, maxLength: creativeLimits.title });
  const text = el("textarea", { rows: 3, value: d.text, maxLength: creativeLimits.text });
  title.setAttribute("aria-label", "Заголовок нового варианта");
  text.setAttribute("aria-label", "Текст нового варианта");
  const picture = d.banner === "ready"
    ? el("img", { src: d.banner_url, alt: "Баннер нового варианта", className: "ab-banner" })
    : el("div", { className: "ab-banner empty", textContent: d.banner === "working" ? "Баннер рисуется…" : d.banner === "failed" ? "Не вышло, нарисуйте заново" : "Баннера пока нет" });

  const save = el("button", { type: "button", className: "secondary", textContent: "Сохранить текст" });
  save.addEventListener("click", async () => {
    save.disabled = true;
    const r = await creativeCall("PUT", `/${d.creative_id}`, { title: title.value.trim(), body: text.value.trim() });
    save.disabled = false;
    if (!r.ok) { say(errorText(r), "err"); return; }
    say("Текст сохранён. Баннер подписан старым заголовком: если заголовок изменился, нарисуйте баннер заново.");
    await loadAbPage();
  });
  const again = el("button", { type: "button", className: "secondary", textContent: "Другой текст" });
  again.addEventListener("click", async () => {
    again.disabled = true; again.textContent = "Пишем…";
    const r = await creativeCall("POST", `/ads/${d.index}/rewrite`);
    if (!r.ok) { say(errorText(r), "err"); again.disabled = false; again.textContent = "Другой текст"; return; }
    say(`Новый текст: «${r.data.title}». Баннер к нему нужно нарисовать заново.`);
    await loadAbPage();
  });
  const draw = el("button", { type: "button", textContent: `${d.banner === "ready" ? "Нарисовать заново" : "Нарисовать баннер"} · ${usd(creativePrices.ai_banner || 0.07)}` });
  draw.disabled = d.banner === "working";
  draw.addEventListener("click", async () => {
    draw.disabled = true;
    const r = await creativeCall("POST", `/ads/${d.index}/ai-banner`);
    if (!r.ok) { draw.disabled = false; say(errorText(r), "err"); return; }
    await loadAbPage();
  });
  const go = el("button", { type: "button", className: "primary", textContent: "Запустить A/B" });
  go.disabled = d.banner !== "ready";   // без баннера сравнение нечестное: у идущего он есть
  go.title = d.banner === "ready" ? "" : "Сначала нарисуйте баннер";
  go.addEventListener("click", async () => {
    if (!confirm(`В рекламу уйдёт ОДНО новое объявление «${d.title}» в своей кампании, бюджет как у идущего. Через неделю или раньше сервис оставит сильное. Запустить?`)) return;
    go.disabled = true; go.textContent = "Запускаем…";
    const r = await api("POST", `/api/businesses/${currentBusiness.id}/launch/ab`, { creative_id: d.creative_id });
    go.disabled = false; go.textContent = "Запустить A/B";
    if (!r.ok) { say(errorText(r), "err"); return; }
    say(r.data.message);
    showPage("stats");
  });
  const drop = el("button", { type: "button", className: "secondary", textContent: "Убрать черновик" });
  drop.addEventListener("click", async () => {
    const r = await creativeCall("DELETE", `/${d.creative_id}`);
    if (!r.ok) { say(errorText(r), "err"); return; }
    await loadAbPage();
  });

  const variant = el("div", { className: "ab-side" },
    el("h3", { textContent: "B: новый вариант" }), picture,
    el("label", { className: "sub", textContent: "Заголовок" }), title,
    el("label", { className: "sub", textContent: "Текст" }), text,
    el("div", { className: "row" }, save, again, draw), el("div", { className: "row" }, go, drop));
  return el("section", { className: "ab-draft" }, original, variant);
}

$("ab-back").addEventListener("click", () => showPage("stats"));
