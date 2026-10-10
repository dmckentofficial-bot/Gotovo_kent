(() => {
  const params = new URLSearchParams(window.location.search);
  const isDemo = params.has("demo");
  const webApp = isDemo ? createDemoWebApp() : (window.Telegram?.WebApp || null);
  const initData = webApp?.initData || "";

  const $ = (selector) => document.querySelector(selector);
  const nodes = {
    boot: $("#boot"),
    bootText: $("#boot-text"),
    app: $("#app"),
    matrix: $("#matrix"),
    // login gate (пароль доступа)
    loginScreen: $("#login-screen"),
    loginSubtitle: $("#login-subtitle"),
    loginDots: $("#login-dots"),
    loginError: $("#login-error"),
    loginPad: $("#login-pad"),
    // dashboard
    badge: $("#service-badge"),
    toggle: $("#toggle-service"),
    heroStatus: $("#hero-status"),
    botLine: $("#bot-line"),
    statusDetail: $("#status-detail"),
    stats: $("#stats"),
    history: $("#history"),
    refresh: $("#refresh"),
    // bot & owner
    botToken: $("#bot-token"),
    botOwner: $("#bot-owner"),
    botSave: $("#bot-save"),
    botTokenHint: $("#bot-token-hint"),
    botOwnerHint: $("#bot-owner-hint"),
    // settings
    sources: $("#sources"),
    sourceCounter: $("#source-counter"),
    addSource: $("#add-source"),
    destination: $("#destination"),
    filterEnabled: $("#filter-enabled"),
    filterTerms: $("#filter-terms"),
    filterMode: $("#filter-mode"),
    filterOptions: $("#filter-options"),
    accessSetup: $("#access-setup"),
    save: $("#save"),
    test: $("#test"),
    // signature
    signatureEnabled: $("#signature-enabled"),
    signatureText: $("#signature-text"),
    signatureSave: $("#signature-save"),
    previewEmpty: $("#signature-preview-empty"),
    previewText: $("#signature-preview-text"),
    previewSign: $("#signature-preview-sign"),
    // shared
    notice: $("#notice"),
    confirm: $("#confirm"),
    confirmTitle: $("#confirm-title"),
    confirmMessage: $("#confirm-message"),
    confirmOk: $("#confirm-ok"),
    confirmCancel: $("#confirm-cancel"),
    // pin gate
    pinScreen: $("#pin-screen"),
    pinSubtitle: $("#pin-subtitle"),
    pinDots: $("#pin-dots"),
    pinError: $("#pin-error"),
    pinPad: $("#pin-pad"),
    pinCancel: $("#pin-cancel"),
  };

  let config = { sources: [], destination: "", filter_enabled: false, filter_terms: "", filter_mode: "any", signature_enabled: false, signature_text: "" };
  let pinConfigured = false;
  let pinUnlocked = false;

  const ADMIN_KEY_STORAGE = "fwd_admin_key";
  let adminKey = "";
  try { adminKey = localStorage.getItem(ADMIN_KEY_STORAGE) || ""; } catch (_) {}

  const AUTO_REFRESH_MS = 15000;
  const MAX_SOURCES = 5;
  const PIN_DIGITS = 4;
  const STATUS_LABELS = { forwarded: "Переслано", filtered: "Фильтр", skipped: "Пропущено", error: "Ошибка" };
  const STATUS_ICONS = { forwarded: "✓", filtered: "⌁", skipped: "↷", error: "!" };
  const PLAY_PATH = "M8 5v14l11-7z";
  const PAUSE_PATH = "M7 5h3.4v14H7zM13.6 5H17v14h-3.4z";

  const DEMOSTATE = {
    running: true,
    lastChecked: Math.floor(Date.now() / 1000) - 42,
    pinEnabled: false,
    config: {
      sources: ["channel_tech", "channel_news", "channel_sport", "channel_finance", "channel_design"],
      destination: "@my_destination",
      filter_enabled: true,
      filter_terms: "скидка\n#новости",
      filter_mode: "any",
      signature_enabled: true,
      signature_text: "Подписывайтесь: @prime_tg_channel",
      enabled: true,
    },
  };

  const DEMO_HISTORY_TEMPLATES = [
    { status: "forwarded", media_type: "text", message: "Запуск нового сервиса: промокод СКИДКА20 действует до конца недели.", details: "" },
    { status: "forwarded", media_type: "photo", message: "Новая коллекция уже в продаже. Подробности по ссылке внутри поста.", details: "" },
    { status: "filtered", media_type: "text", message: "Повтор анонса про акцию из соседнего канала.", details: "Не прошёл фильтр по ключевым словам." },
    { status: "forwarded", media_type: "video", message: "Видеообзор обновления 2.0 за 90 секунд.", details: "" },
    { status: "forwarded", media_type: "text", message: "Итоги недели: пять главных новостей отрасли в одном посте.", details: "" },
    { status: "skipped", media_type: "text", message: "", details: "Пустой пост без поддерживаемого медиа." },
    { status: "forwarded", media_type: "photo", message: "Оформляем подборку из трёх фото — сегодня в 18:00.", details: "" },
    { status: "filtered", media_type: "text", message: "Анонс стрима без нужных хэштегов.", details: "Не прошёл фильтр по ключевым словам." },
    { status: "error", media_type: "photo", message: "Изображение в этот раз слишком тяжёлое.", details: "Файл photo превышает лимит загрузки Telegram." },
  ];

  function demoHistory() {
    const now = Date.now();
    return DEMO_HISTORY_TEMPLATES.map((item, index) => ({
      created_at: new Date(now - (index * 7 + 1) * 60 * 1000).toISOString(),
      source: index % 2 ? "channel_news" : "channel_tech",
      message_id: String(1000 - index),
      status: item.status,
      media_type: item.media_type,
      message: item.message,
      details: item.details,
    }));
  }

  function demoStats() {
    const counts = {};
    demoHistory().forEach((item) => { counts[item.status] = (counts[item.status] || 0) + 1; });
    return {
      forwarded: counts.forwarded || 0,
      filtered: counts.filtered || 0,
      skipped: counts.skipped || 0,
      errors: counts.error || 0,
      total: demoHistory().length,
    };
  }

  async function demoApi(path, options = {}) {
    await new Promise((resolve) => setTimeout(resolve, 220));
    const method = (options.method || "GET").toUpperCase();
    const key = String(path).replace(/^\/+/, "");
    const payload = options.body ? JSON.parse(options.body) : {};
    if (key === "api/config") {
      if (method === "PUT") DEMOSTATE.config = { ...DEMOSTATE.config, ...payload };
      return { ...DEMOSTATE.config, sources: [...DEMOSTATE.config.sources] };
    }
    if (key === "api/signature" && method === "PUT") {
      DEMOSTATE.config.signature_enabled = Boolean(payload.enabled);
      DEMOSTATE.config.signature_text = String(payload.text || "").trim();
      return { saved: true, signature_enabled: DEMOSTATE.config.signature_enabled, signature_text: DEMOSTATE.config.signature_text };
    }
    if (key === "api/status") {
      return {
        running: DEMOSTATE.running,
        bot: "demo_forwarder_bot",
        sources: [...DEMOSTATE.config.sources],
        destination: DEMOSTATE.config.destination,
        last_checked: DEMOSTATE.lastChecked,
        last_error: "",
        menu_error: "",
      };
    }
    if (key === "api/history") return demoHistory();
    if (key === "api/stats") return demoStats();
    if (key === "api/test") {
      return {
        sources: DEMOSTATE.config.sources.map((source, index) => ({ source, available: true, posts: 32 - index * 4 })),
        bot: "demo_forwarder_bot",
        destination: DEMOSTATE.config.destination,
      };
    }
    if (key === "api/service" && method === "PUT") {
      DEMOSTATE.running = Boolean(payload.enabled);
      return { running: DEMOSTATE.running };
    }
    if (key === "api/pin/status") return { enabled: DEMOSTATE.pinEnabled };
    if (key.startsWith("api/pin/")) return { ok: true, enabled: DEMOSTATE.pinEnabled };
    return {};
  }

  function createDemoWebApp() {
    const light = params.has("light");
    const palette = light ? {
      bg_color: "#f6f8fa", secondary_bg_color: "#ffffff", text_color: "#101828",
      hint_color: "#5b6572", link_color: "#8F1E3A", button_color: "#8F1E3A", button_text_color: "#ffffff",
    } : {
      bg_color: "#0F0C0D", secondary_bg_color: "#1A1315", text_color: "#F7F3F4",
      hint_color: "#8A797D", link_color: "#8F1E3A", button_color: "#8F1E3A", button_text_color: "#ffffff",
    };
    const noop = () => {};
    return {
      initData: 'query_id=demo&user={"id":123456,"first_name":"Демо"}&auth_date=0',
      colorScheme: light ? "light" : "dark",
      themeParams: palette,
      ready: noop, expand: noop, disableVerticalSwipes: noop,
      setHeaderColor: noop, setBackgroundColor: noop,
      HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
    };
  }

  webApp?.ready();
  webApp?.expand();
  if (typeof webApp?.disableVerticalSwipes === "function") webApp.disableVerticalSwipes();
  if (typeof webApp?.setHeaderColor === "function") {
    try { webApp.setHeaderColor("#0F0C0D"); } catch (_) {}
  }

  function haptic(type = "light") {
    const feedback = webApp?.HapticFeedback;
    if (!feedback || typeof feedback.impactOccurred !== "function") return;
    try { feedback.impactOccurred(type); } catch (_) {}
  }

  function hapticResult(type) {
    const feedback = webApp?.HapticFeedback;
    if (!feedback || typeof feedback.notificationOccurred !== "function") return;
    try { feedback.notificationOccurred(type); } catch (_) {}
  }

  let noticeTimer = null;
  function showNotice(message, type = "") {
    nodes.notice.textContent = message;
    nodes.notice.className = `notice ${type}`;
    nodes.notice.hidden = !message;
    clearTimeout(noticeTimer);
    if (message && type === "success") {
      noticeTimer = setTimeout(() => { nodes.notice.hidden = true; }, 6000);
    }
  }

  async function api(path, options = {}) {
    if (isDemo) return demoApi(path, options);
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
        "X-Admin-Key": adminKey,
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail || `Ошибка сервера: ${response.status}`;
      if (response.status === 401 && !initData && response.headers.get("X-Requires-Auth") === "1") {
        adminKey = "";
        try { localStorage.removeItem(ADMIN_KEY_STORAGE); } catch (_) {}
        if (nodes.loginScreen.hidden) showLogin();
        throw new Error("Нужен вход по паролю доступа.");
      }
      throw new Error(detail);
    }
    return body;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = "Подождите…";
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  }

  function markInvalid(node, invalid) {
    if (!node) return;
    node.classList.toggle("invalid", invalid);
    if (invalid) node.setAttribute("aria-invalid", "true");
    else node.removeAttribute("aria-invalid");
  }

  function focusFirstInvalid() {
    const first = document.querySelector(".invalid");
    if (!first) return;
    first.focus?.();
    first.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }

  /* ---------- tabs ---------- */
  function activateTab(name) {
    document.querySelectorAll(".screen").forEach((screen) => {
      screen.hidden = screen.dataset.screen !== name;
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.classList.toggle("is-active", tab.dataset.tab === name);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      haptic("light");
      activateTab(tab.dataset.tab);
    });
  });

  /* ---------- validation ---------- */
  const USERNAME_RE = /^[A-Za-z0-9_]{5,32}$/;
  const TME_LINK_RE = /^https?:\/\/(www\.)?t\.me\/(s\/)?([A-Za-z0-9_]{5,32})\/?$/;

  function usernameFrom(value) {
    const cleaned = value.trim().replace(/\/+$/, "");
    if (cleaned.startsWith("@")) return cleaned.slice(1);
    const match = cleaned.match(TME_LINK_RE);
    if (match) return match[3];
    return cleaned;
  }

  function validateForm() {
    const errors = [];
    markInvalid(nodes.destination, false);
    const sourceInputs = [...nodes.sources.querySelectorAll("input")];
    sourceInputs.forEach((input) => markInvalid(input, false));

    const destination = nodes.destination.value.trim();
    if (!destination) {
      errors.push("Укажите канал назначения.");
      markInvalid(nodes.destination, true);
    } else if (!usernameFrom(destination)) {
      errors.push("Канал назначения — это @username или ссылка t.me.");
      markInvalid(nodes.destination, true);
    }

    const sourceValues = sourceInputs.map((input) => input.value.trim()).filter(Boolean);
    if (!sourceValues.length) errors.push("Добавьте хотя бы один канал-источник.");
    const seenValid = new Set();
    sourceValues.forEach((value, index) => {
      const username = usernameFrom(value);
      if (!username) return;
      if (!USERNAME_RE.test(username)) {
        errors.push(`Канал-источник ${index + 1}: ожидается @username или ссылка t.me.`);
        markInvalid(sourceInputs[index], true);
        return;
      }
      const key = username.toLowerCase();
      if (seenValid.has(key)) {
        errors.push(`Канал-источник ${index + 1} повторяется.`);
        markInvalid(sourceInputs[index], true);
      }
      seenValid.add(key);
    });

    if (nodes.filterEnabled.checked && !nodes.filterTerms.value.trim()) {
      errors.push("Включён фильтр, но не указаны слова или хэштеги.");
    }
    return errors;
  }

  /* ---------- settings ---------- */
  function renderSources() {
    nodes.sources.replaceChildren();
    config.sources.forEach((source, index) => {
      const row = document.createElement("div");
      row.className = "source-row";
      const input = document.createElement("input");
      input.className = "text-input";
      input.type = "text";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = `https://t.me/канал_${index + 1}`;
      input.setAttribute("aria-label", `Канал-источник ${index + 1}`);
      input.value = source;
      input.addEventListener("input", () => { config.sources[index] = input.value; });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); nodes.save.click(); }
      });
      row.append(input);
      if (config.sources.length > 1) {
        const remove = document.createElement("button");
        remove.className = "button secondary remove-source";
        remove.type = "button";
        remove.textContent = "−";
        remove.setAttribute("aria-label", `Удалить источник ${index + 1}`);
        remove.addEventListener("click", () => { config.sources.splice(index, 1); renderSources(); });
        row.append(remove);
      }
      nodes.sources.append(row);
    });
    nodes.sourceCounter.textContent = `${config.sources.length} / ${MAX_SOURCES}`;
    nodes.addSource.disabled = config.sources.length >= MAX_SOURCES;
  }

  function readForm() {
    config.sources = [...nodes.sources.querySelectorAll("input")]
      .map((input) => input.value.trim())
      .filter(Boolean);
    config.destination = nodes.destination.value.trim();
    config.filter_enabled = nodes.filterEnabled.checked;
    config.filter_terms = nodes.filterTerms.value;
    config.filter_mode = nodes.filterMode.value;
    return {
      sources: config.sources,
      destination: config.destination,
      filter_enabled: config.filter_enabled,
      filter_terms: config.filter_terms,
      filter_mode: config.filter_mode,
    };
  }

  function fillSettingsForm() {
    renderSources();
    nodes.destination.value = config.destination || "";
    nodes.filterEnabled.checked = Boolean(config.filter_enabled);
    nodes.filterTerms.value = config.filter_terms || "";
    nodes.filterMode.value = config.filter_mode || "any";
    nodes.filterOptions.hidden = !nodes.filterEnabled.checked;
  }

  async function loadConfig() {
    config = { ...config, ...(await api("/api/config")) };
    fillSettingsForm();
    fillSignatureForm();
  }

  /* ---------- signature ---------- */
  function fillSignatureForm() {
    nodes.signatureEnabled.checked = Boolean(config.signature_enabled);
    nodes.signatureText.value = config.signature_text || "";
    renderSignaturePreview();
  }

  function renderSignaturePreview() {
    const enabled = nodes.signatureEnabled.checked;
    const signature = nodes.signatureText.value.trim();
    const show = enabled && signature;
    nodes.previewEmpty.hidden = Boolean(show);
    nodes.previewText.hidden = !show;
    nodes.previewSign.hidden = !show;
    if (show) {
      nodes.previewText.textContent = "Новый пост из канала-источника — так будет выглядеть пересланное сообщение.";
      nodes.previewSign.textContent = signature;
    }
  }

  nodes.signatureEnabled.addEventListener("change", () => { haptic("light"); renderSignaturePreview(); });
  nodes.signatureText.addEventListener("input", renderSignaturePreview);

  /* ---------- status / history ---------- */
  function renderStats(stats) {
    nodes.stats.replaceChildren();
    const chips = [
      ["forwarded", "Переслано", stats.forwarded],
      ["filtered", "Фильтр", stats.filtered],
      ["skipped", "Пропущено", stats.skipped],
      ["error", "Ошибки", stats.errors],
    ];
    for (const [key, label, count] of chips) {
      const chip = document.createElement("span");
      chip.className = `chip chip-${key}`;
      const value = document.createElement("b");
      value.textContent = count;
      const name = document.createElement("span");
      name.textContent = label;
      chip.append(value, name);
      nodes.stats.append(chip);
    }
  }

  function relativeTime(iso) {
    const then = new Date(iso).getTime();
    if (!Number.isFinite(then)) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 5) return "только что";
    if (seconds < 60) return `${seconds} с назад`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} мин назад`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} ч назад`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days} дн назад`;
    return new Date(iso).toLocaleDateString("ru-RU");
  }

  function renderHistory(history) {
    nodes.history.replaceChildren();
    if (!history.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Событий пока нет.";
      nodes.history.append(empty);
      return;
    }
    for (const item of history) {
      const card = document.createElement("article");
      card.className = "history-item";
      card.dataset.status = item.status || "";
      const meta = document.createElement("div");
      meta.className = "history-meta";
      const state = document.createElement("span");
      state.className = "history-state";
      const icon = STATUS_ICONS[item.status] || "";
      state.textContent = `${icon} ${STATUS_LABELS[item.status] || item.status} · @${item.source} · ${item.media_type}`.trim();
      const time = document.createElement("time");
      time.className = "history-time";
      time.dateTime = item.created_at;
      time.textContent = relativeTime(item.created_at);
      time.title = new Date(item.created_at).toLocaleString("ru-RU");
      meta.append(state, time);
      card.append(meta);
      if (item.message) {
        const message = document.createElement("p");
        message.className = "history-message";
        message.textContent = item.message;
        card.append(message);
      }
      if (item.details) {
        const details = document.createElement("p");
        details.className = "hint";
        details.textContent = item.details;
        card.append(details);
      }
      nodes.history.append(card);
    }
  }

  async function refreshStatus() {
    const [status, history, stats] = await Promise.all([
      api("/api/status"),
      api("/api/history"),
      api("/api/stats"),
    ]);
    const running = Boolean(status.running);
    nodes.badge.innerHTML = `<span class="dot" aria-hidden="true"></span>${running ? "Пересылка активна" : "Остановлено"}`;
    nodes.badge.classList.toggle("running", running);
    nodes.badge.classList.toggle("stopped", !running);
    nodes.heroStatus.textContent = running ? "Пересылка активна" : "Пересылка остановлена";
    nodes.toggle.classList.toggle("is-running", running);
    const icon = nodes.toggle.querySelector(".hero-toggle-icon");
    if (icon) icon.setAttribute("d", running ? PAUSE_PATH : PLAY_PATH);
    nodes.toggle.setAttribute("aria-label", running ? "Остановить пересылку" : "Запустить пересылку");
    nodes.botLine.textContent = status.bot ? `Бот: @${status.bot}` : "";

    const details = [];
    if (status.last_checked) {
      details.push(`Последняя проверка: ${new Date(status.last_checked * 1000).toLocaleTimeString("ru-RU")}`);
    }
    if (status.last_error) details.push(`Ошибка: ${status.last_error}`);
    if (status.menu_error) details.push(`Кнопка бота: ${status.menu_error}`);
    if (!details.length) details.push("Проверка источников выполняется автоматически каждые 15 секунд.");
    nodes.statusDetail.textContent = details.join("\n");

    renderStats(stats);
    renderHistory(history);
  }

  /* ---------- confirm modal ---------- */
  function askConfirm({ title, message, okLabel, danger }) {
    nodes.confirmTitle.textContent = title;
    nodes.confirmMessage.textContent = message;
    nodes.confirmOk.textContent = okLabel;
    nodes.confirmOk.className = `button ${danger ? "danger" : "primary"}`;
    nodes.confirm.hidden = false;
    nodes.confirmCancel.focus();
    return new Promise((resolve) => { nodes.confirm._resolve = resolve; });
  }

  nodes.confirmCancel.addEventListener("click", () => { nodes.confirm.hidden = true; nodes.confirm._resolve?.(false); });
  nodes.confirmOk.addEventListener("click", () => { nodes.confirm.hidden = true; nodes.confirm._resolve?.(true); });
  nodes.confirm.addEventListener("click", (event) => {
    if (event.target === nodes.confirm) { nodes.confirm.hidden = true; nodes.confirm._resolve?.(false); }
  });

  /* ---------- matrix animation ---------- */
  const matrixFx = (() => {
    const canvas = nodes.matrix;
    if (!canvas) return { start() {}, stop() {}, hide() {} };
    const ctx = canvas.getContext("2d");
    const GLYPHS = "アイウエオカキクケコサシスセソタチツテトナニヌネノ0123456789ABCDEFXYZ@#$%&*+".split("");
    let raf = 0;
    let fontSize = 16;
    let drops = [];
    let running = false;

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(window.innerWidth * dpr);
      canvas.height = Math.floor(window.innerHeight * dpr);
      fontSize = Math.max(12, Math.floor((window.innerWidth < 600 ? 14 : 16) * dpr));
      const columns = Math.ceil(canvas.width / fontSize);
      drops = Array.from({ length: columns }, () => Math.floor(Math.random() * -60));
    }

    function frame() {
      ctx.fillStyle = "rgba(4, 7, 9, 0.09)";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#35e07f";
      ctx.textBaseline = "top";
      ctx.font = `${fontSize}px monospace`;
      for (let i = 0; i < drops.length; i += 1) {
        const glyph = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
        ctx.fillText(glyph, i * fontSize, drops[i] * fontSize);
        if (drops[i] * fontSize > canvas.height && Math.random() > 0.972) drops[i] = 0;
        drops[i] += 1;
      }
      raf = requestAnimationFrame(frame);
    }

    return {
      start() {
        if (canvas.hidden) canvas.hidden = false;
        if (running) return;
        running = true;
        window.addEventListener("resize", resize);
        resize();
        frame();
      },
      stop() {
        running = false;
        cancelAnimationFrame(raf);
        window.removeEventListener("resize", resize);
      },
      hide() {
        running = false;
        cancelAnimationFrame(raf);
        canvas.hidden = true;
        window.removeEventListener("resize", resize);
      },
    };
  })();

  /* ---------- вход по паролю доступа (установка и повторный вход) ---------- */
  const ACCESS_MIN = 4;
  const ACCESS_MAX = 8;
  const loginGate = (() => {
    let buffer = "";
    let stage = 0;
    let first = null;
    let mode = "login";
    let submitFn = null;
    let resolver = null;

    const SUBTITLES = {
      login: "Введите пароль доступа",
      setup0: "Придумайте пароль",
      setup1: "Повторите пароль",
    };

    function currentSubtitle() {
      if (mode === "setup") return stage === 0 ? SUBTITLES.setup0 : SUBTITLES.setup1;
      return SUBTITLES.login;
    }

    function renderDots() {
      nodes.loginDots.replaceChildren();
      const count = Math.max(ACCESS_MIN, buffer.length);
      for (let i = 0; i < count; i += 1) {
        const dot = document.createElement("span");
        if (i < buffer.length) dot.className = "filled";
        nodes.loginDots.append(dot);
      }
    }

    function setError(text) {
      nodes.loginError.textContent = text || "";
      nodes.loginError.hidden = !text;
      if (text) {
        nodes.loginDots.classList.remove("shake");
        void nodes.loginDots.offsetWidth;
        nodes.loginDots.classList.add("shake");
      }
    }

    function reset() { buffer = ""; renderDots(); }

    function open(nextMode, submit) {
      mode = nextMode;
      stage = 0;
      first = null;
      submitFn = submit;
      buffer = "";
      setError("");
      renderDots();
      nodes.loginSubtitle.textContent = currentSubtitle();
      nodes.loginScreen.hidden = false;
      matrixFx.start();
      return new Promise((resolve) => { resolver = resolve; });
    }

    function hide() {
      nodes.loginScreen.hidden = true;
      matrixFx.hide();
    }

    async function confirmEntry() {
      const value = buffer;
      if (value.length < ACCESS_MIN) {
        setError(`Минимум ${ACCESS_MIN} цифры.`);
        haptic("heavy");
        return;
      }
      if (mode === "setup") {
        if (stage === 0) {
          first = value;
          stage = 1;
          buffer = "";
          setError("");
          renderDots();
          nodes.loginSubtitle.textContent = currentSubtitle();
          return;
        }
        if (value !== first) {
          setError("Пароли не совпадают. Начните заново.");
          haptic("heavy");
          stage = 0;
          first = null;
          buffer = "";
          renderDots();
          nodes.loginSubtitle.textContent = currentSubtitle();
          return;
        }
      }
      try {
        await submitFn(value);
      } catch (error) {
        setError(error.message || "Не удалось сохранить пароль.");
        haptic("heavy");
        buffer = "";
        renderDots();
        if (mode === "setup") {
          stage = 0;
          first = null;
          nodes.loginSubtitle.textContent = currentSubtitle();
        }
        return;
      }
      hapticResult("success");
      const done = resolver;
      resolver = null;
      hide();
      done?.(value);
    }

    function press(key) {
      if (key === "back") { buffer = buffer.slice(0, -1); renderDots(); return; }
      if (key === "ok") { confirmEntry(); return; }
      if (!/^\d$/.test(key) || buffer.length >= ACCESS_MAX) return;
      haptic("light");
      buffer += key;
      renderDots();
    }

    nodes.loginPad.addEventListener("click", (event) => {
      const button = event.target.closest("[data-key]");
      if (button) press(button.dataset.key);
    });

    return { open, hide };
  })();

  function showLogin() {
    loginGate.open("login", verifyAccessPassword).then(() => { start(); });
  }

  /* ---------- работа с паролем доступа на сервере ---------- */
  function rememberAccessPassword(value) {
    adminKey = value;
    try { localStorage.setItem(ADMIN_KEY_STORAGE, value); } catch (_) {}
  }

  function forgetAccessPassword() {
    adminKey = "";
    try { localStorage.removeItem(ADMIN_KEY_STORAGE); } catch (_) {}
  }

  // Создание пароля при первом входе (сервер разрешает, пока пароль не задан).
  async function saveAccessPassword(password) {
    await api("/api/access/setup", { method: "POST", body: JSON.stringify({ password }) });
    rememberAccessPassword(password);
  }

  // Проверка пароля при входе (сервер отвечает 401/429 при ошибках).
  async function verifyAccessPassword(password) {
    await api("/api/access/verify", { method: "POST", body: JSON.stringify({ password }) });
    rememberAccessPassword(password);
  }

  // Полный цикл авторизации из браузера: создание пароля при первом входе или проверка сохранённого.
  async function ensureBrowserAuth() {
    let configured = true;
    try {
      configured = Boolean((await api("/api/access/status")).configured);
    } catch (_) {
      // если статус недоступен — покажем экран входа
    }
    if (adminKey) {
      try {
        await api("/api/access/verify", { method: "POST", body: JSON.stringify({ password: adminKey }) });
        return;
      } catch (_) {
        forgetAccessPassword();
      }
    }
    if (!configured) {
      await loginGate.open("setup", saveAccessPassword);
      return;
    }
    await loginGate.open("login", verifyAccessPassword);
  }

  matrixFx.start();

  /* ---------- PIN gate ---------- */
  const pinGate = (() => {
    let mode = "unlock";
    let buffer = "";
    let stage = 0;
    let firstPin = null;
    let currentPin = null;
    let resolver = null;

    const subtitles = {
      unlock: "Введите PIN-код",
      setup0: "Придумайте PIN-код",
      setup1: "Повторите код",
      change0: "Текущий PIN-код",
      change1: "Новый PIN-код",
      change2: "Повторите новый код",
      disable: "Введите текущий PIN-код",
    };

    function renderDots() {
      nodes.pinDots.replaceChildren();
      for (let i = 0; i < PIN_DIGITS; i += 1) {
        const dot = document.createElement("span");
        if (i < buffer.length) dot.className = "filled";
        nodes.pinDots.append(dot);
      }
    }

    function setSubtitle(text) { nodes.pinSubtitle.textContent = text; }

    function setError(text) {
      nodes.pinError.textContent = text || "";
      nodes.pinError.hidden = !text;
      if (text) {
        nodes.pinDots.classList.remove("shake");
        void nodes.pinDots.offsetWidth;
        nodes.pinDots.classList.add("shake");
      }
    }

    function resetInput() { buffer = ""; renderDots(); }

    function close(result) {
      nodes.pinScreen.hidden = true;
      nodes.pinError.hidden = true;
      const done = resolver;
      resolver = null;
      done?.(result);
    }

    function open(nextMode, configured) {
      mode = nextMode;
      stage = 0;
      firstPin = null;
      currentPin = null;
      setError("");
      resetInput();
      pinConfigured = configured;
      const label = mode === "unlock" ? subtitles.unlock : subtitles[`${mode}${stage}`] || subtitles[mode];
      setSubtitle(label);
      nodes.pinCancel.hidden = mode === "unlock";
      nodes.pinScreen.hidden = false;
      return new Promise((resolve) => { resolver = resolve; });
    }

    async function complete() {
      const value = buffer;
      if (mode === "unlock") {
        try {
          await api("/api/pin/verify", { method: "POST", body: JSON.stringify({ pin: value }) });
          hapticResult("success");
          close(true);
        } catch (error) {
          setError(error.message); hapticResult("error"); resetInput();
        }
        return;
      }
      if (mode === "setup") {
        if (stage === 0) { firstPin = value; stage = 1; setError(""); resetInput(); setSubtitle(subtitles.setup1); return; }
        if (value !== firstPin) { setError("Коды не совпадают."); hapticResult("error"); stage = 0; firstPin = null; resetInput(); setSubtitle(subtitles.setup0); return; }
        try {
          await api("/api/pin/setup", { method: "POST", body: JSON.stringify({ pin: value }) });
          hapticResult("success"); close(true);
        } catch (error) { setError(error.message); hapticResult("error"); resetInput(); stage = 0; firstPin = null; setSubtitle(subtitles.setup0); }
        return;
      }
      if (mode === "change") {
        if (stage === 0) {
          try {
            await api("/api/pin/verify", { method: "POST", body: JSON.stringify({ pin: value }) });
            currentPin = value; stage = 1; setError(""); resetInput(); setSubtitle(subtitles.change1);
          } catch (error) { setError(error.message); hapticResult("error"); resetInput(); }
          return;
        }
        if (stage === 1) { firstPin = value; stage = 2; setError(""); resetInput(); setSubtitle(subtitles.change2); return; }
        if (value !== firstPin) { setError("Коды не совпадают."); hapticResult("error"); stage = 1; firstPin = null; resetInput(); setSubtitle(subtitles.change1); return; }
        try {
          await api("/api/pin/change", { method: "POST", body: JSON.stringify({ current_pin: currentPin, pin: value }) });
          hapticResult("success"); close(true);
        } catch (error) { setError(error.message); hapticResult("error"); resetInput(); stage = 1; firstPin = null; setSubtitle(subtitles.change1); }
        return;
      }
      if (mode === "disable") {
        try {
          await api("/api/pin/disable", { method: "POST", body: JSON.stringify({ pin: value }) });
          hapticResult("success"); close(true);
        } catch (error) { setError(error.message); hapticResult("error"); resetInput(); }
      }
    }

    function press(key) {
      if (key === "back") { buffer = buffer.slice(0, -1); renderDots(); return; }
      if (!/^\d$/.test(key) || buffer.length >= PIN_DIGITS) return;
      haptic("light");
      buffer += key;
      renderDots();
      if (buffer.length === PIN_DIGITS) setTimeout(complete, 130);
    }

    nodes.pinPad.addEventListener("click", (event) => {
      const button = event.target.closest("[data-key]");
      if (button) press(button.dataset.key);
    });
    nodes.pinCancel.addEventListener("click", () => { haptic("light"); close(false); });

    return { open, renderDots };
  })();

  /* ---------- bot & owner management ---------- */
  async function loadBotSettings() {
    let settings = null;
    try { settings = await api("/api/bot"); } catch (_) { return; }
    if (!settings) return;
    if (nodes.botToken) {
      nodes.botToken.value = "";
      nodes.botToken.placeholder = settings.token_hint || "не задан";
      if (nodes.botTokenHint) {
        nodes.botTokenHint.textContent = settings.token_hint
          ? `Текущий: ${settings.token_hint}`
          : "Введите токен от @BotFather";
      }
    }
    if (nodes.botOwner) {
      nodes.botOwner.value = settings.admin_id || "";
      if (nodes.botOwnerHint) nodes.botOwnerHint.textContent = "Ваш Telegram ID";
    }
    if (nodes.botLine) {
      nodes.botLine.textContent = settings.bot_username ? `@${settings.bot_username}` : "";
    }
  }

  nodes.botSave.addEventListener("click", async (event) => {
    setBusy(event.currentTarget, true);
    try {
      const token = nodes.botToken.value.trim();
      const adminId = nodes.botOwner.value.trim();
      if (!token && !adminId) {
        showNotice("Введите токен бота или ID владельца.", "error");
        return;
      }
      const result = await api("/api/bot", {
        method: "PUT",
        body: JSON.stringify({ token, admin_id: adminId }),
      });
      nodes.botToken.value = "";
      nodes.botToken.placeholder = result.token_hint || "не задан";
      if (nodes.botTokenHint) {
        nodes.botTokenHint.textContent = result.token_hint ? `Текущий: ${result.token_hint}` : "Введите токен от @BotFather";
      }
      if (result.bot_username && nodes.botLine) nodes.botLine.textContent = `@${result.bot_username}`;
      showNotice(result.bot_username ? `Бот @${result.bot_username} применён.` : "Настройки бота сохранены.", "success");
      hapticResult("success");
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(event.currentTarget, false);
      await refreshStatus().catch(() => {});
    }
  });

  nodes.accessSetup.addEventListener("click", async () => {
    haptic("light");
    const password = await loginGate.open("setup", saveAccessPassword);
    if (password) showNotice("Пароль доступа изменён.", "success");
  });

  /* ---------- settings events ---------- */
  nodes.addSource.addEventListener("click", () => {
    if (config.sources.length < MAX_SOURCES) {
      config.sources.push("");
      renderSources();
      nodes.sources.lastElementChild?.querySelector("input")?.focus();
    }
  });

  nodes.filterEnabled.addEventListener("change", () => {
    nodes.filterOptions.hidden = !nodes.filterEnabled.checked;
  });

  document.querySelectorAll("input.text-input").forEach((input) => {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); nodes.save.click(); }
    });
  });

  nodes.save.addEventListener("click", async (event) => {
    const errors = validateForm();
    if (errors.length) { showNotice(errors.join("\n"), "error"); focusFirstInvalid(); haptic("heavy"); return; }
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      const result = await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) });
      config = { ...config, ...result.config };
      renderSources();
      showNotice("Настройки сохранены. Новые публикации будут проверяться автоматически.", "success");
      hapticResult("success");
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(button, false);
    }
  });

  nodes.test.addEventListener("click", async (event) => {
    const errors = validateForm();
    if (errors.length) { showNotice(errors.join("\n"), "error"); focusFirstInvalid(); haptic("heavy"); return; }
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) });
      const result = await api("/api/test", { method: "POST" });
      const sourcesText = result.sources.map((item) => `@${item.source}: доступен (${item.posts} постов)`).join("\n");
      showNotice(`Подключение работает.\nБот: @${result.bot}\nКанал назначения: ${result.destination}\n${sourcesText}`, "success");
      hapticResult("success");
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(button, false);
    }
  });

  nodes.toggle.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const errors = validateForm();
    if (errors.length) {
      showNotice(errors.join("\n"), "error");
      focusFirstInvalid();
      haptic("heavy");
      activateTab("settings");
      return;
    }
    const currentlyRunning = nodes.badge.classList.contains("running");
    const confirmed = await askConfirm({
      title: currentlyRunning ? "Остановить пересылку?" : "Запустить пересылку?",
      message: currentlyRunning
        ? "Фоновый мониторинг источников будет приостановлен. Настройки сохранятся."
        : "Сначала проверятся каналы и права бота, затем пересылка начнётся сразу.",
      okLabel: currentlyRunning ? "Остановить" : "Запустить",
      danger: currentlyRunning,
    });
    if (!confirmed) return;

    setBusy(button, true);
    try {
      const saved = await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) });
      config = { ...config, ...saved.config };
      renderSources();
      await api("/api/service", { method: "PUT", body: JSON.stringify({ enabled: !currentlyRunning }) });
      showNotice(currentlyRunning ? "Пересылка остановлена." : "Пересылка запущена.", "success");
      hapticResult("success");
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(button, false);
      try { await refreshStatus(); } catch (error) { showNotice(`Не удалось обновить статус: ${error.message}`, "error"); }
    }
  });

  nodes.refresh.addEventListener("click", async () => {
    haptic("light");
    try { await refreshStatus(); showNotice("Данные обновлены.", "success"); }
    catch (error) { showNotice(error.message, "error"); }
  });

  nodes.signatureSave.addEventListener("click", async (event) => {
    const enabled = nodes.signatureEnabled.checked;
    const text = nodes.signatureText.value.trim();
    if (enabled && !text) {
      showNotice("Введите текст подписи или отключите её.", "error");
      haptic("heavy");
      return;
    }
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      const result = await api("/api/signature", { method: "PUT", body: JSON.stringify({ enabled, text }) });
      config.signature_enabled = result.signature_enabled;
      config.signature_text = result.signature_text;
      hapticResult("success");
      showNotice(result.signature_enabled ? "Подпись сохранена." : "Подпись отключена.", "success");
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(button, false);
    }
  });

  setInterval(() => {
    if ((!initData && !adminKey) || !pinUnlocked) return;
    refreshStatus().catch(() => {});
  }, AUTO_REFRESH_MS);

  async function start() {
    if (isDemo) showNotice("Демо-режим: данные имитированы локально, ничего никуда не пересылается.");
    const inTelegram = Boolean(initData);
    if (!inTelegram) {
      // Браузер без Telegram: сначала пароль доступа (создать при первом входе или ввести сохранённый).
      nodes.boot.hidden = true;
      await ensureBrowserAuth();
    }
    const byPassword = !inTelegram && Boolean(adminKey);
    try {
      const pinStatus = await api("/api/pin/status");
      pinConfigured = Boolean(pinStatus.enabled);
      if (pinConfigured && !byPassword) {
        nodes.boot.hidden = true;
        const ok = await pinGate.open("unlock", true);
        if (!ok) return;
      }
      pinUnlocked = true;
      nodes.app.hidden = false;
      if (byPassword) hapticResult("success");
      await loadConfig();
      await loadBotSettings();
      await refreshStatus();
    } catch (error) {
      nodes.app.hidden = false;
      showNotice(error.message, "error");
    } finally {
      nodes.boot.hidden = true;
      matrixFx.hide();
    }
  }

  start();
})();
