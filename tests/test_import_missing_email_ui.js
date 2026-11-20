// Synthetic list refresh only; no real contacts or delivery endpoints.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {if (!elements.has(id)) elements.set(id, {innerHTML:'', value:'', hidden:true}); return elements.get(id);};
let items = [{id:1,company_name:'<img src=x>',effective_phones:['+70000000001','+70000000002'],extraction_warnings:[]}];
const paths=[];
const context={document:{getElementById:element,activeElement:null},window:{},URLSearchParams,
 fetch:async path=>{paths.push(path); assert.ok(path.includes('/rows?')); return {ok:true,json:async()=>({items,total:items.length})};}};
let source=fs.readFileSync('app/static/imports.js','utf8');
const i=source.lastIndexOf('})();');
source=source.slice(0,i)+'window.testMissingEmail={state,renderRows,loadRows};'+source.slice(i);
vm.runInNewContext(source,context);
(async()=>{
 const t=context.window.testMissingEmail;
 t.state.batches=[{id:991}];t.state.selectedBatchId=991;t.state.segment='without_email_phone';
 await t.loadRows();
 const area=element('importRowsArea');
 assert.ok(area.innerHTML.includes('value="without_email"'));
 assert.ok(area.innerHTML.includes('value="without_email_no_phone"'));
 assert.ok(area.innerHTML.includes('&lt;img src=x&gt;'));
 assert.ok(area.innerHTML.includes('tel:+70000000001')&&area.innerHTML.includes('tel:+70000000002'));
 items=[];await t.loadRows(true);assert.ok(area.innerHTML.includes('Нет строк по выбранному фильтру'));
 assert.equal(t.state.segment,'without_email_phone');
 assert.ok(paths.every(p=>p.includes('segment=without_email_phone')));
 console.log('Missing email UI: all phones, escaping, empty refresh and filter preservation passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
