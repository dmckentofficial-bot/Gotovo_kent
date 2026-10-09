(() => {
  const webApp = window.Telegram?.WebApp;
  const initData = webApp?.initData || "";
  const sourcesNode = document.querySelector("#sources");
  const noticeNode = document.querySelector("#notice");
  const historyNode = document.querySelector("#history");
  const addSourceButton = document.querySelector("#add-source");
  const filterEnabled = document.querySelector("#filter-enabled");
  let config = { sources: [], destination: "", filter_enabled: false, filter_terms: "", filter_mode: "any" };

  webApp?.ready();
  webApp?.expand();

  function showNotice(message, type = "") {
    noticeNode.textContent = message;
    noticeNode.className = `notice ${type}`;
    noticeNode.hidden = !message;
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

  function renderSources() {
    sourcesNode.replaceChildren();
    config.sources.forEach((source, index) => {
      const row = document.createElement("div");
      row.className = "source-row";
      const input = document.createElement("input");
      input.className = "text-input";
      input.type = "text";
      input.autocomplete = "off";
      input.placeholder = `https://t.me/канал_${index + 1}`;
      input.setAttribute("aria-label", `Канал-источник ${index + 1}`);
      input.value = source;
      input.addEventListener("input", () => { config.sources[index] = input.value; });
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
      sourcesNode.append(row);
    });
    document.querySelector("#source-counter").textContent = `${config.sources.length} / 5`;
    addSourceButton.disabled = config.sources.length >= 5;
  }

  function readForm() {
    config.sources = [...sourcesNode.querySelectorAll("input")]
      .map((input) => input.value.trim())
      .filter(Boolean);
    config.destination = document.querySelector("#destination").value.trim();
    config.filter_enabled = filterEnabled.checked;
    config.filter_terms = document.querySelector("#filter-terms").value;
    config.filter_mode = document.querySelector("#filter-mode").value;
    return config;
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    if (busy) button.dataset.label = button.textContent;
    button.textContent = busy ? "Подождите…" : (button.dataset.label || button.textContent);
  }

  async function loadConfig() {
    config = await api("/api/config");
    renderSources();
    document.querySelector("#destination").value = config.destination || "";
    filterEnabled.checked = Boolean(config.filter_enabled);
    document.querySelector("#filter-terms").value = config.filter_terms || "";
    document.querySelector("#filter-mode").value = config.filter_mode || "any";
    document.querySelector("#filter-options").hidden = !filterEnabled.checked;
  }

  async function refreshStatus() {
    const [status, history] = await Promise.all([api("/api/status"), api("/api/history")]);
    const badge = document.querySelector("#service-badge");
    badge.textContent = status.running ? "Мониторинг включён" : "Настройте каналы";
    badge.className = `badge ${status.running ? "running" : "stopped"}`;
    const details = [];
    if (status.last_checked) {
      details.push(`Последняя проверка: ${new Date(status.last_checked * 1000).toLocaleTimeString("ru-RU")}`);
    }
    if (status.last_error) details.push(`Ошибка: ${status.last_error}`);
    if (status.menu_error) details.push(`Кнопка бота: ${status.menu_error}`);
    document.querySelector("#status-detail").textContent = details.join("\n") || "Проверка источников выполняется автоматически раз в минуту.";
    historyNode.replaceChildren();
    if (!history.length) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Событий пока нет.";
      historyNode.append(empty);
      return;
    }
    for (const item of history) {
      const card = document.createElement("article");
      card.className = "history-item";
      const meta = document.createElement("div");
      meta.className = "history-meta";
      const labels = { forwarded: "Переслано", filtered: "Фильтр", skipped: "Пропущено", error: "Ошибка" };
      const state = document.createElement("span");
      state.textContent = `${labels[item.status] || item.status} · @${item.source} · ${item.media_type}`;
      const date = document.createElement("time");
      date.textContent = new Date(item.created_at).toLocaleString("ru-RU");
      meta.append(state, date);
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
      historyNode.append(card);
    }
  }

  addSourceButton.addEventListener("click", () => {
    if (config.sources.length < 5) {
      config.sources.push("");
      renderSources();
      sourcesNode.lastElementChild?.querySelector("input")?.focus();
    }
  });

  filterEnabled.addEventListener("change", () => {
    document.querySelector("#filter-options").hidden = !filterEnabled.checked;
  });

  document.querySelector("#save").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      config = await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) }).then((result) => result.config);
      renderSources();
      showNotice("Настройки сохранены. Новые публикации будут проверяться автоматически.", "success");
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });

  document.querySelector("#test").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setBusy(button, true);
    try {
      await api("/api/config", { method: "PUT", body: JSON.stringify(readForm()) });
      const result = await api("/api/test", { method: "POST" });
      const sourcesText = result.sources.map((item) => `@${item.source}: доступен (${item.posts} постов)`).join("\n");
      showNotice(`Подключение работает.\nБот: @${result.bot}\nКанал назначения: ${result.destination}\n${sourcesText}`, "success");
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });

  document.querySelector("#refresh").addEventListener("click", async () => {
    try {
      await refreshStatus();
      showNotice("Данные обновлены.", "success");
    } catch (error) {
      showNotice(error.message, "error");
    }
  });

  (async () => {
    if (!initData) {
      showNotice("Откройте Mini App через кнопку в чате с вашим Telegram-ботом.", "error");
      return;
    }
    try {
      await loadConfig();
      await refreshStatus();
    } catch (error) {
      showNotice(error.message, "error");
    }
  })();
})();
