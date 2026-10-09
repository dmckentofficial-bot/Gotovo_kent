(() => {
  const webApp = window.Telegram?.WebApp;
  const themeParams = webApp?.themeParams || {};
  const initData = webApp?.initData || "";

  const $ = (selector) => document.querySelector(selector);
  const nodes = {
    boot: $("#boot"),
    sources: $("#sources"),
    sourceCounter: $("#source-counter"),
    addSource: $("#add-source"),
    destination: $("#destination"),
    filterEnabled: $("#filter-enabled"),
    filterTerms: $("#filter-terms"),
    filterMode: $("#filter-mode"),
    filterOptions: $("#filter-options"),
    notice: $("#notice"),
    history: $("#history"),
    stats: $("#stats"),
    statusDetail: $("#status-detail"),
    badge: $("#service-badge"),
    toggle: $("#toggle-service"),
    botLine: $("#bot-line"),
    save: $("#save"),
    test: $("#test"),
    refresh: $("#refresh"),
    confirm: $("#confirm"),
    confirmTitle: $("#confirm-title"),
    confirmMessage: $("#confirm-message"),
    confirmOk: $("#confirm-ok"),
    confirmCancel: $("#confirm-cancel"),
  };

  let config = { sources: [], destination: "", filter_enabled: false, filter_terms: "", filter_mode: "any", enabled: false };
  const AUTO_REFRESH_MS = 60000;
  const STATUS_LABELS = { forwarded: "Переслано", filtered: "Фильтр", skipped: "Пропущено", error: "Ошибка" };
  const STATUS_ICONS = { forwarded: "✅", filtered: "🚫", skipped: "⏭️", error: "⚠️" };

  webApp?.ready();
  webApp?.expand();
  applyTelegramTheme();
  if (typeof webApp?.disableVerticalSwipes === "function") webApp.disableVerticalSwipes();

  function applyTelegramTheme() {
    const scheme = webApp?.colorScheme === "light" ? "light" : "dark";
    const root = document.documentElement;
    root.dataset.theme = scheme;
    const fallback = (light, dark) => (scheme === "light" ? light : dark);
    const set = (prop, value, fb) => root.style.setProperty(prop, value || fb);
    set("--bg", themeParams.bg_color, fallback("#f6f8fa", "#0b1220"));
    set("--surface", themeParams.secondary_bg_color, fallback("#ffffff", "#121d2e"));
    set("--text", themeParams.text_color, fallback("#101828", "#f3f7ff"));
    set("--muted", themeParams.hint_color, fallback("#5b6572", "#9db0c8"));
    set("--accent", themeParams.link_color, "#43c4e5");
    set("--button", themeParams.button_color, "#43c4e5");
    set("--button-text", themeParams.button_text_color, scheme === "light" ? "#ffffff" : "#07131b");
    const bgColor = themeParams.bg_color || fallback("#f6f8fa", "#0b1220");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", root.style.getPropertyValue("--bg") || bgColor);
    try { webApp?.setHeaderColor?.(bgColor); } catch (_) {}
    try { webApp?.setBackgroundColor?.(bgColor); } catch (_) {}
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
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
        ...(options.headers || {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `Ошибка сервера: ${response.status}`);
    return body;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = "Подождите…";
    } else {
      if (button.dataset.label) button.textContent = button.dataset.label;
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
    if (!sourceValues.length) {
      errors.push("Добавьте хотя бы один канал-источник.");
    }
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
        if (event.key === "Enter" && !event.isComposing) {
          event.preventDefault();
          nodes.save.click();
        }
      });
      row.append(input);
      if (config.sources.length > 1) {
        const remove = document.createElement("button");
        remove.className = "button secondary remove-source";
        remove.type = "button";
        remove.textContent = "−";
        remove.setAttribute("aria-label", `Удалить источник ${index + 1}`);
        remove.addEventListener("click", () => {
          config.sources.splice(index, 1);
          renderSources();
        });
        row.append(remove);
      }
      nodes.sources.append(row);
    });
    nodes.sourceCounter.textContent = `${config.sources.length} / 5`;
    nodes.addSource.disabled = config.sources.length >= 5;
  }

  function readForm() {
    config.sources = [...nodes.sources.querySelectorAll("input")]
      .map((input) => input.value.trim())
      .filter(Boolean);
    config.destination = nodes.destination.value.trim();
    config.filter_enabled = nodes.filterEnabled.checked;
    config.filter_terms = nodes.filterTerms.value;
    config.filter_mode = nodes.filterMode.value;
    return config;
  }

  async function loadConfig() {
    config = await api("/api/config");
    renderSources();
    nodes.destination.value = config.destination || "";
    nodes.filterEnabled.checked = Boolean(config.filter_enabled);
    nodes.filterTerms.value = config.filter_terms || "";
    nodes.filterMode.value = config.filter_mode || "any";
    nodes.filterOptions.hidden = !nodes.filterEnabled.checked;
  }

  function renderStats(stats) {
    nodes.stats.replaceChildren();
    nodes.stats.hidden = stats.total === 0;
    const chips = [
      ["forwarded", "Переслано", stats.forwarded],
      ["filtered", "Фильтр", stats.filtered],
      ["skipped", "Пропущено", stats.skipped],
      ["error", "Ошибки", stats.errors],
    ];
    for (const [key, label, count] of chips) {
      const chip = document.createElement("span");
      chip.className = `chip chip-${key}`;
      const name = document.createElement("span");
      name.textContent = label;
      const value = document.createElement("b");
      value.textContent = count;
      chip.append(name, value);
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
    nodes.badge.innerHTML = `<span class="dot" aria-hidden="true"></span>${running ? "Мониторинг включён" : "Остановлено"}`;
    nodes.badge.classList.toggle("running", running);
    nodes.badge.classList.toggle("stopped", !running);
    nodes.toggle.textContent = running ? "⏹ Остановить пересылку" : "▶️ Запустить пересылку";
    nodes.toggle.className = `button ${running ? "secondary" : "primary"}`;
    nodes.botLine.textContent = status.bot ? `Бот: @${status.bot}` : "";

    const details = [];
    if (status.last_checked) {
      details.push(`Последняя проверка: ${new Date(status.last_checked * 1000).toLocaleTimeString("ru-RU")}`);
    }
    if (status.last_error) details.push(`Ошибка: ${status.last_error}`);
    if (status.menu_error) details.push(`Кнопка бота: ${status.menu_error}`);
    if (!details.length) details.push("Проверка источников выполняется автоматически каждую минуту.");
    nodes.statusDetail.textContent = details.join("\n");

    renderStats(stats);
    renderHistory(history);
  }

  function askConfirm({ title, message, okLabel, danger }) {
    nodes.confirmTitle.textContent = title;
    nodes.confirmMessage.textContent = message;
    nodes.confirmOk.textContent = okLabel;
    nodes.confirmOk.className = `button ${danger ? "danger" : "primary"}`;
    nodes.confirm.hidden = false;
    nodes.confirmCancel.focus();
    return new Promise((resolve) => {
      nodes.confirm._resolve = resolve;
    });
  }

  nodes.confirmCancel.addEventListener("click", () => {
    nodes.confirm.hidden = true;
    nodes.confirm._resolve?.(false);
  });

  nodes.confirmOk.addEventListener("click", () => {
    nodes.confirm.hidden = true;
    nodes.confirm._resolve?.(true);
  });

  nodes.confirm.addEventListener("click", (event) => {
    if (event.target === nodes.confirm) {
      nodes.confirm.hidden = true;
      nodes.confirm._resolve?.(false);
    }
  });

  nodes.addSource.addEventListener("click", () => {
    if (config.sources.length < 5) {
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
      if (event.key === "Enter" && !event.isComposing) {
        event.preventDefault();
        nodes.save.click();
      }
    });
  });

  nodes.save.addEventListener("click", async (event) => {
    const errors = validateForm();
    if (errors.length) {
      showNotice(errors.join("\n"), "error");
      focusFirstInvalid();
      haptic("heavy");
      return;
    }
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      config = await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) }).then((result) => result.config);
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
    if (errors.length) {
      showNotice(errors.join("\n"), "error");
      focusFirstInvalid();
      haptic("heavy");
      return;
    }
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
    const errors = validateForm();
    if (errors.length) {
      showNotice(errors.join("\n"), "error");
      focusFirstInvalid();
      haptic("heavy");
      return;
    }
    const currentlyRunning = nodes.badge.classList.contains("running");
    const confirmed = await askConfirm({
      title: currentlyRunning ? "Остановить пересылку?" : "Запустить пересылку?",
      message: currentlyRunning
        ? "Фоновый мониторинг источников будет приостановлен. Настройки сохранятся."
        : "Сначала будут проверены каналы и права бота, затем мониторинг начнётся.",
      okLabel: currentlyRunning ? "Остановить" : "Запустить",
      danger: currentlyRunning,
    });
    if (!confirmed) return;

    const button = event.currentTarget;
    setBusy(button, true);
    try {
      const saved = await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) });
      config = saved.config;
      renderSources();
      await api("/api/service", {
        method: "PUT",
        body: JSON.stringify({ enabled: !currentlyRunning }),
      });
      showNotice(currentlyRunning ? "Пересылка остановлена." : "Настройки проверены, пересылка запущена.", "success");
      hapticResult("success");
    } catch (error) {
      showNotice(error.message, "error");
      haptic("heavy");
    } finally {
      setBusy(button, false);
      try {
        await refreshStatus();
      } catch (error) {
        showNotice(`Не удалось обновить статус: ${error.message}`, "error");
      }
    }
  });

  nodes.refresh.addEventListener("click", async () => {
    haptic("light");
    try {
      await refreshStatus();
      showNotice("Данные обновлены.", "success");
    } catch (error) {
      showNotice(error.message, "error");
    }
  });

  setInterval(() => {
    if (!initData) return;
    refreshStatus().catch(() => {});
  }, AUTO_REFRESH_MS);

  (async () => {
    if (!initData) {
      nodes.boot.hidden = true;
      showNotice("Откройте Mini App через кнопку в чате с вашим Telegram-ботом.", "error");
      return;
    }
    try {
      await loadConfig();
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      nodes.boot.hidden = true;
    }
  })();
})();