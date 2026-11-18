const $ = id => document.getElementById(id);

let currentId = null;
let campaignItems = [];
let recipientItems = [];
let selectedRecipients = new Map();
let registryRecipientItems = [];
let selectedRegistryRecordIds = new Set();
let enrichmentResults = [];
let attachmentItems = [];

function htmlEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[ch]);
}

function toast(message) {
  const el = $("mailToast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...options, headers, credentials: "same-origin"
  });
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  }
  return data;
}

function currentPayload() {
  return {
    name: $("mailingName").value.trim(),
    subject: $("mailingSubject").value.trim(),
    body_html: $("mailingHtml").value,
    body_text: $("mailingText").value
  };
}

function showEditor(show) {
  $("emptyState").hidden = show;
  $("editor").hidden = !show;
}

function showRecipientFolders(view = "projects") {
  $("recipientFolderHome").hidden = false;
  $("crmRecipientsPanel").hidden = true;
  $("registryRecipientsPanel").hidden = true;
  $("folderProjectsView").hidden = view !== "projects";
  $("folderGabView").hidden = view !== "gab";
}

function openRecipientSource(source) {
  $("recipientFolderHome").hidden = true;
  $("crmRecipientsPanel").hidden = source !== "crm";
  $("registryRecipientsPanel").hidden = source !== "registry";
}

async function loadCampaigns() {
  const data = await api("/api/mailings/");
  campaignItems = data.items || [];
  $("campaignList").innerHTML = campaignItems.length
    ? campaignItems.map(item => `
      <button class="campaign-item ${item.id === currentId ? "active" : ""}"
              data-campaign="${item.id}">
        <strong>${htmlEscape(item.name)}</strong>
        <small>${htmlEscape(item.status)} · ${item.attachment_count} файлов · ${item.recipient_count} получателей</small>
      </button>
    `).join("")
    : '<p class="hint">Черновиков пока нет</p>';
}

async function createMailing() {
  const name = prompt("Название новой рассылки:");
  if (!name || !name.trim()) return;
  const data = await api("/api/mailings/", {
    method: "POST",
    body: JSON.stringify({name: name.trim()})
  });
  await openMailing(data.id);
  await loadCampaigns();
  toast("Черновик создан");
}

async function openMailing(id) {
  const data = await api(`/api/mailings/${id}`);
  const m = data.mailing;
  currentId = id;
  showRecipientFolders("projects");
  $("mailingName").value = m.name || "";
  $("mailingSubject").value = m.subject || "";
  $("mailingHtml").value = m.body_html || "";
  $("mailingText").value = m.body_text || "";
  attachmentItems = data.attachments || [];
  selectedRecipients = new Map();
  for (const r of (data.recipients || [])) {
    if (r.investor_id !== null && r.email) {
      const email = String(r.email).trim();
      const key = recipientSelectionKey(r.investor_id, email);
      selectedRecipients.set(key, {
        investor_id: Number(r.investor_id),
        email
      });
    }
  }

  selectedRegistryRecordIds = new Set(
    (data.recipients || [])
      .filter(r => r.review_record_id !== null)
      .map(r => String(r.review_record_id))
  );

  $("editorTitle").textContent = m.name || "Редактор письма";
  showEditor(true);
  renderAttachments();
  await loadRecipients();
  await loadRegistryRecipients();
  await loadCampaigns();
}

function renderAttachments() {
  $("attachmentCount").textContent = `${attachmentItems.length} файлов`;
  $("attachmentList").innerHTML = attachmentItems.length
    ? attachmentItems.map(file => `
      <div class="attachment-row">
        <div class="attachment-meta">
          <strong>${htmlEscape(file.original_filename)}</strong>
          <small>${(Number(file.size_bytes) / 1024 / 1024).toFixed(2)} МБ · ${htmlEscape(file.mime_type)}</small>
        </div>
        <a class="small-action"
           href="/api/mailings/${currentId}/attachments/${file.id}/download"
           target="_blank" rel="noopener">Открыть</a>
        <button class="small-action" data-delete-attachment="${file.id}">Удалить</button>
      </div>
    `).join("")
    : '<p class="hint">Вложений пока нет</p>';
}

async function saveMailing() {
  if (!currentId) return;
  const payload = currentPayload();
  if (!payload.name) throw new Error("Укажи название рассылки");
  await api(`/api/mailings/${currentId}`, {
    method: "PATCH", body: JSON.stringify(payload)
  });
  $("editorTitle").textContent = payload.name;
  $("saveMessage").textContent = "Черновик сохранён";
  await loadCampaigns();
  toast("Черновик сохранён");
}

async function loadRecipients() {
  if (!currentId) return;
  const q = $("recipientSearch").value.trim();
  const data = await api(`/api/mailings/recipients?q=${encodeURIComponent(q)}&limit=5000`);
  recipientItems = data.items || [];
  renderRecipients();
}

function recipientEmailKey(email) {
  return String(email || "").trim().toLowerCase();
}

function recipientSelectionKey(investorId, email) {
  return `${Number(investorId)}|${recipientEmailKey(email)}`;
}

function renderRecipients() {
  const withEmail = recipientItems.filter(r => r.email).length;
  const withoutEmail = recipientItems.length - withEmail;

  $("recipientList").innerHTML = recipientItems.length
    ? recipientItems.map(r => {
        const email = String(r.email || "").trim();
        const key = recipientSelectionKey(r.investor_id, email);
        const checked = email && selectedRecipients.has(key);

        return `
          <div class="recipient-row">
            <label>
              ${email ? `
                <input type="checkbox"
                       data-investor="${Number(r.investor_id)}"
                       data-email="${htmlEscape(email)}"
                       ${checked ? "checked" : ""}>
              ` : ""}
              <span class="recipient-main">
                <strong>${htmlEscape(r.investor_name || r.company_name || "Инвестор")}</strong>
                <small>${htmlEscape(r.company_name || "Компания не указана")} · ИНН: ${htmlEscape(r.inn || "—")}</small>
                <span class="recipient-email ${email ? "" : "missing"}">
                  ${email ? htmlEscape(email) : "Email не найден"}
                  ${r.email_source === "company" ? " · общий адрес компании" : ""}
                </span>
              </span>
            </label>
          </div>
        `;
      }).join("")
    : '<p class="hint">Получатели не найдены</p>';

  const uniqueSelectedEmails = new Set(
    [...selectedRecipients.values()].map(
      item => recipientEmailKey(item.email)
    )
  );
  $("recipientCount").textContent =
    `${uniqueSelectedEmails.size} уникальных email выбрано`;

  $("recipientSummary").textContent =
    `В списке адресов: ${recipientItems.length}; с email: ${withEmail}; без email: ${withoutEmail}`;
}

async function loadRegistryRecipients() {
  if (!currentId) return;
  const q = $("registryRecipientSearch").value.trim();
  const data = await api(
    `/api/mailings/registry-recipients?q=${encodeURIComponent(q)}&limit=500`
  );
  registryRecipientItems = data.items || [];
  renderRegistryRecipients();
}

function renderRegistryRecipients() {
  $("registryRecipientList").innerHTML = registryRecipientItems.length
    ? registryRecipientItems.map(r => {
        const id = String(r.record_id);
        const contracts = Array.isArray(r.source_records)
          ? r.source_records : [];

        const contractHtml = contracts.map(item => `
          <li>
            <strong>${htmlEscape(item.record_id)}</strong>
            · стр. ${htmlEscape(item.source_page ?? "—")}
            · строка ${htmlEscape(item.source_row ?? "—")}
            · объект: ${htmlEscape(item.internal_name || "не указан")}
          </li>
        `).join("");

        return `
          <div class="recipient-row registry-group-row">
            <label>
              <input type="checkbox"
                     data-review-record="${htmlEscape(id)}"
                     ${selectedRegistryRecordIds.has(id) ? "checked" : ""}>
              <span class="recipient-main">
                <strong>${htmlEscape(r.object_names || "Объект не указан")}</strong>
                <small>Исходная запись: ${htmlEscape(r.counterparty || "не указана")}</small>
                <small>Связанных договоров: ${Number(r.occurrences) || 1}</small>
                <span class="recipient-email missing">
                  Email не найден — требуется поиск
                </span>
              </span>
            </label>
            ${contracts.length ? `
              <details class="registry-contracts">
                <summary>Показать договоры (${contracts.length})</summary>
                <ul>${contractHtml}</ul>
              </details>
            ` : ""}
          </div>
        `;
      }).join("")
    : '<p class="hint">Контрагенты не найдены</p>';

  $("registryRecipientCount").textContent =
    `${selectedRegistryRecordIds.size} выбрано`;

  $("registryRecipientSummary").textContent =
    `Уникальных групп в списке: ${registryRecipientItems.length}; выбрано: ${selectedRegistryRecordIds.size}`;
}


function renderEnrichmentResults() {
  const target = $("registryEnrichmentResults");
  if (!target) return;

  if (!enrichmentResults.length) {
    target.innerHTML = "";
    return;
  }

  target.innerHTML = enrichmentResults.map(group => {
    const candidates = group.candidates || [];

    if (group.error) {
      return `
        <div class="enrichment-card">
          <strong>${htmlEscape(group.record_id)}</strong>
          <p class="hint">${htmlEscape(group.error)}</p>
        </div>`;
    }

    if (!candidates.length) {
      return `
        <div class="enrichment-card">
          <strong>${htmlEscape(group.source_name || group.record_id)}</strong>
          <p class="hint">Совпадений в DaData не найдено. Нужна ручная проверка.</p>
        </div>`;
    }

    return `
      <div class="enrichment-group">
        <h3>${htmlEscape(group.source_name || group.record_id)}</h3>
        <small class="hint">
          Исходная запись: ${htmlEscape(group.record_id)}
          · запрос: ${htmlEscape(group.search_query || "")}
        </small>

        ${candidates.map(entry => {
          const c = entry.candidate || {};
          const checked = entry.checko || null;
          const confirmed = checked && checked.match_status === "confirmed";

          const checkoHtml = checked ? `
            <div class="enrichment-details">
              <div><b>Данные Checko</b></div>
              <div>Название: ${htmlEscape(checked.legal_name || "—")}</div>
              <div>Статус: ${htmlEscape(checked.status_name || "—")}</div>
              <div>ОКВЭД: ${htmlEscape(
                [checked.okved_code, checked.okved_name].filter(Boolean).join(" — ") || "—"
              )}</div>
              <div>Регион: ${htmlEscape(checked.region || "—")}</div>
              <div>Адрес: ${htmlEscape(checked.legal_address || "—")}</div>
              <div>Руководитель: ${htmlEscape(checked.director_name || "—")}</div>
              <div>Сайт: ${htmlEscape(checked.website || "—")}</div>
              <div>Телефоны: ${htmlEscape(
                Array.isArray(checked.phones) && checked.phones.length
                  ? checked.phones.join(", ") : "не найдены"
              )}</div>
              <div>Email: ${htmlEscape(
                Array.isArray(checked.emails) && checked.emails.length
                  ? checked.emails.join(", ") : "не найдены"
              )}</div>
              <div>Выручка: ${
                checked.revenue_rub != null
                  ? Number(checked.revenue_rub).toLocaleString("ru-RU") + " ₽ за " + htmlEscape(checked.revenue_year)
                  : "не найдена"
              }</div>
              <p class="hint">
                Контакты получены из Checko. Их актуальность и принадлежность компании
                нужно проверить перед рассылкой.
              </p>
              ${confirmed
                ? '<span class="enrichment-confirmed">Совпадение подтверждено</span>'
                : `<button class="button primary"
                    data-confirm-record="${htmlEscape(group.record_id)}"
                    data-confirm-inn="${htmlEscape(c.inn)}">
                    Подтвердить эту компанию
                   </button>`
              }
            </div>
          ` : "";

          return `
            <div class="enrichment-candidate">
              <strong>${htmlEscape(c.legal_name || "Название не указано")}</strong>
              <div>ИНН: ${htmlEscape(c.inn || "—")}</div>
              <div>ОГРН/ОГРНИП: ${htmlEscape(c.ogrn || "—")}</div>
              <div>Статус DaData: ${htmlEscape(c.status_name || "—")}</div>
              <div>Регион: ${htmlEscape(c.region || "—")}</div>
              <div>Адрес: ${htmlEscape(c.legal_address || "—")}</div>
              <div>ОКВЭД: ${htmlEscape(c.okved_code || "—")}</div>
              <div class="enrichment-actions">
                <button class="button secondary"
                  data-checko-record="${htmlEscape(group.record_id)}"
                  data-checko-inn="${htmlEscape(c.inn)}"
                  ${checked ? "disabled" : ""}>
                  ${checked ? "Карточка Checko получена" : "Получить карточку Checko"}
                </button>
              </div>
              ${checkoHtml}
            </div>
          `;
        }).join("")}
      </div>`;
  }).join("");
}

async function searchSelectedRegistry() {
  const ids = [...selectedRegistryRecordIds];

  if (!ids.length) {
    toast("Сначала выбери контрагентов в папке реестра");
    return;
  }

  const button = $("enrichSelectedRegistry");
  button.disabled = true;
  enrichmentResults = [];
  renderEnrichmentResults();

  const batchSize = 10;
  const total = ids.length;
  let processed = 0;
  let failed = 0;

  $("enrichmentStatus").textContent =
    `Начинаем поиск ${total} контрагентов...`;

  try {
    for (let i = 0; i < ids.length; i += batchSize) {
      const batch = ids.slice(i, i + batchSize);

      $("enrichmentStatus").textContent =
        `DaData: обработка ${Math.min(i + batch.length, total)} из ${total}...`;

      try {
        const result = await api("/api/mailings/registry-enrichment/search", {
          method: "POST",
          body: JSON.stringify({record_ids: batch})
        });

        enrichmentResults.push(...(result.items || []));
        processed += batch.length;
        renderEnrichmentResults();
      } catch (error) {
        failed += batch.length;
        processed += batch.length;
        enrichmentResults.push({
          record_id: batch.join(", "),
          error: `Не удалось обработать эту партию: ${error.message}`
        });
        renderEnrichmentResults();
      }

      $("enrichmentStatus").textContent =
        `Обработано ${processed} из ${total}; ошибок в партиях: ${failed}.`;
    }

    const found = enrichmentResults.reduce(
      (sum, item) => sum + (item.candidates || []).length, 0
    );

    $("enrichmentStatus").textContent =
      `Готово: ${processed} из ${total}; найдено вариантов: ${found}; ошибок в партиях: ${failed}.`;
  } finally {
    button.disabled = false;
  }
}

async function getCheckoDetails(recordId, inn, button) {
  button.disabled = true;
  button.textContent = "Запрашиваем Checko...";

  try {
    const result = await api(
      `/api/mailings/registry-enrichment/${encodeURIComponent(recordId)}/checko`,
      {
        method: "POST",
        body: JSON.stringify({inn})
      }
    );

    const group = enrichmentResults.find(x => x.record_id === recordId);
    const entry = group?.candidates?.find(
      x => String(x.candidate?.inn) === String(inn)
    );

    if (entry) entry.checko = result.item;

    renderEnrichmentResults();
    toast("Карточка Checko получена");
  } catch (error) {
    button.disabled = false;
    button.textContent = "Повторить запрос Checko";
    toast(error.message);
  }
}

async function confirmEnrichment(recordId, inn, button) {
  if (!confirm(
    `Подтвердить связь записи ${recordId} с ИНН ${inn}?`
  )) return;

  button.disabled = true;

  try {
    await api(
      `/api/mailings/registry-enrichment/${encodeURIComponent(recordId)}/confirm`,
      {
        method: "POST",
        body: JSON.stringify({inn})
      }
    );

    const group = enrichmentResults.find(x => x.record_id === recordId);
    const entry = group?.candidates?.find(
      x => String(x.candidate?.inn) === String(inn)
    );
    if (entry?.checko) entry.checko.match_status = "confirmed";

    renderEnrichmentResults();
    toast("Совпадение подтверждено и сохранено");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

async function saveRecipients() {
  if (!currentId) return;
  const result = await api(`/api/mailings/${currentId}/recipients`, {
    method: "POST",
    body: JSON.stringify({
      recipients: [...selectedRecipients.values()],
      review_record_ids: [...selectedRegistryRecordIds]
    })
  });
  $("recipientSummary").textContent =
    `Сохранено: ${result.selected}; с email: ${result.with_email}; без email: ${result.without_email}`;
  await loadCampaigns();
  $("registryRecipientSummary").textContent =
    `Сохранено записей реестра: ${result.registry_selected}; у них пока нет email`;
  toast("Получатели сохранены");
}

function previewMailing() {
  const body = $("mailingHtml").value ||
    `<pre style="white-space:pre-wrap;font-family:Arial">${htmlEscape($("mailingText").value)}</pre>`;
  const subject = htmlEscape($("mailingSubject").value);
  $("previewFrame").srcdoc = `
    <!doctype html><html><head><meta charset="utf-8">
    <style>body{font-family:Arial,sans-serif;color:#263746;padding:24px;line-height:1.6}
    h3{color:#7b8792;font-size:12px;font-weight:normal}</style>
    </head><body><h3>${subject}</h3>${body}</body></html>`;
  $("previewPanel").hidden = false;
  $("previewPanel").scrollIntoView({behavior:"smooth",block:"start"});
}

$("newMailing").addEventListener("click", () => createMailing().catch(e => toast(e.message)));
$("emptyNewMailing").addEventListener("click", () => createMailing().catch(e => toast(e.message)));

$("campaignList").addEventListener("click", event => {
  const button = event.target.closest("[data-campaign]");
  if (button) openMailing(Number(button.dataset.campaign)).catch(e => toast(e.message));
});

$("saveMailing").addEventListener("click", () => saveMailing().catch(e => toast(e.message)));
$("previewMailing").addEventListener("click", previewMailing);
$("closePreview").addEventListener("click", () => $("previewPanel").hidden = true);
$("searchRecipients").addEventListener("click", () => loadRecipients().catch(e => toast(e.message)));
$("recipientSearch").addEventListener("keydown", event => {
  if (event.key === "Enter") loadRecipients().catch(e => toast(e.message));
});
$("saveRecipients").addEventListener("click", () => saveRecipients().catch(e => toast(e.message)));

$("recipientList").addEventListener("change", event => {
  const input = event.target.closest("[data-investor][data-email]");
  if (!input) return;

  const id = Number(input.dataset.investor);
  const email = String(input.dataset.email || "").trim();
  const key = recipientSelectionKey(id, email);

  if (!id || !email) return;

  if (input.checked) {
    selectedRecipients.set(key, {
      investor_id: id,
      email
    });
  } else {
    selectedRecipients.delete(key);
  }

  renderRecipients();
});


$("selectAllRegistryRecipients").addEventListener("click", () => {
  for (const item of registryRecipientItems) {
    selectedRegistryRecordIds.add(String(item.record_id));
  }
  renderRegistryRecipients();
});

$("clearRegistryRecipients").addEventListener("click", () => {
  selectedRegistryRecordIds.clear();
  renderRegistryRecipients();
});

$("saveRegistryRecipients").addEventListener(
  "click", () => saveRecipients().catch(e => toast(e.message))
);

$("searchRegistryRecipients").addEventListener(
  "click", () => loadRegistryRecipients().catch(e => toast(e.message))
);

$("registryRecipientSearch").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    loadRegistryRecipients().catch(e => toast(e.message));
  }
});

$("registryRecipientList").addEventListener("change", event => {
  const input = event.target.closest("[data-review-record]");
  if (!input) return;

  const id = String(input.dataset.reviewRecord);
  if (input.checked) selectedRegistryRecordIds.add(id);
  else selectedRegistryRecordIds.delete(id);

  renderRegistryRecipients();
});

$("recipientFolderHome").addEventListener("click", event => {
  const button = event.target.closest("[data-folder-action]");
  if (!button) return;

  const action = button.dataset.folderAction;

  if (action === "open-project") {
    showRecipientFolders("gab");
  } else if (action === "back-projects") {
    showRecipientFolders("projects");
  } else if (action === "open-source") {
    openRecipientSource(button.dataset.source);
  }
});

$("backFromCrmRecipients").addEventListener("click", () => {
  showRecipientFolders("gab");
});

$("backFromRegistryRecipients").addEventListener("click", () => {
  showRecipientFolders("gab");
});


$("enrichSelectedRegistry").addEventListener(
  "click", () => searchSelectedRegistry()
);

$("registryEnrichmentResults").addEventListener("click", event => {
  const checkoButton = event.target.closest("[data-checko-inn]");
  if (checkoButton) {
    getCheckoDetails(
      String(checkoButton.dataset.checkoRecord),
      String(checkoButton.dataset.checkoInn),
      checkoButton
    );
    return;
  }

  const confirmButton = event.target.closest("[data-confirm-inn]");
  if (confirmButton) {
    confirmEnrichment(
      String(confirmButton.dataset.confirmRecord),
      String(confirmButton.dataset.confirmInn),
      confirmButton
    );
  }
});

$("attachmentInput").addEventListener("change", async event => {
  if (!currentId) return;
  const files = [...event.target.files];
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      const uploaded = await api(`/api/mailings/${currentId}/attachments`, {
        method: "POST", body: form
      });
      attachmentItems.push(uploaded);
      renderAttachments();
      toast(`Загружен файл: ${file.name}`);
    } catch (error) {
      toast(`${file.name}: ${error.message}`);
    }
  }
  event.target.value = "";
});

$("attachmentList").addEventListener("click", async event => {
  const button = event.target.closest("[data-delete-attachment]");
  if (!button) return;
  if (!confirm("Удалить это вложение?")) return;
  try {
    await api(`/api/mailings/${currentId}/attachments/${button.dataset.deleteAttachment}`, {
      method: "DELETE"
    });
    attachmentItems = attachmentItems.filter(
      f => String(f.id) !== button.dataset.deleteAttachment
    );
    renderAttachments();
    await loadCampaigns();
    toast("Вложение удалено");
  } catch (error) {
    toast(error.message);
  }
});

(async function init() {
  try {
    await loadCampaigns();
  } catch (error) {
    toast(error.message);
  }
})();



$("sendTestMail").addEventListener("click", async () => {
  if (!currentId) {
    toast("Сначала создай или открой черновик");
    return;
  }

  const input = $("testRecipientEmail");
  const toEmail = input.value.trim();

  if (!input.checkValidity() || !toEmail) {
    input.reportValidity();
    return;
  }

  const button = $("sendTestMail");
  button.disabled = true;
  $("testSendMessage").textContent = "Отправляем тестовое письмо...";

  try {
    await saveMailing();

    const result = await api(`/api/mailings/${currentId}/test-send`, {
      method: "POST",
      body: JSON.stringify({to_email: toEmail})
    });

    $("testSendMessage").textContent =
      `Отправлено на ${result.to_email}; вложений: ${result.attachment_count}`;

    toast("Тестовое письмо отправлено");
    await loadCampaigns();
  } catch (error) {
    $("testSendMessage").textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
