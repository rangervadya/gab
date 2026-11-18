(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const MAX_SIZE = 20 * 1024 * 1024;
  const ALLOWED = ["pdf", "xls", "xlsx", "doc", "docx"];

  const state = {
    batches: [],
    selectedBatchId: null,
    selectedFiles: [],
    uploading: false,
    initialized: false,
    query: "",
    segment: "all"
  };

  const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[ch]);

  const text = value => {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  };

  const dateText = value => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? esc(value)
      : date.toLocaleString("ru-RU");
  };

  const moneyText = value => {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number)
      ? number.toLocaleString("ru-RU") + " ₽"
      : esc(value);
  };

  const batchStatus = value => ({
    uploaded: "Ожидает обработки",
    processing: "Обрабатывается",
    review: "На проверке",
    ready: "Готов к проверке",
    completed: "Завершён",
    failed: "Ошибка"
  })[value] || value || "Неизвестно";

  const rowStatus = value => ({
    pending: "Ожидает",
    created: "Создан",
    matched: "Найдено совпадение",
    duplicate: "Дубликат",
    needs_review: "Нужна проверка",
    rejected: "Отклонён"
  })[value] || value || "—";

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "Accept": "application/json", ...(options.headers || {}) },
      ...options
    });

    if (!response.ok) {
      let message = `Ошибка HTTP ${response.status}`;
      try {
        const data = await response.json();
        message = data.detail || data.message || message;
      } catch {}
      throw new Error(message);
    }

    if (response.status === 204) return null;
    return response.json();
  }

  function notifyImport(message, isError = false) {
    const toast = $("toast");
    if (toast) {
      toast.textContent = message;
      toast.classList.add("show");
      toast.classList.toggle("error", isError);
      setTimeout(() => toast.classList.remove("show"), 4500);
    } else {
      alert(message);
    }
  }

  function showFileSelection() {
    const box = $("importSelectedFiles");
    const button = $("importUploadBtn");
    if (!box || !button) return;

    if (!state.selectedFiles.length) {
      box.textContent = "Файлы не выбраны";
      button.disabled = state.uploading;
      return;
    }

    box.innerHTML = state.selectedFiles.map(file =>
      `<div class="import-file-line">
        <span>${esc(file.name)}</span>
        <small>${(file.size / 1024 / 1024).toFixed(2)} МБ</small>
      </div>`
    ).join("");

    button.disabled = state.uploading;
  }

  function validateFiles(files) {
    const errors = [];
    for (const file of files) {
      const ext = file.name.split(".").pop().toLowerCase();
      if (!ALLOWED.includes(ext)) {
        errors.push(`${file.name}: неподдерживаемый формат`);
      } else if (!file.size) {
        errors.push(`${file.name}: файл пуст`);
      } else if (file.size > MAX_SIZE) {
        errors.push(`${file.name}: превышен лимит 20 МБ`);
      }
    }
    return errors;
  }

  function setFiles(files) {
    state.selectedFiles = Array.from(files || []);
    const errors = validateFiles(state.selectedFiles);
    showFileSelection();

    if (errors.length) {
      $("importUploadResults").innerHTML =
        `<div class="import-error">${errors.map(esc).join("<br>")}</div>`;
      $("importUploadBtn").disabled = true;
    } else {
      $("importUploadResults").innerHTML = "";
      $("importUploadBtn").disabled =
        state.uploading || state.selectedFiles.length === 0;
    }
  }

  function uploadOne(file, folderName, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const body = new FormData();
      body.append("file", file, file.name);
      body.append("folder_name", folderName || "");

      xhr.open("POST", "/api/mailings/imports", true);
      xhr.withCredentials = true;
      xhr.setRequestHeader("Accept", "application/json");

      xhr.upload.addEventListener("progress", event => {
        if (event.lengthComputable) {
          onProgress(Math.round(event.loaded / event.total * 100));
        }
      });

      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch {}

        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(data);
        } else {
          reject(new Error(
            data.detail || `Ошибка загрузки ${file.name}: HTTP ${xhr.status}`
          ));
        }
      };

      xhr.onerror = () => reject(new Error(`Сетевая ошибка: ${file.name}`));
      xhr.onabort = () => reject(new Error(`Загрузка отменена: ${file.name}`));
      xhr.send(body);
    });
  }

  function renderBatches() {
    const box = $("importBatchList");
    $("importBatchCount").textContent = String(state.batches.length);

    if (!state.batches.length) {
      box.innerHTML = '<p class="import-muted">Пока нет загруженных документов.</p>';
      return;
    }

    box.innerHTML = state.batches.map(batch => {
      const selected = Number(batch.id) === Number(state.selectedBatchId);
      return `
        <button type="button"
          class="import-batch-item ${selected ? "selected" : ""}"
          data-import-batch="${Number(batch.id)}">
          <span class="import-batch-name">${esc(batch.original_filename || batch.folder_name || "Документ")}</span>
          <span class="import-batch-meta">
            ${esc(batchStatus(batch.status))}
            · строк: ${Number(batch.actual_rows || 0)}
          </span>
          <span class="import-batch-meta">${dateText(batch.created_at)}</span>
        </button>`;
    }).join("");
  }

  function renderEmptyRows(batch) {
    let message = "В этом пакете пока нет распознанных строк.";
    if (batch.status === "uploaded") {
      message = "Документ загружен и ожидает обработки. GAB 02 сейчас выключен, поэтому автоматическая обработка ещё не запускалась.";
    } else if (batch.status === "failed") {
      message = "Обработка пакета завершилась ошибкой. Проверьте состояние workflow и журнал n8n.";
    } else if (batch.status === "processing") {
      message = "Документ сейчас обрабатывается.";
    }

    $("importRowsArea").innerHTML =
      `<p class="import-muted">${esc(message)}</p>`;
  }

  function renderRows(rows, batch) {
    const area = $("importRowsArea");

    if (!rows.length) {
      renderEmptyRows(batch);
      return;
    }

    const filters = `
      <div class="import-row-filters">
        <input id="importRowSearch" class="import-text-input"
          value="${esc(state.query)}" placeholder="Поиск по компании, ИНН, контакту">
        <select id="importRowSegment" class="import-text-input">
          <option value="all">Все строки</option>
          <option value="eligible_email">Есть email</option>
          <option value="eligible_phone">Есть телефон</option>
          <option value="below_threshold">Ниже порога</option>
          <option value="unknown">Не определено</option>
        </select>
        <button type="button" class="button secondary" id="importRowFilterBtn">
          Найти
        </button>
      </div>`;

    const tableRows = rows.map(row => {
      const email = row.effective_email || row.email ||
        (Array.isArray(row.emails) ? row.emails[0] : "");
      const phone = row.effective_phone || row.phone ||
        (Array.isArray(row.phones) ? row.phones[0] : "");
      const warnings = row.extraction_warnings;
      const hasWarnings = Array.isArray(warnings)
        ? warnings.length > 0
        : Boolean(warnings);

      return `
        <tr>
          <td><strong>${esc(row.company_name || "—")}</strong>
            <div class="import-muted">${esc(row.city || "")}</div></td>
          <td>${esc(row.inn || "—")}</td>
          <td>${esc(row.full_name || "—")}
            <div class="import-muted">${esc(row.position || "")}</div></td>
          <td>${esc(email || "—")}</td>
          <td>${esc(phone || "—")}</td>
          <td>${moneyText(row.revenue_rub)}
            <div class="import-muted">${esc(row.revenue_year || "")}</div></td>
          <td><span class="import-status">${esc(rowStatus(row.import_status))}</span>
            ${hasWarnings ? '<div class="import-warning">Есть предупреждения</div>' : ""}</td>
          <td><button class="button secondary import-preview-button"
            type="button" data-preview-row="${Number(row.id)}">Совпадения</button></td>
        </tr>`;
    }).join("");

    area.innerHTML = `
      ${filters}
      <div class="import-table-wrap">
        <table class="import-table">
          <thead><tr>
            <th>Компания</th><th>ИНН</th><th>Контакт</th>
            <th>Email</th><th>Телефон</th><th>Выручка</th>
            <th>Статус</th><th>Проверка</th>
          </tr></thead>
          <tbody>${tableRows}</tbody>
        </table>
      </div>
      <p class="import-muted">Показано строк: ${rows.length}. Для проверки совпадений нажмите соответствующую кнопку.</p>`;

    $("importRowSegment").value = state.segment;
  }

  async function loadRows() {
    const batch = state.batches.find(
      item => Number(item.id) === Number(state.selectedBatchId)
    );
    if (!batch) return;

    $("importRowsArea").innerHTML =
      '<p class="import-muted">Загружаем строки...</p>';

    const params = new URLSearchParams({
      limit: "200",
      offset: "0",
      segment: state.segment,
      q: state.query
    });

    const result = await request(
      `/api/mailings/imports/${Number(batch.id)}/rows?${params}`
    );

    const rows = Array.isArray(result.items) ? result.items : [];
    renderRows(rows, batch);
  }

  async function selectBatch(batchId) {
    state.selectedBatchId = Number(batchId);
    renderBatches();

    const batch = state.batches.find(
      item => Number(item.id) === state.selectedBatchId
    );
    if (!batch) return;

    $("importPreviewPanel").hidden = true;
    $("importDetailHeader").innerHTML = `
      <div class="import-detail-title">
        <div>
          <h2>${esc(batch.original_filename || batch.folder_name || "Документ")}</h2>
          <p class="import-muted">
            ${esc(batchStatus(batch.status))} · ${dateText(batch.created_at)}
          </p>
        </div>
        <a class="button secondary" target="_blank" rel="noopener"
          href="/api/mailings/imports/${Number(batch.id)}/file">Скачать оригинал</a>
      </div>`;

    await loadRows();
  }

  async function loadBatches(preferredId = null) {
    const result = await request("/api/mailings/imports?limit=100");
    state.batches = Array.isArray(result.items) ? result.items : [];

    state.batches.sort((a, b) => {
      const dateA = new Date(a.created_at || 0).getTime();
      const dateB = new Date(b.created_at || 0).getTime();
      return dateB - dateA || Number(b.id) - Number(a.id);
    });

    renderBatches();

    if (!state.batches.length) {
      state.selectedBatchId = null;
      $("importDetailHeader").innerHTML =
        "<h2>Результаты обработки</h2><p class='import-muted'>Загрузите первый документ.</p>";
      $("importRowsArea").innerHTML = "";
      return;
    }

    const wanted = preferredId || state.selectedBatchId;
    const selected = state.batches.find(
      item => Number(item.id) === Number(wanted)
    ) || state.batches[0];

    await selectBatch(selected.id);
  }

  function prettyList(value) {
    if (!value || (Array.isArray(value) && !value.length)) return "Нет данных";
    return JSON.stringify(value, null, 2);
  }

  async function showPreview(rowId) {
    const batchId = Number(state.selectedBatchId);
    $("importPreviewPanel").hidden = false;
    $("importPreviewContent").innerHTML =
      '<p class="import-muted">Проверяем совпадения...</p>';

    try {
      const data = await request(
        `/api/mailings/imports/${batchId}/rows/${Number(rowId)}/match-preview`
      );

      const reasons = Array.isArray(data.reasons) ? data.reasons : [];
      $("importPreviewContent").innerHTML = `
        <div class="import-preview-grid">
          <div><strong>Компания</strong><br>${esc(data.company_name || "—")}</div>
          <div><strong>ИНН</strong><br>${esc(data.inn || "—")}</div>
          <div><strong>Контакт</strong><br>${esc(data.full_name || "—")}</div>
          <div><strong>Статус проверки</strong><br>
            ${data.can_auto_confirm
              ? '<span class="import-status">Проверки пройдены</span>'
              : '<span class="import-warning">Требуется ручная проверка</span>'}
          </div>
        </div>
        <h3>Причины проверки</h3>
        ${reasons.length
          ? `<ul>${reasons.map(reason => `<li>${esc(typeof reason === "string" ? reason : JSON.stringify(reason))}</li>`).join("")}</ul>`
          : "<p class='import-muted'>Автоматические проверки не обнаружили блокирующих причин.</p>"}
        <details class="import-candidates">
          <summary>Кандидаты компаний</summary>
          <pre>${esc(prettyList(data.company_candidates))}</pre>
        </details>
        <details class="import-candidates">
          <summary>Кандидаты контактов</summary>
          <pre>${esc(prettyList(data.contact_candidates))}</pre>
        </details>
        <details class="import-candidates">
          <summary>Кандидаты инвесторов</summary>
          <pre>${esc(prettyList(data.investor_candidates))}</pre>
        </details>
        <p class="import-safety-note">
          Это только предпросмотр. Карточки CRM не создаются и не изменяются.
        </p>`;
      $("importPreviewPanel").scrollIntoView({behavior: "smooth", block: "nearest"});
    } catch (error) {
      $("importPreviewContent").innerHTML =
        `<div class="import-error">${esc(error.message)}</div>`;
    }
  }

  async function uploadFiles() {
    if (state.uploading || !state.selectedFiles.length) return;

    const errors = validateFiles(state.selectedFiles);
    if (errors.length) {
      notifyImport(errors.join("; "), true);
      return;
    }

    state.uploading = true;
    $("importUploadBtn").disabled = true;
    $("importProgress").hidden = false;
    $("importUploadResults").innerHTML = "";
    const files = [...state.selectedFiles];
    const folder = $("importFolderName").value.trim();
    const results = [];
    let lastId = null;
    let failed = false;

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      $("importProgressText").textContent =
        `Загружаем ${i + 1} из ${files.length}: ${file.name}`;

      try {
        const result = await uploadOne(file, folder, percent => {
          const total = Math.round(((i + percent / 100) / files.length) * 100);
          $("importProgressBar").style.width = total + "%";
        });

        const id = result.item && result.item.id;
        if (id) lastId = id;
        results.push(`<div class="import-success">✓ ${esc(file.name)} — загружен</div>`);
      } catch (error) {
        failed = true;
        results.push(`<div class="import-error">✕ ${esc(file.name)} — ${esc(error.message)}</div>`);
      }
    }

    $("importUploadResults").innerHTML = results.join("");
    $("importProgressBar").style.width = "100%";
    $("importProgressText").textContent = failed
      ? "Загрузка завершена с ошибками"
      : "Все документы загружены";

    state.uploading = false;
    state.selectedFiles = [];
    $("importFileInput").value = "";
    showFileSelection();

    try {
      await loadBatches(lastId);
    } catch (error) {
      notifyImport("Файлы загружены, но не удалось обновить список: " + error.message, true);
    }

    if (failed) notifyImport("Некоторые файлы не удалось загрузить. Проверьте сообщения.", true);
    else notifyImport("Документы загружены. Обработка пока не запускалась.");
  }

  function initialize() {
    if (state.initialized) return;
    state.initialized = true;

    $("importFileInput").addEventListener("change", event => {
      setFiles(event.target.files);
    });

    $("importUploadBtn").addEventListener("click", uploadFiles);
    $("importRefreshBtn").addEventListener("click", async () => {
      try {
        await loadBatches();
        notifyImport("Список импорта обновлён");
      } catch (error) {
        notifyImport(error.message, true);
      }
    });

    $("importBatchList").addEventListener("click", async event => {
      const button = event.target.closest("[data-import-batch]");
      if (!button) return;
      try {
        await selectBatch(button.dataset.importBatch);
      } catch (error) {
        notifyImport(error.message, true);
      }
    });

    $("importRowsArea").addEventListener("click", async event => {
      if (event.target.closest("#importRowFilterBtn")) {
        state.query = $("importRowSearch").value.trim();
        state.segment = $("importRowSegment").value;
        try { await loadRows(); }
        catch (error) { notifyImport(error.message, true); }
        return;
      }

      const previewButton = event.target.closest("[data-preview-row]");
      if (previewButton) await showPreview(previewButton.dataset.previewRow);
    });

    $("importRowsArea").addEventListener("keydown", async event => {
      if (event.key === "Enter" && event.target.id === "importRowSearch") {
        state.query = event.target.value.trim();
        try { await loadRows(); }
        catch (error) { notifyImport(error.message, true); }
      }
    });

    $("importClosePreview").addEventListener("click", () => {
      $("importPreviewPanel").hidden = true;
    });

    const drop = $("importDropZone");
    ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => {
      event.preventDefault();
      drop.classList.add("drag-over");
    }));
    ["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => {
      event.preventDefault();
      drop.classList.remove("drag-over");
    }));
    drop.addEventListener("drop", event => {
      setFiles(event.dataTransfer.files);
    });
  }

  window.loadImportsView = async function() {
    initialize();
    try {
      await loadBatches();
    } catch (error) {
      $("importBatchList").innerHTML =
        `<div class="import-error">${esc(error.message)}</div>`;
      notifyImport("Не удалось загрузить историю импорта: " + error.message, true);
    }
  };
})();
