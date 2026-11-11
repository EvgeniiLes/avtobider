function setAuthMode(mode) {
  authMode = mode;
  $("tab-login").classList.toggle("active", mode === "login");
  $("tab-register").classList.toggle("active", mode === "register");
  $("auth-submit").textContent = mode === "login" ? "Войти" : "Создать аккаунт";
  $("password-hint").textContent = mode === "register" ? "(не меньше 8 символов)" : "";
  $("password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("password2-box").hidden = mode === "login";
  $("password2").required = mode === "register";
  $("password2").value = "";
  setStatus("auth-status", "");
}

async function submitAuth(event) {
  event.preventDefault();
  if (authMode === "register" && $("password").value !== $("password2").value) {
    setStatus("auth-status", "Пароли не совпадают. Введите один и тот же пароль дважды.", "err");
    return;
  }
  const button = $("auth-submit");
  button.disabled = true;
  setStatus("auth-status", "Подождите…");
  const result = await api("POST", `/api/auth/${authMode}`, {
    email: $("email").value,
    password: $("password").value,
  });
  button.disabled = false;
  if (!result.ok) {
    setStatus("auth-status", errorText(result), "err");
    return;
  }
  $("password").value = "";
  $("password2").value = "";
  showApp(result.data.email);
}

async function logout() {
  await api("POST", "/api/auth/logout");
  showAuth();
}

function showAuth() {
  $("boot").hidden = true;
  currentBusiness = null;
  document.body.classList.add("guest");   // гость видит главную с примером без бокового меню
  $("view-auth").hidden = false;
  $("view-app").hidden = true;
  $("user").hidden = true;
  $("nav").hidden = true;
  $("biz-switch").hidden = true;
  $("biz-menu").hidden = true;
  setAuthMode("register");
}

// Кнопки главной для гостей ведут к форме внизу страницы; «Войти» переключает её на вход.
function goToAuth(mode) {
  setAuthMode(mode);
  $("start").scrollIntoView({ behavior: "smooth" });
  $("email").focus({ preventScroll: true });
}

async function showApp(email) {
  $("boot").hidden = true;
  currentBusiness = null; // не тащим бизнес предыдущего пользователя
  document.body.classList.remove("guest");
  $("view-auth").hidden = true;
  $("view-app").hidden = false;
  $("user").hidden = false;
  $("nav").hidden = false;
  $("user-email").textContent = email;
  $("campaigns-card").hidden = true;
  await Promise.all([loadBusinesses(), loadAccounts()]);
  const page = pageFromHash();
  if (location.hash !== `#/${page}`) history.replaceState(null, "", `#/${page}`);
  showPage(page, false);
}

const PAGES = ["home", "business", "creatives", "test", "media", "stats", "accounts"];
let currentPage = "home";

// Адрес страницы лежит в ссылке (#/business): перезагрузка остаётся на месте, «назад» и «вперёд» работают.
// История изменений теперь внизу «Результатов»: старая ссылка #/history ведёт туда.
function pageFromHash() {
  const name = location.hash.replace(/^#\/?/, "");
  if (name === "history") return "stats";
  return PAGES.includes(name) ? name : "home";
}

function showPage(name, push = true) {
  if (push && location.hash !== `#/${name}`) history.pushState(null, "", `#/${name}`);
  currentPage = name;
  for (const page of PAGES) {
    $(`page-${page}`).hidden = page !== name;
    $(`nav-${page}`).classList.toggle("active", page === name);
  }
  window.scrollTo(0, 0);
  if (name !== "creatives" && name !== "media") clearTimeout(creativeTimer);
  if (name !== "test") clearTimeout(testTimer);
  if (name === "creatives" || name === "media") loadCreatives();
  if (name === "test") loadTestPage();
  if (name === "home" || name === "stats") loadStats();
  if (name === "stats") { setStatus("history-status", ""); loadHistory(); }
  if (name === "business") loadMarking();
  if (name === "accounts") {
    loadTelegram();
    if (accountsList.length) showCampaigns(accountsList[0].id);  // кампании кабинета видны сразу
  }
}

/* ---------- Бизнес ---------- */

