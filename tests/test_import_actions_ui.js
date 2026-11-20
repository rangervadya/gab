// Functional UI tests: synthetic DOM/API only, no provider requests or real CRM writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map(),calls=[];
const element=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',checked:false,disabled:true,hidden:false,querySelectorAll:()=>[]});return elements.get(id)};
const document={getElementById:element};let refreshed=0;
const window={};
let endpoint=async(url,body)=>{
 if(url.endsWith('/configuration'))return {dadata_configured:true,checko_configured:true};
 if(url.includes('/rows?'))return {total:2,items:[{id:1,import_status:'pending',company_name:'Synthetic',edit_version:0},{id:2,import_status:'pending',company_name:'Blocked'}]};
 if(url.endsWith('/confirm-preview'))return {token:'synthetic-preview',items:[{row_id:1,ready:true,company_name:'Synthetic <unsafe>',emails:['synthetic@example.test'],company_emails:[],reasons:[],warnings:[]},{row_id:2,ready:false,reasons:['revenue_below_100m'],warnings:[],emails:[],company_emails:[]}]};
 if(url.endsWith('/confirm-all'))return {transferred:1,items:[{status:'created'}]};
 if(url.endsWith('/search'))return {import_row_id:1,source_name:'Synthetic <unsafe>',version:0,candidates:[{source_inn_match:true,candidate:{inn:'9900001003',legal_name:'Synthetic <unsafe>',provider:'dadata'}}]};
 if(url.endsWith('/checko'))return {item:{inn:'9900001003',legal_name:'Synthetic',provider:'checko',emails:[]}};
 throw new Error('Unexpected URL '+url);
};
vm.runInNewContext(fs.readFileSync('app/static/import-actions.js','utf8'),{window,document,console,fetch:async(url,options)=>{
 const body=options.body?JSON.parse(options.body):null;calls.push({url,body});return {ok:true,json:()=>endpoint(url,body)};
}});
(async()=>{
 window.GabImportActions.mount({id:8,status:'review'},async()=>{refreshed++;});
 await new Promise(setImmediate);
 assert.deepEqual(calls.map(c=>c.url),['/api/mailings/registry-enrichment/imports/configuration']);
 await element('importPrepareAll').onclick();
 assert.ok(element('importBulkPreview').innerHTML.includes('Synthetic &lt;unsafe&gt;'));
 assert.equal(element('importConfirmAll').disabled,true);
 await element('importConfirmAll').onclick();
 assert.equal(calls.filter(c=>c.url.endsWith('/confirm-all')).length,0);
 element('importBulkReviewed').checked=true;element('importBulkReviewed').onchange();
 assert.equal(element('importConfirmAll').disabled,false);
 await element('importConfirmAll').onclick();
 const sent=calls.filter(c=>c.url.endsWith('/confirm-all'));
 assert.equal(sent.length,1);assert.equal(sent[0].body.reviewed,true);assert.equal(refreshed,1);
 await element('importConfirmAll').onclick();
 assert.equal(calls.filter(c=>c.url.endsWith('/confirm-all')).length,1);
 await element('importFindContacts').onclick();
 assert.ok(calls.some(c=>c.url.endsWith('/search')));
 assert.ok(calls.some(c=>c.url.endsWith('/checko')));
 assert.equal(calls.filter(c=>c.url.endsWith('/confirm')).length,0);
 console.log('Import actions UI: no lookup/transfer on load, explicit review, preview escaping, no automatic provider approval or double transfer OK');
})().catch(error=>{console.error(error);process.exitCode=1});
