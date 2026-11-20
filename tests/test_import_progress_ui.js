// Functional polling regression: synthetic responses and minimal DOM, no browser/server.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let source=fs.readFileSync('app/static/imports.js','utf8');
source=source.replace('  window.loadImportsView = async function()',
 '  window.testProgress={state,refreshProcessingProgress};\n  window.loadImportsView = async function()');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false});return elements.get(id)};
const document={getElementById:element};
let response={status:'processing',stage:'llm',batch_id:8,total:8,completed:6,percent:75,
 fragment:{page:7,completed:2,total:5},elapsed_seconds:120,stage_eta_seconds:null,can_cancel:true,error:null};
const window={};
vm.runInNewContext(source,{document,window,console,fetch:async()=>({ok:true,json:async()=>response}),setTimeout:()=>0,clearTimeout:()=>{}});
const {state,refreshProcessingProgress}=window.testProgress;
state.selectedBatchId=8;state.batches=[{id:8,status:'uploaded',original_filename:'Synthetic.pdf'}];
(async()=>{
 await refreshProcessingProgress(8);
 assert.ok(element('importProcessingProgress').innerHTML.includes('готово фрагментов 2 из 5'));
 assert.equal(element('importBatchStatus').textContent,'Обрабатывается');
 assert.equal(element('importCancelBtn').hidden,false);
 assert.equal(element('importRetryBtn').hidden,true);
 response={...response,status:'failed',stage:'failed',can_cancel:false,error:'Synthetic timeout'};
 await refreshProcessingProgress(8);
 assert.equal(element('importBatchStatus').textContent,'Ошибка');
 assert.equal(element('importRetryBtn').hidden,false);
 assert.equal(element('importCancelBtn').hidden,true);
 assert.ok(element('importProcessingProgress').innerHTML.includes('Synthetic timeout'));
 console.log('Progress UI: fragment counts, live header and retry/stop states OK');
})().catch(error=>{console.error(error);process.exitCode=1});
