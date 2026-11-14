const state = { page: 1, pageSize: 30, scope: "pending", q: "", selected: null, total: 0 };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = (v) => (v === null || v === undefined || v === "") ? "—" : String(v);
async function api(url, options={}) {
  const response = await fetch(url, {headers: {"Content-Type":"application/json", ...(options.headers||{})}, ...options});
  if (response.status === 401) throw new Error("Требуется вход. Обновите страницу и войдите в систему.");
  if (!response.ok) throw new Error((await response.text()) || `HTTP ${response.status}`);
  return response.json();
}
async function loadSummary() {
  const s = await api("/api/summary");
  $("totalCount").textContent = s.total_records;
  $("pendingCount").textContent = s.pending;
  $("reviewedCount").textContent = s.reviewed;
  $("noReviewCount").textContent = s.total_records - s.review_required;
}
function recordBadge(r) {
  if (r.review_state === "approved" || r.review_state === "corrected") return '<span class="pill done">Проверено</span>';
  if ((r.review_reasons || "").includes("rotated_page_manual_review")) return '<span class="pill bad">Проверить страницу</span>';
  return r.review_required ? '<span class="pill">Проверить</span>' : '<span class="pill done">Без замечаний</span>';
}
async function loadRecords() {
  const params = new URLSearchParams({scope:state.scope, q:state.q, page:state.page, page_size:state.pageSize});
  const result = await api("/api/records?" + params.toString());
  state.total = result.total;
  $("listCount").textContent = `${result.total} записей`;
  $("listTitle").textContent = state.scope === "pending" ? "Ожидают проверки" : state.scope === "reviewed" ? "Проверены" : state.scope === "not_required" ? "Без обязательной проверки" : "Все записи";
  $("pageInfo").textContent = `Страница ${state.page} из ${Math.max(1,Math.ceil(result.total/state.pageSize))}`;
  $("prevBtn").disabled = state.page <= 1;
  $("nextBtn").disabled = state.page * state.pageSize >= result.total;
  $("records").innerHTML = result.items.length ? result.items.map(r => `
    <div class="record ${state.selected===r.record_id?'active':''}" data-id="${esc(r.record_id)}">
      <div><div class="record-title">${esc(r.internal_name || "Без названия")} <span class="record-meta">· ${esc(r.record_id)}</span></div>
      <div class="record-meta">${esc(r.counterparty_contacts || r.address || "Контрагент не распознан")}</div>
      ${r.additional_review_note ? `<div class="record-note">${esc(r.additional_review_note)}</div>` : (r.review_reasons ? `<div class="record-note">${esc(r.review_reasons.replaceAll("_"," "))}</div>` : "")}</div>
      ${recordBadge(r)}
    </div>`).join("") : '<div class="empty">Записей не найдено</div>';
  document.querySelectorAll(".record").forEach(el => el.addEventListener("click", () => openRecord(el.dataset.id)));
}
async function openRecord(id) {
  const r = await api("/api/records/" + encodeURIComponent(id));
  state.selected = id;
  await loadRecords();
  const p = r.payload || {};
  const corrected = r.corrected_fields || {};
  const rawCells = p.raw_cells_json || p.raw_cells || [];
  let rawText = rawCells;
  if (typeof rawText === "string") { try { rawText = JSON.parse(rawText); } catch {} }
  $("detailPanel").innerHTML = `
    <div class="detail">
      <div class="detail-top"><div><h2>${esc(p.internal_name || r.internal_name || "Без названия")}</h2><div class="detail-id">${esc(id)} · Страница ${esc(r.source_page)}, строка ${esc(r.source_row)}</div></div>${recordBadge(r)}</div>
      ${r.additional_review_note ? `<div class="section-title">Дополнительная проверка</div><div class="note">${esc(r.additional_review_note)}</div>` : ""}
      <div class="section-title">Причины проверки</div><div class="warning-list">${esc((r.review_reasons || "Дополнительных предупреждений нет").replaceAll("; ","\n").replaceAll("_"," "))}</div>
      <div class="section-title">Исходные данные</div>
      <div class="field-grid">
        <div class="field full"><label>Адрес</label><div class="raw">${esc(p.address)}</div></div>
        <div class="field"><label>Площадь OCR</label><div class="raw">${esc(p.area_sqm_raw)}</div></div>
        <div class="field"><label>Распознанная площадь</label><div class="raw">${esc(fmt(p.area_sqm))}</div></div>
        <div class="field"><label>Начало аренды</label><div class="raw">${esc(fmt(p.lease_start_date_raw))} → ${esc(fmt(p.lease_start_date))}</div></div>
        <div class="field"><label>Окончание аренды</label><div class="raw">${esc(fmt(p.lease_end_month_raw))} ${esc(fmt(p.lease_end_year_raw))}</div></div>
        <div class="field"><label>Срок договора</label><div class="raw">${esc(p.lease_term_raw)}</div></div>
        <div class="field"><label>Регистрация</label><div class="raw">${esc(fmt(p.registration_date_raw))} → ${esc(fmt(p.registration_date))}</div></div>
        <div class="field full"><label>Исходные ячейки OCR</label><div class="raw">${esc(JSON.stringify(rawText))}</div></div>
        <div class="field full"><label>Контрагент / контакты</label><div class="raw">${esc(p.counterparty_contacts_raw || p.counterparty_contacts)}</div></div>
      </div>
      <div class="section-title">Результат ручной проверки</div>
      <div class="field-grid">
        <div class="field"><label>Название после проверки</label><input id="f_name" value="${esc(corrected.internal_name || "")}" placeholder="${esc(p.internal_name || "")}"></div>
        <div class="field"><label>Площадь, кв. м</label><input id="f_area" value="${esc(corrected.area_sqm || "")}" placeholder="${esc(fmt(p.area_sqm))}"></div>
        <div class="field"><label>Начало аренды (YYYY-MM-DD)</label><input id="f_start" value="${esc(corrected.lease_start_date || "")}" placeholder="${esc(fmt(p.lease_start_date))}"></div>
        <div class="field"><label>Год окончания</label><input id="f_end" value="${esc(corrected.lease_end_year || "")}" placeholder="${esc(fmt(p.lease_end_year))}"></div>
        <div class="field"><label>Срок договора</label><input id="f_term" value="${esc(corrected.lease_term || "")}" placeholder="${esc(p.lease_term || "")}"></div>
        <div class="field"><label>Дата регистрации (YYYY-MM-DD)</label><input id="f_reg" value="${esc(corrected.registration_date || "")}" placeholder="${esc(fmt(p.registration_date))}"></div>
        <div class="field full"><label>Примечание проверяющего</label><textarea id="f_notes">${esc(r.reviewer_notes || "")}</textarea></div>
        <div class="field full"><label>Решение</label><select id="f_state">
          <option value="pending" ${r.review_state==="pending"?"selected":""}>Оставить в очереди</option>
          <option value="approved" ${r.review_state==="approved"?"selected":""}>Подтверждено без исправлений</option>
          <option value="corrected" ${r.review_state==="corrected"?"selected":""}>Исправлено вручную</option>
          <option value="needs_source_check" ${r.review_state==="needs_source_check"?"selected":""}>Нужна сверка с оригиналом</option>
        </select></div>
      </div>
      <div class="actions"><button class="primary" id="saveBtn">Сохранить решение</button><button class="secondary" id="keepBtn">Оставить в очереди</button></div>
      <div class="footer-hint">Исправления хранятся отдельно. Исходные OCR-данные не перезаписываются.</div>
    </div>`;
  $("saveBtn").addEventListener("click", saveReview);
  $("keepBtn").addEventListener("click", async () => { $("f_state").value="pending"; await saveReview(); });
}
async function saveReview() {
  const id = state.selected;
  const corrected_fields = {};
  const fields = {
    internal_name: $("f_name").value.trim(),
    area_sqm: $("f_area").value.trim(),
    lease_start_date: $("f_start").value.trim(),
    lease_end_year: $("f_end").value.trim(),
    lease_term: $("f_term").value.trim(),
    registration_date: $("f_reg").value.trim()
  };
  for (const [k,v] of Object.entries(fields)) if (v) corrected_fields[k] = v;
  const payload = {
    review_state: $("f_state").value,
    reviewer_decision: $("f_state").selectedOptions[0].textContent,
    reviewer_notes: $("f_notes").value,
    corrected_fields
  };
  await api("/api/records/" + encodeURIComponent(id), {method:"PATCH", body:JSON.stringify(payload)});
  await loadSummary(); await loadRecords(); await openRecord(id);
}
async function refresh() {
  try { await loadSummary(); await loadRecords(); }
  catch (e) { $("records").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
$("refreshBtn").addEventListener("click", refresh);
$("scope").addEventListener("change", e => {state.scope=e.target.value;state.page=1;refresh();});
$("search").addEventListener("input", e => {state.q=e.target.value;state.page=1;refresh();});
$("prevBtn").addEventListener("click",()=>{state.page=Math.max(1,state.page-1);refresh();});
$("nextBtn").addEventListener("click",()=>{state.page++;refresh();});
refresh();
