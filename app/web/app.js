async function start() {
  $("reconcile-go").addEventListener("click", async () => {
    if (!currentBusiness) return;
    const button = $("reconcile-go"), text = $("reconcile-text");
    button.disabled = true; button.textContent = "Сверяем…";
    const r = await api("POST", `/api/businesses/${currentBusiness.id}/reconcile`);
    button.disabled = false; button.textContent = "Сверить с кабинетом";
    text.hidden = false;
    text.textContent = r.ok ? r.data.text : errorText(r);
  });
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("account-form").addEventListener("submit", submitAccount);
  $("logout").addEventListener("click", logout);

  window.addEventListener("popstate", () => { if (!$("view-app").hidden) showPage(pageFromHash(), false); });
  $("example-off").addEventListener("click", () => $("sim-reset").click());
  $("biz-switch").addEventListener("change", onSwitch);
  bindBizMenu();
  bindLightbox();
  $("nav-home").addEventListener("click", () => showPage("home"));
  $("home-cta").addEventListener("click", () => { if (homeAction) homeAction(); });
  $("nav-business").addEventListener("click", () => showPage("business"));
  $("nav-accounts").addEventListener("click", () => showPage("accounts"));
  $("nav-stats").addEventListener("click", () => showPage("stats"));
  $("nav-media").addEventListener("click", () => showPage("media"));
  $("nav-creatives").addEventListener("click", () => showPage("creatives"));
  $("nav-test").addEventListener("click", () => showPage("test"));
  $("land-login").addEventListener("click", () => goToAuth("login"));
  $("land-start").addEventListener("click", () => goToAuth("register"));
  $("land-cta").addEventListener("click", () => goToAuth("register"));
  const toAds = (step) => () => { showPage("creatives"); setStep(step); };
  $("home-ads-more").addEventListener("click", () => showPage("stats"));
  $("launch-separate").addEventListener("change", () => { launchSeparateTouched = true; openLaunch(); });
  $("launch-responsive").addEventListener("change", openLaunch);   // формат поменяли: план (число групп, объявлений и бюджет) пересчитывается
  $("chip-banner").addEventListener("click", toAds("banners"));
  $("chip-ideas").addEventListener("click", toAds("ideas"));
  $("ptile-ideas-go").addEventListener("click", toAds("ideas"));
  $("chip-photo").addEventListener("click", () => showPage("media"));
  $("chip-budget").addEventListener("click", () => showPage("business"));
  $("units-empty-go").addEventListener("click", () => showPage("creatives"));
  $("briefs-gen").addEventListener("click", generateBriefs);
  for (const btn of document.querySelectorAll("#cr-steps .step")) btn.addEventListener("click", () => setStep(btn.dataset.step));
  $("wizard-next").addEventListener("click", wizardNext);
  $("wizard-back").addEventListener("click", wizardBack);
  $("wizard-done").addEventListener("click", finishWizard);
  $("ideas-next").addEventListener("click", () => setStep("banners"));
  $("banners-next").addEventListener("click", () => setStep("launch"));
  $("launch-back").addEventListener("click", () => setStep("banners"));
  $("leads-copy").addEventListener("click", copyLeadsSnippet);
  $("leads-newkey").addEventListener("click", newLeadsKey);
  $("tg-connect").addEventListener("click", connectTelegram);
  $("tg-check").addEventListener("click", checkTelegram);
  $("tg-test").addEventListener("click", testTelegram);
  $("tg-notify").addEventListener("change", setTelegramNotify);
  $("tg-disconnect").addEventListener("click", disconnectTelegram);
  $("q-analyze").addEventListener("click", analyzeQueries);
  $("q-apply").addEventListener("click", applyQueries);
  $("aud-bids-apply").addEventListener("click", () => changeBids("POST"));
  $("aud-placement").addEventListener("change", changePlacement);
  $("aud-bids-clear").addEventListener("click", () => changeBids("DELETE"));
  $("aud-build").addEventListener("click", buildAudience);
  $("aud-save").addEventListener("click", saveAudience);
  $("budget-save").addEventListener("click", saveBudget);
  $("ads-launch").addEventListener("click", () => { showPage("home"); openLaunch(); });
  $("ads-ai-all").addEventListener("click", makeAllBanners);
  $("ads-ai-redo").addEventListener("click", redrawAllBanners);
  $("photos-check").addEventListener("click", checkPhotos);
  $("insights-ai").addEventListener("click", analyzeResults);
  $("cr-gen-images").addEventListener("click", generateImages);
  $("cr-add-own").addEventListener("click", addOwnText);
  $("cr-own-title-n").replaceWith(counterEl($("cr-own-title"), 56));
  $("cr-own-body-n").replaceWith(counterEl($("cr-own-body"), 81));
  $("connect-direct").addEventListener("click", connectDirect);
  $("connect-cancel").addEventListener("click", stopConnect);
  $("m-counter").addEventListener("change", () => { loadGoals(""); loadSnippet(); });
  $("m-snippet-copy").addEventListener("click", copySnippet);
  $("m-auto").addEventListener("click", createMetrikaAuto);
  $("m-goals-add").addEventListener("click", addMissingGoals);
  $("m-health-check").addEventListener("click", () => loadHealth(true));
  $("m-save").addEventListener("click", saveMetrika);
  $("m-clear").addEventListener("click", clearMetrika);
  $("marking-ok").addEventListener("change", () => { $("launch-go").disabled = !$("marking-ok").checked; });
  $("b-nationwide").addEventListener("change", syncNationwide);
  for (const id of ["b-budget", "b-cpa", "b-check", "b-share", "b-description"]) $(id).addEventListener("input", scheduleAdvice);
  $("launch-go").addEventListener("click", confirmLaunch);
  $("mk-save").addEventListener("click", saveMarking);
  $("mk-open").addEventListener("click", async () => { showPage("business"); await loadMarking(); $("marking-card").scrollIntoView({ block: "start" }); });
  $("launch-cancel").addEventListener("click", () => { $("launch-card").hidden = true; });
  $("sync-now").addEventListener("click", syncNow);
  $("clients-save").addEventListener("click", saveClients);
  $("stop-all").addEventListener("click", stopAll);
  $("resume-all").addEventListener("click", resumeAll);
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
  $("business-copy").addEventListener("click", copyBusiness);
  for (const id of ["b-color-main", "b-color-extra"]) {
    $(id).addEventListener("input", () => { $(id).dataset.empty = "0"; });
  }
  for (const button of document.querySelectorAll("[data-clear]")) {
    button.addEventListener("click", () => setColor(button.dataset.clear, ""));
  }
  $("logo-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "logo"); e.target.value = ""; });
  $("screen-file").addEventListener("change", async (e) => { await uploadFiles([...e.target.files], "photo", "screen"); e.target.value = ""; });
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
      loadMarking();
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
