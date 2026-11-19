// Artificial data only; executes the shipped Code nodes and pagination expressions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const workflow=JSON.parse(fs.readFileSync('n8n/workflows/GAB02InvImp20261.json','utf8'))[0];
const nodes=Object.fromEntries(workflow.nodes.map(n=>[n.name,n]));
for(const n of workflow.nodes)if(n.type==='n8n-nodes-base.code')new Function(n.parameters.jsCode);
const root={ok:true,pages:Array.from({length:8},(_,i)=>({page:i+1,raw_text:'Synthetic company '+(i+1)}))};
const run=(code,context)=>vm.runInNewContext('(function(){'+code+'})()',context);
const prepared=run(nodes['Prepare PDF pages'].parameters.jsCode,{$input:{first:()=>({json:root})},$:()=>({first:()=>({json:{batch_id:8}})})});
assert.equal(prepared.length,1);assert.equal(prepared[0].json.pages.length,8);
const pagination=nodes['Ollama extract investors'].parameters.options.pagination.pagination;
const evaluate=(expression,context)=>vm.runInNewContext(expression.slice(3,-2),context);
const outputs=[];
for(let pageCount=0;pageCount<8;pageCount++){
  const context={$json:prepared[0].json,$pageCount:pageCount};
  const parameters=Object.fromEntries(pagination.parameters.parameters.map(p=>[p.name,evaluate(p.value,context)]));
  assert.equal(parameters.source_page,pageCount+1);
  assert.equal(typeof parameters.ollama_request,'object');
  assert.ok(parameters.ollama_request.messages[1].content.includes('Synthetic company '+(pageCount+1)));
  const result={_gab_source_page:pageCount+1,_gab_last_page:pageCount===7,message:{content:JSON.stringify({records:[{company_name:'Synthetic '+(pageCount+1)}]})}};
  assert.equal(evaluate(pagination.completeExpression,{$response:{body:result}}),pageCount===7);
  outputs.push({json:result});
}
const parse=(items)=>run(nodes['Parse PDF records'].parameters.jsCode,{$input:{all:()=>items},$:()=>({first:()=>prepared[0]})});
// Reverse arrival order: records must still refer to the actual source page.
const parsed=parse([...outputs].reverse());
assert.equal(parsed[0].json.records[0].source_page,8);
assert.equal(parsed[0].json.records[0].company_name,'Synthetic 8');
assert.throws(()=>parse(outputs.slice(0,7)),/Не все страницы/);
assert.throws(()=>parse([...outputs.slice(0,7),outputs[0]]),/повторный/);
console.log('Workflow: 8 pages, one pagination input, correct requests and stop, page identities and completeness OK');
