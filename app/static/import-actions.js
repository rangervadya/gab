(() => {
  'use strict';
  let generation=0,updateStatus=()=>{};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const byId=id=>document.getElementById(id);
  async function api(url,body) {
    const response=await fetch(url,{credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/json'},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
    let data;try{data=await response.json();}catch(_){const error=new Error('Ошибка HTTP '+response.status);error.status=response.status;throw error;}if(!response.ok){const error=new Error(data.detail||'Ошибка запроса');error.status=response.status;throw error;}return data;
  }
  window.GabImportActions={
    refreshStatus(id,status){updateStatus(id,status);},
    mount(batch,refresh,reasonText=v=>v) {
      const current=++generation,root=byId('importActionsPanel');if(!root)return;
      let busy=false,stop=false,groups=[],previews=[],status=batch.status;
      const active=()=>current===generation;
      root.innerHTML=`<details><summary>DaData, Checko и массовый перенос</summary>
        <p id="importProvidersStatus">Проверяем настройки интеграций…</p>
        <p class="import-muted">DaData получает название или ИНН компании, Checko — выбранный ИНН. Запросы используют лимиты вашего тарифа. Результаты могут не содержать email.</p>
        <label><input type="checkbox" id="importUseChecko">Получать контакты и выручку также из Checko</label><div class="import-actions"><button class="button secondary" id="importFindContacts">Найти компании и почты во всём пакете</button><button class="button secondary" id="importStopActions" hidden>Остановить после текущего запроса</button></div>
        <p id="importActionsStatus" role="status"></p><div id="importRegistryCandidates"></div>
        <div class="import-actions"><button class="button secondary" id="importPrepareAll">Проверить все перед переносом</button></div>
        <div id="importBulkPreview"></div>
      </details>`;
      const progress=message=>{if(active())byId('importActionsStatus').textContent=message;};
      function controls() {
        if(!active())return;
        const ready=['review','ready','completed'].includes(status);
        byId('importFindContacts').disabled=busy||!ready;
        byId('importPrepareAll').disabled=busy||!ready;
        byId('importStopActions').hidden=!busy;
      }
      updateStatus=(id,next)=>{if(Number(id)===Number(batch.id)){status=next;controls();}};
      controls();
      byId('importStopActions').onclick=()=>{stop=true;progress('Останавливаемся после текущего запроса. Выполненные действия сохраняются.');};
      let providers={};
      api('/api/mailings/registry-enrichment/imports/configuration').then(value=>{
        if(!active())return;providers=value;byId('importUseChecko').checked=value.checko_configured;byId('importUseChecko').disabled=!value.checko_configured;
        byId('importProvidersStatus').textContent=`DaData: ${value.dadata_configured?'ключ настроен':'ключ не настроен'}. Checko: ${value.checko_configured?'ключ настроен':'ключ не настроен'}. Проверка ключей выполняется первым запросом.`;
      }).catch(error=>{if(active())byId('importProvidersStatus').textContent=error.message;});
      async function allRows() {
        let rows=[],offset=0;
        do {
          const result=await api(`/api/mailings/imports/${batch.id}/rows?limit=500&offset=${offset}`);
          if(result.total>5000)throw new Error('Для пакета больше 5000 строк используйте меньшие пакеты');
          rows.push(...result.items);offset+=result.items.length;
          if(!active()||stop||offset>=result.total||!result.items.length)break;
        }while(true);
        return rows;
      }
      function renderCandidates() {
        if(!active())return;
        byId('importRegistryCandidates').innerHTML=groups.map((group,i)=>`<div class="import-processing-progress">
          <b>${esc(group.source_name||'Строка '+group.import_row_id)}</b> · ИНН файла: ${esc(group.source_inn||'—')}
          ${group.error?`<p class="import-error">${esc(group.error)}</p>`:''}
          ${group.candidates.length?`<select class="import-text-input" data-registry-choice="${i}"><option value="">Выберите найденную компанию</option>${group.candidates.map((c,j)=>`<option value="${j}"${group.selected===j?' selected':''}>${esc(c.candidate.legal_name)} · ИНН ${esc(c.candidate.inn)} · ${esc(c.candidate.region||'')}</option>`).join('')}</select>
          <button class="button secondary" data-checko="${i}">Получить контакты выбранной компании</button><button class="button secondary" data-dadata="${i}">Использовать данные DaData</button>
          ${group.card?`<p>${esc(group.card.provider==='checko'?'Checko':'DaData')}: ${esc(group.card.legal_name)} · ИНН ${esc(group.card.inn)}</p><p>Email: ${esc((group.card.emails||[]).join(', ')||'не найден')} · телефон: ${esc((group.card.phones||[]).join(', ')||'не найден')}</p><p>Выручка: ${esc(group.card.revenue_rub??'—')} · ${esc(group.card.revenue_year??'—')}</p>${group.card.provider==='checko'&&group.selected!==null?`<p>Дополнительно DaData: ${esc([...(group.candidates[group.selected].candidate.emails||[]),...(group.candidates[group.selected].candidate.phones||[])].join(', ')||'контакты не найдены')}</p>`:''}<label><input type="checkbox" data-registry-apply="${i}"${group.apply?" checked":""}${group.applied?" disabled":""}>${group.applied?"Применено. ":""}Проверил компанию и показанные контакты; применить к строке</label>`:''}`:'<p>Подходящих компаний не найдено</p>'}
          </div>`).join('')+(groups.length?`<label><input type="checkbox" id="importRegistryRevenue">Использовать выручку Checko вместо распознанной; ручные исправления сохраняются</label><button class="button" id="importApplyRegistry">Применить отмеченные компании и контакты</button>`:'');
        for(const select of root.querySelectorAll('[data-registry-choice]'))select.onchange=()=>{
          const g=groups[Number(select.dataset.registryChoice)];g.selected=select.value===''?null:Number(select.value);g.card=null;g.apply=false;g.applied=false;renderCandidates();};
        for(const button of root.querySelectorAll('[data-checko]'))button.onclick=async()=>{
          if(busy)return;const g=groups[Number(button.dataset.checko)];if(g.selected===null){progress('Сначала выберите компанию');return;}
          busy=true;controls();try{await details(g);renderCandidates();}catch(error){progress(error.message);}finally{busy=false;controls();}};
        for(const button of root.querySelectorAll('[data-dadata]'))button.onclick=()=>{if(busy)return;const g=groups[Number(button.dataset.dadata)];if(g.selected===null){progress('Сначала выберите компанию');return;}g.card=g.candidates[g.selected].candidate;g.applied=false;g.apply=false;renderCandidates();};
        for(const check of root.querySelectorAll('[data-registry-apply]'))check.onchange=()=>{groups[Number(check.dataset.registryApply)].apply=check.checked;};
        const apply=byId('importApplyRegistry');if(apply)apply.onclick=async()=>{
          if(busy)return;const selected=[...root.querySelectorAll('[data-registry-apply]:checked:not(:disabled)')].map(e=>groups[Number(e.dataset.registryApply)]);
          busy=true;stop=false;controls();let count=0;
          try{for(const g of selected){if(!active()||stop)break;
            const result=await api(`/api/mailings/registry-enrichment/imports/${g.import_row_id}/confirm`,{inn:g.card.inn,provider:g.card.provider,version:g.version,checked_at:g.card.checked_at,include_dadata:g.card.provider==='checko'&&Boolean((g.candidates[g.selected].candidate.emails||[]).length||(g.candidates[g.selected].candidate.phones||[]).length),dadata_checked_at:g.candidates[g.selected].candidate.checked_at,use_revenue:byId('importRegistryRevenue').checked});g.version=result.import_row.edit_version;g.applied=true;g.apply=false;count++;}
            if(active()){progress(`Применено компаний: ${count}. Теперь выполните проверку перед переносом.`);previews=[];byId('importBulkPreview').innerHTML='';renderCandidates();await refresh();}
          }catch(error){progress(`Применено: ${count}. ${error.message}. Обновите поиск для оставшихся строк.`);if(active()){renderCandidates();await refresh();}}finally{busy=false;controls();}
        };
      }
      async function details(group) {
        group.apply=false;group.applied=false;
        const item=group.candidates[group.selected];
        if(providers.checko_configured&&byId('importUseChecko').checked){const result=await api(`/api/mailings/registry-enrichment/imports/${group.import_row_id}/checko`,{inn:item.candidate.inn});group.card=result.item;}
        else group.card=item.candidate;
      }
      byId('importFindContacts').onclick=async()=>{
        if(busy)return;if(!providers.dadata_configured){progress('Ключ DaData не настроен. На сервере выполните python3 scripts/configure-integrations.py');return;}
        busy=true;stop=false;groups=[];previews=[];byId('importBulkPreview').innerHTML='';controls();
        try{const rows=(await allRows()).filter(r=>['pending','needs_review'].includes(r.import_status));
          for(const [i,row] of rows.entries()){
            if(!active()||stop)break;progress(`Поиск компаний и контактов: ${i+1} из ${rows.length}`);
            let group={import_row_id:row.id,source_name:row.company_name||row.full_name,source_inn:row.inn,candidates:[],selected:null,version:row.edit_version};
            try{group={...group,...await api(`/api/mailings/registry-enrichment/imports/${row.id}/search`,{})};
              if(group.candidates.length===1&&group.candidates[0].source_inn_match){group.selected=0;await details(group);}
            }catch(error){group.error=error.message;if(error.status>=500||[401,403,429].includes(error.status)){stop=true;}}
            groups.push(group);renderCandidates();
          }
          progress(`${stop?'Поиск приостановлен':'Поиск завершён'}: ${groups.length} строк. Проверьте сообщения и найденные компании; отметьте нужные. Письма не отправлены.`);
        }catch(error){progress(error.message);}finally{busy=false;controls();}
      };
      byId('importPrepareAll').onclick=async()=>{
        if(busy)return;busy=true;stop=false;previews=[];controls();
        try{const rows=await allRows();for(let offset=0;offset<rows.length;offset+=25){
            if(!active()||stop)break;
            progress(`Проверяем строки: ${Math.min(offset+25,rows.length)} из ${rows.length}`);
            previews.push(await api(`/api/mailings/imports/${batch.id}/confirm-preview`,{row_ids:rows.slice(offset,offset+25).map(r=>r.id)}));
          }
          if(stop||!active()){previews=[];return;}
          const items=previews.flatMap(p=>p.items),ready=items.filter(r=>r.ready),blocked=items.filter(r=>!r.ready&&!r.already_confirmed);
          byId('importBulkPreview').innerHTML=`<h3>Предпросмотр переноса</h3><p>Готово: ${ready.length}. Требуют отдельной проверки: ${blocked.length}. Уже перенесено: ${items.filter(r=>r.already_confirmed).length}.</p>
            <div class="import-table-wrap"><table class="import-table"><thead><tr><th>Компания / контакт</th><th>ИНН</th><th>Email</th><th>Результат проверки</th></tr></thead><tbody>${items.map(r=>`<tr><td>${esc(r.company_name||r.full_name||'—')}</td><td>${esc(r.inn||'—')}</td><td>${esc([...r.emails,...r.company_emails].join(', ')||'—')}</td><td>${r.already_confirmed?'Уже перенесено':r.ready?'Готово к подтверждению':'Нужна отдельная проверка'} ${esc(r.reasons.map(reasonText).join('; '))}${r.warnings.length?`<p>${esc(r.warnings.join('; '))}</p>`:''}</td></tr>`).join('')}</tbody></table></div>
            <label><input type="checkbox" id="importBulkReviewed">Проверил список и найденные компании; подтверждаю перенос готовых строк в CRM</label><br>
            <label><input type="checkbox" id="importBulkWarnings">Проверил по оригиналу строки с предупреждениями распознавания</label><br>
            <button class="button" id="importConfirmAll" disabled>Подтвердить все готовые (${ready.length})</button>`;
          const execute=byId('importConfirmAll');const confirmControls=()=>{const count=ready.filter(r=>!r.requires_warning_review||byId('importBulkWarnings').checked).length;execute.disabled=!byId('importBulkReviewed').checked||!count;execute.textContent=`Подтвердить все готовые (${count})`;};byId('importBulkReviewed').onchange=confirmControls;byId('importBulkWarnings').onchange=confirmControls;confirmControls();
          execute.onclick=async()=>{
            if(busy||!byId('importBulkReviewed').checked)return;busy=true;stop=false;execute.disabled=true;controls();let transferred=0,skipped=0;
            const warnings=byId('importBulkWarnings').checked;
            try{for(const p of previews){if(!active()||stop)break;
                const result=await api(`/api/mailings/imports/${batch.id}/confirm-all`,{token:p.token,reviewed:true,acknowledge_warnings:warnings});
                transferred+=result.transferred;skipped+=result.items.filter(r=>['skipped','needs_review'].includes(r.status)).length;
                progress(`Перенесено: ${transferred}; пропущено: ${skipped}`);
              }if(active())await refresh();progress(`Перенос завершён. Перенесено: ${transferred}. Пропущено: ${skipped+blocked.length}. Для оставшихся строк обновите проверку; неоднозначные совпадения выбираются через «Совпадения». Письма не отправлялись.`);
            }catch(error){progress(`Часть строк могла быть перенесена. ${error.message}. Обновите проверку перед повтором.`);}finally{busy=false;controls();previews=[];if(active()){execute.disabled=true;byId('importBulkReviewed').disabled=true;byId('importBulkWarnings').disabled=true;}}
          };
          progress('Предпросмотр готов. Перенос ещё не запускался.');
        }catch(error){progress(error.message);}finally{busy=false;controls();}
      };
    }
  };
})();
