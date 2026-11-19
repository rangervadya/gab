const $ = id => document.getElementById(id);

const state = {
  view: "dashboard",
  page: 1,
  pageSize: 30,
  query: "",
  entity: null,
  items: [],
  editingId: null
};

const labels = {
  cold: "Холодный",
  warm: "Тёплый",
  hot: "Горячий",
  not_contacted: "Не контактировали",
  awaiting_reply: "Ожидаем ответ",
  replied: "Ответил",
  no_response: "Нет ответа",
  unreachable: "Нет контакта",
  new: "Новый",
  email: "Письмо",
  call: "Звонок",
  conversation: "Переговоры",
  qualification: "Квалификация",
  passed_to_director: "Передан директору",
  negotiation: "Сделка / переговоры",
  deal: "Сделка заключена",
  refusal: "Отказ",
  nurture: "Отложен",
  up_to_3_months: "До 3 месяцев",
  "3_to_6_months": "3–6 месяцев",
  "6_to_12_months": "6–12 месяцев",
  over_12_months: "Более года",
  unknown: "Не определён",
  shopping_center: "Торговый центр",
  commercial_space: "Коммерческое помещение",
  land: "Земельный участок",
  logistics: "Логистика",
  industrial: "Промышленность",
  other: "Другое",
  draft: "Черновик",
  active: "Активный",
  reserved: "Зарезервирован",
  sold: "Продан",
  archived: "Архив",
  inbound: "Входящее",
  outbound: "Исходящее",
  telegram: "Telegram",
  meeting: "Встреча",
  follow_up: "Повторный контакт",
  open: "Открыта",
  in_progress: "В работе",
  done: "Выполнена",
  cancelled: "Отменена",
  low: "Низкий",
  normal: "Обычный",
  high: "Высокий"
};

const field = (key, label, type = "text", extra = {}) =>
  ({key, label, type, ...extra});

const select = (key, label, options, extra = {}) =>
  field(key, label, "select", {options, ...extra});

const relation = (key, label, entity, extra = {}) =>
  field(key, label, "relation", {entity, ...extra});

const forms = {
  companies: {
    title: "Компании",
    subtitle: "Компании и организации инвесторов",
    name: "company_name",
    relationLabel: "company_name",
    columns: [
      ["company_name", "Компания"],
      ["inn", "ИНН"],
      ["city", "Город"],
      ["general_email", "Email"],
      ["general_phone", "Телефон"]
    ],
    fields: [
      field("company_name", "Название компании", "text", {required:true}),
      field("inn", "ИНН"),
      field("city", "Город"),
      field("website", "Сайт", "url"),
      field("general_email", "Общий email", "email"),
      field("general_phone", "Общий телефон", "tel"),
      field("emails", "Дополнительные email (JSON)", "json", {
        full:true,
        placeholder:'["investor@example.com","office@example.com"]'
      }),
      field("phones", "Дополнительные телефоны (JSON)", "json", {
        full:true,
        placeholder:'["+7 900 000-00-00","+7 900 000-00-01"]'
      }),
      field("notes", "Примечания", "textarea", {full:true})
    ]
  },

  contacts: {
    title: "Контакты",
    subtitle: "Лица, принимающие решения, и представители компаний",
    relationLabel: "full_name",
    columns: [
      ["full_name", "ФИО"],
      ["position", "Должность"],
      ["phone", "Телефон"],
      ["email", "Email"]
    ],
    fields: [
      field("full_name", "ФИО", "text", {required:true}),
      relation("company_id", "Компания", "companies"),
      field("position", "Должность"),
      field("phone", "Основной телефон", "tel"),
      field("email", "Основной email", "email"),
      field("emails", "Дополнительные email (JSON)", "json", {
        full:true,
        placeholder:'["investor@example.com","office@example.com"]'
      }),
      field("phones", "Дополнительные телефоны (JSON)", "json", {
        full:true,
        placeholder:'["+7 900 000-00-00","+7 900 000-00-01"]'
      }),
      field("city", "Город"),
      field("is_decision_maker", "Принимает решение", "checkbox"),
      field("notes", "Примечания", "textarea", {full:true})
    ]
  },

  assets: {
    title: "Инвестиционные объекты",
    subtitle: "Каталог объектов GAB Invest",
    relationLabel: "title",
    columns: [
      ["title", "Объект"],
      ["asset_type", "Тип"],
      ["city", "Город"],
      ["price_rub", "Цена, ₽"],
      ["asset_status", "Статус"]
    ],
    fields: [
      field("title", "Название объекта", "text", {required:true}),
      select("asset_type", "Тип объекта", [
        ["shopping_center","Торговый центр"],
        ["commercial_space","Коммерческое помещение"],
        ["land","Земельный участок"],
        ["logistics","Логистика"],
        ["industrial","Промышленность"],
        ["other","Другое"]
      ]),
      field("city", "Город"),
      field("address", "Адрес"),
      field("price_rub", "Цена, ₽", "number", {min:0}),
      field("area_sqm", "Площадь, кв. м", "number", {min:0}),
      field("annual_income_rub", "Годовой доход, ₽", "number"),
      field("expected_yield_pct", "Доходность, %", "number"),
      select("asset_status", "Статус объекта", [
        ["draft","Черновик"],
        ["active","Активный"],
        ["reserved","Зарезервирован"],
        ["sold","Продан"],
        ["archived","Архив"]
      ]),
      field("description", "Описание", "textarea", {full:true}),
      field("key_data", "Дополнительные данные JSON", "json", {
        full:true,
        placeholder:'{"источник":"презентация"}'
      })
    ]
  },

  investors: {
    title: "Инвесторы",
    subtitle: "База инвесторов и квалификация 4 из 5",
    relationLabel: "name",
    columns: [
      ["name", "Инвестор"],
      ["temperature", "Температура"],
      ["pipeline_stage", "Этап сделки"],
      ["response_status", "Ответ"],
      ["qualification_score", "Квалификация"],
      ["budget_max_rub", "Бюджет до, ₽"]
    ],
    fields: [
      field("name", "Имя инвестора / название", "text", {required:true}),
      relation("company_id", "Компания", "companies"),
      relation("primary_contact_id", "Основной контакт", "contacts"),
      field("budget_min_rub", "Бюджет от, ₽", "number"),
      field("budget_max_rub", "Бюджет до, ₽", "number"),
      field("source_of_funds", "Источник средств", "textarea", {full:true}),
      field("preferred_asset_types", "Интересующие типы объектов JSON", "json", {
        full:true,
        placeholder:'["shopping_center","land"]'
      }),
      relation("interested_asset_id", "Интересующий объект", "assets"),
      select("investment_horizon", "Срок инвестирования", [
        ["up_to_3_months","До 3 месяцев"],
        ["3_to_6_months","3–6 месяцев"],
        ["6_to_12_months","6–12 месяцев"],
        ["over_12_months","Более года"],
        ["unknown","Не определён"]
      ]),
      field("motivation", "Мотивация", "textarea", {full:true}),
      select("temperature", "Температура инвестора", [
        ["cold","Холодный"],
        ["warm","Тёплый"],
        ["hot","Горячий"]
      ]),
      select("response_status", "Статус ответа", [
        ["not_contacted","Не контактировали"],
        ["awaiting_reply","Ожидаем ответ"],
        ["replied","Ответил"],
        ["no_response","Нет ответа"],
        ["unreachable","Нет контакта"]
      ]),
      select("pipeline_stage", "Этап сделки", [
        ["new","Новый"],
        ["email","Письмо"],
        ["call","Звонок"],
        ["conversation","Переговоры"],
        ["qualification","Квалификация"],
        ["passed_to_director","Передан директору"],
        ["negotiation","Переговоры по сделке"],
        ["deal","Сделка заключена"],
        ["refusal","Отказ"],
        ["nurture","Отложен"]
      ]),
      field("has_budget_source", "Подтверждены бюджет и источник средств", "checkbox"),
      field("interested_in_asset", "Есть интерес к конкретному объекту", "checkbox"),
      field("horizon_within_6_months", "Горизонт до 6 месяцев", "checkbox"),
      field("decision_maker_access", "Есть доступ к ЛПР", "checkbox"),
      field("agreed_next_step", "Согласован следующий шаг", "checkbox"),
      field("lead_source", "Источник контакта"),
      field("owner", "Ответственный"),
      field("next_step", "Следующее действие", "textarea", {full:true}),
      field("next_step_at", "Дата следующего действия", "datetime"),
      field("notes", "Заметки", "textarea", {full:true})
    ]
  },

  interactions: {
    title: "История общения",
    subtitle: "Звонки, письма, Telegram и встречи",
    relationLabel: "subject",
    columns: [
      ["interaction_type", "Канал"],
      ["direction", "Направление"],
      ["subject", "Тема"],
      ["happened_at", "Дата"]
    ],
    fields: [
      relation("investor_id", "Инвестор", "investors"),
      relation("company_id", "Компания", "companies"),
      relation("contact_id", "Контакт", "contacts"),
      relation("asset_id", "Объект", "assets"),
      select("interaction_type", "Тип общения", [
        ["call","Звонок"],
        ["email","Email"],
        ["telegram","Telegram"],
        ["meeting","Встреча"],
        ["other","Другое"]
      ], {required:true}),
      select("direction", "Направление", [
        ["outbound","Исходящее"],
        ["inbound","Входящее"]
      ]),
      field("subject", "Тема"),
      field("body", "Содержание разговора / письма", "textarea", {full:true}),
      field("happened_at", "Дата и время", "datetime")
    ]
  },

  tasks: {
    title: "Задачи",
    subtitle: "Следующие действия и напоминания",
    relationLabel: "title",
    columns: [
      ["title", "Задача"],
      ["task_type", "Тип"],
      ["due_at", "Срок"],
      ["assigned_to", "Ответственный"],
      ["status", "Статус"],
      ["priority", "Приоритет"]
    ],
    fields: [
      field("title", "Название задачи", "text", {required:true}),
      field("description", "Описание", "textarea", {full:true}),
      select("task_type", "Тип задачи", [
        ["call","Звонок"],
        ["email","Email"],
        ["meeting","Встреча"],
        ["follow_up","Повторный контакт"],
        ["other","Другое"]
      ]),
      field("due_at", "Срок выполнения", "datetime"),
      select("status", "Статус", [
        ["open","Открыта"],
        ["in_progress","В работе"],
        ["done","Выполнена"],
        ["cancelled","Отменена"]
      ]),
      select("priority", "Приоритет", [
        ["low","Низкий"],
        ["normal","Обычный"],
        ["high","Высокий"]
      ]),
      field("assigned_to", "Ответственный"),
      relation("investor_id", "Инвестор", "investors"),
      relation("company_id", "Компания", "companies"),
      relation("contact_id", "Контакт", "contacts"),
      relation("asset_id", "Объект", "assets"),
      field("completed_at", "Дата выполнения", "datetime")
    ]
  }
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;",
    '"':"&quot;", "'":"&#39;"
  })[c]);
}

function pretty(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (Array.isArray(value) || typeof value === "object") return JSON.stringify(value);
  return labels[value] || String(value);
}

function notify(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => toast.classList.remove("show"), 3500);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {"Content-Type":"application/json", ...(options.headers || {})},
    ...options
  });

  if (response.status === 401) {
    throw new Error("Необходима авторизация. Обновите страницу и войдите снова.");
  }

  if (!response.ok) {
    let message = `Ошибка HTTP ${response.status}`;
    try {
      const data = await response.json();
      message = data.detail || message;
    } catch {}
    throw new Error(message);
  }

  if (response.status === 204) return null;
  return response.json();
}

async function loadDashboard() {
  const s = await api("/api/crm/summary");

  const cards = [
    ["Инвесторы", s.investors],
    ["Квалифицированные", s.qualified_investors],
    ["Компании", s.companies],
    ["Объекты", s.assets],
    ["Задачи", s.tasks],
    ["Взаимодействия", s.interactions]
  ];

  $("summaryCards").innerHTML = cards.map(([name, count]) =>
    `<div class="stat"><span>${esc(name)}</span><strong>${count}</strong></div>`
  ).join("");

  $("temperatureStats").innerHTML = Object.entries(s.investors_by_temperature || {})
    .map(([name, count]) =>
      `<span class="metric-chip">${esc(pretty(name))}<strong>${count}</strong></span>`
    ).join("") || "Инвесторов пока нет";

  $("stageStats").innerHTML = Object.entries(s.investors_by_stage || {})
    .map(([name, count]) =>
      `<span class="metric-chip">${esc(pretty(name))}<strong>${count}</strong></span>`
    ).join("") || "Сделок пока нет";
}

async function loadEntity() {
  const config = forms[state.entity];
  if (!config) return;

  $("entityTitle").textContent = config.title;
  $("entitySubtitle").textContent = config.subtitle;
  $("searchInput").value = state.query;

  const params = new URLSearchParams({
    q: state.query,
    page: state.page,
    page_size: state.pageSize
  });

  $("tableBody").innerHTML = '<tr><td colspan="10" class="empty">Загрузка...</td></tr>';

  const result = await api(`/api/crm/${state.entity}?${params}`);

  state.items = result.items;

  $("tableHead").innerHTML =
    "<tr>" +
    config.columns.map(([key, label]) => `<th>${esc(label)}</th>`).join("") +
    "<th>Действие</th></tr>";

  if (!result.items.length) {
    $("tableBody").innerHTML =
      `<tr><td colspan="${config.columns.length + 1}" class="empty">Записей пока нет</td></tr>`;
  } else {
    $("tableBody").innerHTML = result.items.map(row => `
      <tr>
        ${config.columns.map(([key]) =>
          `<td>${esc(pretty(row[key]))}</td>`
        ).join("")}
        <td><button class="edit-btn" data-edit="${row.id}">Открыть / изменить</button></td>
      </tr>
    `).join("");
  }

  const pages = Math.max(1, Math.ceil(result.total / state.pageSize));
  $("pageInfo").textContent = `Страница ${state.page} из ${pages} · Всего: ${result.total}`;
  $("prevBtn").disabled = state.page <= 1;
  $("nextBtn").disabled = state.page >= pages;
}

async function openView(view) {
  state.view = view;

  document.querySelectorAll(".nav-btn").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.view === view)
  );

  const isImportsView = view === "imports";
  $("dashboardView").hidden = view !== "dashboard";
  $("entityView").hidden = view === "dashboard" || isImportsView;
  const addButton = $("addBtn");
  if (addButton) addButton.hidden = !forms[view];
  $("importsView").hidden = !isImportsView;

  if (isImportsView) {
    if (typeof window.loadImportsView === "function") {
      await window.loadImportsView();
    }
    return;
  }

  if (view === "dashboard") {
    await loadDashboard();
    return;
  }

  if (!forms[view]) return;

  state.entity = view;
  state.page = 1;
  state.query = "";
  $("searchInput").value = "";
  await loadEntity();
}

async function loadRelationOptions(entity) {
  const result = await api(`/api/crm/${entity}?page=1&page_size=100`);
  const config = forms[entity];
  return result.items.map(item => ({
    id: item.id,
    label: item[config.relationLabel] || `Запись ${item.id}`
  }));
}

function formatDateValue(value) {
  if (!value) return "";
  return String(value).replace(" ", "T").slice(0,16);
}

async function renderForm(record = null) {
  const config = forms[state.entity];
  state.editingId = record?.id ?? null;

  $("formTitle").textContent = record
    ? `Изменение записи №${record.id}`
    : `Новая запись — ${config.title}`;

  const fieldsHTML = [];

  for (const f of config.fields) {
    const value = record?.[f.key];
    const id = `crm_${f.key}`;
    const full = f.full ? " full" : "";

    if (f.type === "checkbox") {
      fieldsHTML.push(`
        <div class="form-field check${full}">
          <input id="${id}" name="${f.key}" type="checkbox" ${value ? "checked" : ""}>
          <label for="${id}">${esc(f.label)}</label>
        </div>
      `);
      continue;
    }

    let control = "";

    if (f.type === "select" || f.type === "relation") {
      let options = [];

      if (f.type === "relation") {
        options = await loadRelationOptions(f.entity);
      } else {
        options = f.options.map(([v, label]) => ({id:v, label}));
      }

      control = `
        <select id="${id}" name="${f.key}" ${f.required ? "required" : ""}>
          <option value="">— Не выбрано —</option>
          ${options.map(o => `
            <option value="${esc(o.id)}" ${String(value ?? "") === String(o.id) ? "selected" : ""}>
              ${esc(o.label)}
            </option>
          `).join("")}
        </select>
      `;
    } else if (f.type === "textarea" || f.type === "json") {
      const textValue = f.type === "json"
        ? (value == null ? "" : JSON.stringify(value, null, 2))
        : (value ?? "");

      control = `
        <textarea id="${id}" name="${f.key}" placeholder="${esc(f.placeholder || "")}">${esc(textValue)}</textarea>
      `;
    } else {
      let inputValue = value ?? "";
      if (f.type === "datetime") inputValue = formatDateValue(inputValue);

      control = `
        <input
          id="${id}"
          name="${f.key}"
          type="${f.type || "text"}"
          value="${esc(inputValue)}"
          ${f.required ? "required" : ""}
          ${f.min !== undefined ? `min="${f.min}"` : ""}
          ${f.type === "number" ? 'step="any"' : ""}
          placeholder="${esc(f.placeholder || "")}"
        >
      `;
    }

    fieldsHTML.push(`
      <div class="form-field${full}">
        <label for="${id}">${esc(f.label)}</label>
        ${control}
      </div>
    `);
  }

  $("formFields").innerHTML = fieldsHTML.join("");
  $("recordDialog").showModal();
}

function buildPayload() {
  const config = forms[state.entity];
  const payload = {};

  for (const f of config.fields) {
    const el = $("crm_" + f.key);
    if (!el) continue;

    if (f.type === "checkbox") {
      payload[f.key] = el.checked;
      continue;
    }

    const raw = el.value.trim();

    if (!raw) continue;

    if (f.type === "json") {
      try {
        payload[f.key] = JSON.parse(raw);
      } catch {
        throw new Error(`Проверь формат JSON в поле «${f.label}»`);
      }
    } else if (f.type === "number" || f.type === "relation") {
      const number = Number(raw);
      if (!Number.isFinite(number)) {
        throw new Error(`Некорректное число в поле «${f.label}»`);
      }
      payload[f.key] = number;
    } else if (f.type === "datetime") {
      const date = new Date(raw);
      if (Number.isNaN(date.getTime())) {
        throw new Error(`Некорректная дата в поле «${f.label}»`);
      }
      payload[f.key] = date.toISOString();
    } else {
      payload[f.key] = raw;
    }
  }

  return payload;
}

async function saveRecord(event) {
  event.preventDefault();

  try {
    const payload = buildPayload();
    const editing = state.editingId !== null;

    const url = editing
      ? `/api/crm/${state.entity}/${state.editingId}`
      : `/api/crm/${state.entity}`;

    await api(url, {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(payload)
    });

    $("recordDialog").close();
    notify(editing ? "Изменения сохранены" : "Запись создана");

    if (state.view === "dashboard") {
      await loadDashboard();
    } else {
      await loadEntity();
    }
  } catch (error) {
    notify(error.message);
  }
}

document.querySelectorAll(".nav-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    try {
      await openView(btn.dataset.view);
    } catch (error) {
      notify(error.message);
    }
  });
});

$("refreshDashboard").addEventListener("click", async () => {
  try {
    await loadDashboard();
    notify("Сводка обновлена");
  } catch (error) {
    notify(error.message);
  }
});

$("addBtn").addEventListener("click", async () => {
  try {
    if (!forms[state.entity]) { notify("В этом разделе стандартное добавление записей недоступно."); return; }
      await renderForm();
  } catch (error) {
    notify(error.message);
  }
});

$("tableBody").addEventListener("click", async event => {
  const button = event.target.closest("[data-edit]");
  if (!button) return;

  const record = state.items.find(row => String(row.id) === button.dataset.edit);
  if (!record) return;

  try {
    await renderForm(record);
  } catch (error) {
    notify(error.message);
  }
});

$("recordForm").addEventListener("submit", saveRecord);

function closeDialog() {
  $("recordDialog").close();
}

$("closeDialog").addEventListener("click", closeDialog);
$("cancelBtn").addEventListener("click", closeDialog);

async function search() {
  state.query = $("searchInput").value.trim();
  state.page = 1;
  try {
    await loadEntity();
  } catch (error) {
    notify(error.message);
  }
}

$("searchBtn").addEventListener("click", search);
$("searchInput").addEventListener("keydown", event => {
  if (event.key === "Enter") search();
});

$("prevBtn").addEventListener("click", async () => {
  state.page = Math.max(1, state.page - 1);
  try { await loadEntity(); }
  catch (error) { notify(error.message); }
});

$("nextBtn").addEventListener("click", async () => {
  state.page++;
  try { await loadEntity(); }
  catch (error) { notify(error.message); }
});

loadDashboard().catch(error => notify(error.message));

// GAB CRM — внутренняя система уведомлений

async function loadNotifications() {
  const badge = $("notificationBadge");
  const list = $("notificationList");

  try {
    const data = await api("/api/crm/notifications?limit=30");

    badge.textContent = data.unread_count > 99
      ? "99+"
      : String(data.unread_count);

    badge.hidden = data.unread_count === 0;

    if (!data.items.length) {
      list.innerHTML =
        '<div class="notification-empty">Новых уведомлений пока нет</div>';
      return;
    }

    list.innerHTML = data.items.map(item => {
      const date = new Date(item.created_at);
      const dateText = Number.isNaN(date.getTime())
        ? ""
        : date.toLocaleString("ru-RU");

      return `
        <button
          type="button"
          class="notification-item ${item.read_at ? "" : "unread"}"
          data-notification-id="${item.id}"
          data-investor-id="${item.investor_id}"
        >
          <strong>${esc(item.title)}</strong>
          <p>${esc(item.message)}</p>
          <small>
            ${esc(item.investor_name)} · ${esc(dateText)}
          </small>
        </button>
      `;
    }).join("");

  } catch (error) {
    badge.hidden = true;
    list.innerHTML =
      `<div class="notification-empty">${esc(error.message)}</div>`;
  }
}

$("notificationBtn").addEventListener("click", async () => {
  const panel = $("notificationPanel");
  const opened = panel.hidden;

  panel.hidden = !opened;
  $("notificationBtn").setAttribute("aria-expanded", String(opened));

  if (opened) {
    await loadNotifications();
  }
});

$("notificationList").addEventListener("click", async event => {
  const item = event.target.closest("[data-notification-id]");
  if (!item) return;

  const notificationId = item.dataset.notificationId;
  const investorId = item.dataset.investorId;

  try {
    await api(
      `/api/crm/notifications/${notificationId}/read`,
      {method:"PATCH"}
    );

    await loadNotifications();

    $("notificationPanel").hidden = true;
    $("notificationBtn").setAttribute("aria-expanded", "false");

    await openView("investors");

    const investor = await api(`/api/crm/investors/${investorId}`);
    await renderForm(investor);

  } catch (error) {
    notify(error.message);
  }
});

$("markAllRead").addEventListener("click", async () => {
  try {
    const result = await api(
      "/api/crm/notifications/read-all",
      {method:"POST"}
    );

    await loadNotifications();
    notify(`Прочитано уведомлений: ${result.updated}`);

  } catch (error) {
    notify(error.message);
  }
});

// Обновляем счётчик при открытии CRM и периодически
loadNotifications();
setInterval(() => loadNotifications(), 30000);
