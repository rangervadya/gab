(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const MAX_SIZE = 20 * 1024 * 1024;
  const ALLOWED = ["pdf", "xls", "xlsx", "doc", "docx", "jpg", "jpeg", "png", "tif", "tiff", "csv", "pptx"];

  const state = {
    batches: [],
    rows: [],
    selectedBatchId: null,
    selectedFiles: [],
    uploading: false,
    initialized: false,
    query: "",
    offset: 0,
    total: 0,
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
      message = "Документ загружен и ожидает обработки. Обновите список для проверки результата.";
    } else if (batch.status === "failed") {
      message = "Ошибка обработки: " + (batch.error_message || "Проверьте журнал n8n");
    } else if (batch.status === "processing") {
      message = "Документ сейчас обрабатывается.";
    }

    $("importRowsArea").innerHTML =
      `<p class="import-muted">${esc(message)}</p>`;
  }

  function renderRows(rows, batch) {
    const area = $("importRowsArea");

    if (!rows.length && !state.query && state.segment === "all" && state.offset === 0) {
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
          <td><button class="button secondary" type="button" data-edit-import="${Number(row.id)}">Изменить</button><button class="button secondary import-preview-button"
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
          <tbody>${tableRows || '<tr><td colspan="8">Нет строк по выбранному фильтру</td></tr>'}</tbody>
        </table>
      </div>
      <p class="import-muted">Показано строк: ${rows.length} из ${state.total}.</p>
      <div class="import-actions">
        <button class="button secondary" type="button" data-import-page="prev" ${state.offset === 0 ? "disabled" : ""}>Назад</button>
        <button class="button secondary" type="button" data-import-page="next" ${state.offset + rows.length >= state.total ? "disabled" : ""}>Далее</button>
      </div>`;

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
      offset: String(state.offset),
      segment: state.segment,
      q: state.query
    });

    const result = await request(
      `/api/mailings/imports/${Number(batch.id)}/rows?${params}`
    );

    const rows = Array.isArray(result.items) ? result.items : [];
    state.rows = rows;
    state.total = Number(result.total || 0);
    renderRows(rows, batch);
  }

  async function selectBatch(batchId) {
    state.selectedBatchId = Number(batchId);
    state.offset = 0;
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
        ${batch.status === "failed" ? `<button type="button" class="button secondary" id="importRetryBtn">Повторить обработку</button>` : ""}
        <a class="button secondary" target="_blank" rel="noopener"
          href="/api/mailings/imports/${Number(batch.id)}/file">Скачать оригинал</a>
      </div>`;

    const retry = $("importRetryBtn");
    if (retry) retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        await request(`/api/mailings/imports/${Number(batch.id)}/retry`, {method: "POST"});
        await loadBatches(batch.id);
      } catch (error) { notifyImport(error.message, true); retry.disabled = false; }
    });
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

  const reasonLabels = {
  "identity_missing": "Не указаны компания и ФИО",
  "no_contact_details_requires_review": "Нет контактных данных",
  "reserved_test_email_domain_requires_review": "Email относится к тестовому домену",
  "revenue_status_not_eligible": "Финансовая проверка не пройдена",
  "revenue_amount_missing_or_invalid": "Выручка не указана или некорректна",
  "revenue_below_100m": "Выручка ниже обязательного порога 100 млн ₽",
  "revenue_year_missing_or_invalid": "Не указан корректный год выручки",
  "revenue_source_missing": "Не указан источник выручки",
  "extraction_warnings_require_manual_review": "Проверьте предупреждения извлечения в исходных данных",
  "invalid_inn_requires_review": "Неверная контрольная сумма ИНН",
  "inn_missing_requires_review": "Не указан ИНН",
  "inn_and_company_name_point_to_different_companies": "ИНН и название указывают на разные компании",
  "multiple_companies_with_same_inn": "Найдены несколько компаний с одинаковым ИНН",
  "multiple_companies_with_same_name": "Найдены несколько компаний с одинаковым названием",
  "company_name_match_has_different_inn": "У компании с таким названием другой ИНН",
  "company_name_match_requires_confirmation": "Совпадение по названию требует выбора компании",
  "multiple_contacts_match_email_or_phone": "Email или телефон совпадает у нескольких контактов",
  "contact_company_not_matched_to_import_company": "Компания найденного контакта не сопоставлена",
  "contact_belongs_to_different_company": "Контакт принадлежит другой компании",
  "contact_name_conflicts_with_matched_email_or_phone": "У найденного по email или телефону контакта другое ФИО",
  "contact_name_match_requires_confirmation": "Совпадение по ФИО требует выбора контакта",
  "multiple_investors_match": "Найдены несколько инвесторов, выберите карточку",
  "investor_has_another_primary_contact": "У инвестора другой основной контакт"
};
  const reasonText = reason => reasonLabels[reason] || String(reason);

  async function showPreview(rowId, decisions = {}) {
    const batchId = Number(state.selectedBatchId);
    $("importPreviewPanel").hidden = false;
    $("importPreviewContent").innerHTML =
      '<p class="import-muted">Проверяем совпадения...</p>';

    try {
      const data = await request(
        `/api/mailings/imports/${batchId}/rows/${Number(rowId)}/match-preview`,
        {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({decisions})}
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
          ? `<ul>${reasons.map(reason => `<li>${esc(typeof reason === "string" ? reasonText(reason) : JSON.stringify(reason))}</li>`).join("")}</ul>`
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
        <h3>План переноса</h3>
        <p>${esc(data.plan.merge)}</p>
        <p>Компания: ${esc({create:"создать",match:"сопоставить",none:"не создаётся"}[data.plan.company])} · контакт: ${esc({create:"создать",match:"сопоставить",none:"не создаётся"}[data.plan.contact])} · инвестор: ${esc({create:"создать",match:"сопоставить"}[data.plan.investor])}</p>
        <p>Финансовая проверка: ${esc(data.row.revenue_status)} · ${moneyText(data.row.revenue_rub)} · ${esc(data.row.revenue_year || "—")} · ${esc(data.row.revenue_source || "—")}</p>
        <details><summary>Данные и ручные исправления</summary><pre class="import-raw">${esc(prettyList(data.row))}</pre></details>
        <form id="importConfirmForm">
          ${["company", "contact", "investor"].map(kind => `
            <label>${esc({company: "Компания", contact: "Контакт", investor: "Инвестор"}[kind])}
              <select class="import-text-input" name="${kind}_id">
                <option value="auto">Предложение системы: ${esc(data["suggested_" + kind + "_id"] || "новая карточка")}</option>
                <option value="new">Создать новую карточку</option>
                ${(data[kind + "_candidates"] || []).map(item => `<option value="${Number(item.id)}">№${Number(item.id)} ${esc(item.company_name || item.full_name || item.name)}</option>`).join("")}
              </select>
            </label>`).join("")}
          <button type="button" class="button secondary" id="importRecheckPlan">Обновить план с выбранными карточками</button>
          ${reasons.map((reason, i) => (data.acknowledgeable_reasons || []).includes(reason)
            ? `<label class="import-ack"><input type="checkbox" name="warning_${i}">Разрешаю продолжить с предупреждением: ${esc(reasonText(reason))}</label>`
            : `<p class="import-warning">Блокирует перенос: ${esc(reasonText(reason))}</p>`).join("")}
          <p class="import-muted">Финансовые ограничения и противоречивые связи остаются блокирующими.</p>
          <label class="import-ack"><input type="checkbox" name="reviewed" required>Проверил данные и план. Подтверждаю перенос в CRM.</label>
          <button class="button" type="submit">Подтвердить перенос</button>
          <p id="importConfirmStatus" role="status"></p>
        </form>`;
      const form = $("importConfirmForm");
      for (const [key, value] of Object.entries(decisions)) form.elements[key].value = value === null ? "new" : String(value);
      const getDecisions = () => Object.fromEntries(["company_id", "contact_id", "investor_id"]
        .filter(key => form.elements[key].value !== "auto")
        .map(key => [key, form.elements[key].value === "new" ? null : Number(form.elements[key].value)]));
      $("importRecheckPlan").addEventListener("click", () => showPreview(rowId, getDecisions()));
      form.addEventListener("change", event => {
        if (["company_id", "contact_id", "investor_id"].includes(event.target.name)) {
          form.querySelector('[type="submit"]').disabled = true;
          $("importConfirmStatus").textContent = "Обновите план после выбора карточек";
        }
      });
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
          const result = await request(`/api/mailings/imports/${batchId}/rows/${Number(rowId)}/confirm`, {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({version: data.version, decisions: getDecisions(),
              expected_plan: Object.fromEntries(["company_id", "contact_id", "investor_id"].map(key => [key, data["suggested_" + key]])),
              acknowledged_warnings: reasons.filter((_, i) => form.elements["warning_" + i]?.checked)})
          });
          $("importConfirmStatus").textContent = result.status === "needs_review"
            ? "Перенос заблокирован: " + (result.reasons || []).map(reasonText).join("; ")
            : `Перенос выполнен. Компания №${result.company_id || "—"}, контакт №${result.contact_id || "—"}, инвестор №${result.investor_id}.`;
          await loadRows();
        } catch (error) { $("importConfirmStatus").textContent = error.message; }
      });
      $("importPreviewPanel").scrollIntoView({behavior: "smooth", block: "nearest"});
    } catch (error) {
      $("importPreviewContent").innerHTML =
        `<div class="import-error">${esc(error.message)}</div>`;
    }
  }

  const editFields = {
    company_name: "Компания", inn: "ИНН", city: "Город", full_name: "ФИО",
    position: "Должность", phone: "Телефон", email: "Email", revenue_rub: "Выручка, ₽",
    revenue_year: "Год выручки", revenue_source: "Источник выручки", notes: "Комментарий"
  };

  async function showEditor(rowId) {
    const row = state.rows.find(item => Number(item.id) === rowId);
    if (!row) return;
    const panel = $("importPreviewPanel");
    panel.hidden = false;
    const readonly = !["pending", "needs_review"].includes(row.import_status);
    $("importPreviewContent").innerHTML = `
      <h3>Редактирование строки №${rowId}</h3>
      <form id="importEditForm">
        <div class="import-preview-grid">${Object.entries(editFields).map(([key, label]) => `
          <label>${esc(label)}<input class="import-text-input" name="${key}"
            type="${key === "email" ? "email" : "text"}" value="${esc(row[key] ?? "")}"
            ${readonly ? "disabled" : ""}></label>`).join("")}</div>
        <p id="importSaveStatus" role="status">${readonly ? "Строка закрыта для изменений" : "Изменения ещё не сохранены"}</p>
        <button class="button" type="submit" ${readonly ? "disabled" : ""}>Сохранить</button>
        <button class="button secondary" type="reset">Отменить изменения</button>
      </form>
      <details><summary>Исходные данные OCR</summary><pre class="import-raw">${esc(prettyList(row.raw_data))}</pre></details>
      <div id="importEditHistory">Загрузка истории…</div>`;
    const form = $("importEditForm");
    form.addEventListener("reset", () => { $("importSaveStatus").textContent = "Изменения отменены"; });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      const changes = Object.fromEntries(Object.keys(editFields)
        .filter(key => form.elements[key].value !== String(row[key] ?? ""))
        .map(key => [key, form.elements[key].value]));
      if (!Object.keys(changes).length) {
        $("importSaveStatus").textContent = "Нет изменений";
        button.disabled = false;
        return;
      }
      $("importSaveStatus").textContent = "Сохраняем…";
      try {
        const result = await request(`/api/mailings/imports/${state.selectedBatchId}/rows/${rowId}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({version: row.edit_version, changes})
        });
        Object.assign(row, result.item);
        await loadRows();
        await showEditor(rowId);
        $("importSaveStatus").textContent = "Сохранено";
      } catch (error) {
        $("importSaveStatus").textContent = error.message;
        button.disabled = false;
      }
    });
    try {
      const history = await request(`/api/mailings/imports/${state.selectedBatchId}/rows/${rowId}/edits`);
      if ($("importEditForm") === form) $("importEditHistory").innerHTML = `<h4>История исправлений</h4>${history.items.map(item =>
        `<details><summary>${dateText(item.created_at)} · ${esc(item.actor)}</summary><pre class="import-raw">${esc(prettyList(item.changes))}</pre></details>`
      ).join("") || "Исправлений пока нет"}`;
    } catch (error) {
      if ($("importEditForm") === form) $("importEditHistory").textContent = error.message;
    }
    panel.scrollIntoView({behavior: "smooth", block: "nearest"});
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
      $("importUploadResults").innerHTML = results.join("");
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
      const pageButton = event.target.closest("[data-import-page]");
      if (pageButton) {
        state.offset = Math.max(0, state.offset + (pageButton.dataset.importPage === "next" ? 200 : -200));
        try { await loadRows(); } catch (error) { notifyImport(error.message, true); }
        return;
      }
      if (event.target.closest("#importRowFilterBtn")) {
        state.offset = 0;
        state.query = $("importRowSearch").value.trim();
        state.segment = $("importRowSegment").value;
        try { await loadRows(); }
        catch (error) { notifyImport(error.message, true); }
        return;
      }

      const editButton = event.target.closest("[data-edit-import]");
      if (editButton) { await showEditor(Number(editButton.dataset.editImport)); return; }
      const previewButton = event.target.closest("[data-preview-row]");
      if (previewButton) await showPreview(previewButton.dataset.previewRow);
    });

    $("importRowsArea").addEventListener("keydown", async event => {
      if (event.key === "Enter" && event.target.id === "importRowSearch") {
        state.offset = 0;
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
