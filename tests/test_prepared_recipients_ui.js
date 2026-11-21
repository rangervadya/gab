// Synthetic recipient selection only. No server, SMTP or webhook is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', innerHTML: '', textContent: '', hidden: false, disabled: false,
    listeners: {}, classList: {add() {}, remove() {}},
    setAttribute(name, value) { this[name] = value; },
    addEventListener(event, callback) { this.listeners[event] = callback; },
  });
  return elements.get(id);
}
const calls = [];
const document = {getElementById: element, querySelectorAll: () => []};
const context = {document, console, Headers, FormData,
  setTimeout: () => 0, clearTimeout() {},
  fetch: async (path, options = {}) => {
    calls.push({path, options});
    assert.ok(!path.endsWith('/bulk-send') && !path.endsWith('/test-send'));
    let data = {items: []};
    if (path === '/api/mailings/99') data = {
      mailing: {id: 99, name: 'Synthetic draft', subject: 'Synthetic subject'},
      attachments: [], recipients: [{id: 501, investor_id: null,
        review_record_id: null, recipient_name: '<img src=x>', email: 'one@example.test',
        revenue_segment_eligible: true, company_revenue: {amount_rub:'100000000',year:2025}},
        {id:502, investor_id:null, review_record_id:null, recipient_name:'Other company', email:'two@example.test'}],
    };
    if (path.endsWith('/audience')) data = JSON.parse(options.body);
    if (path.endsWith('/bulk-status')) data = {started: false};
    if (options.method === 'POST') data = {selected: 1, with_email: 1, without_email: 0, registry_selected: 0};
    return {ok: true, headers: {get: () => 'application/json'}, json: async () => data};
  },
};
vm.runInNewContext(fs.readFileSync('app/static/mailings.js', 'utf8') +
  '\nglobalThis.testing = {openMailing, saveRecipients, setRecipientScope};', context);
(async () => {
  await context.testing.openMailing(99);
  assert.equal(element('preparedRecipientsPanel').hidden, false);
  assert.equal(element('preparedRecipientCount').textContent, '2 выбрано');
  assert.ok(element('preparedRecipientList').innerHTML.includes('&lt;img src=x&gt;'));
  await context.testing.setRecipientScope('revenue_100m');
  assert.equal(element('preparedRecipientCount').textContent, '1 выбрано');
  assert.ok(!element('preparedRecipientList').innerHTML.includes('two@example.test'));
  await context.testing.saveRecipients();
  let sent = JSON.parse(calls.filter(c => c.path.endsWith('/recipients') && c.options.method === 'POST').at(-1).options.body);
  assert.deepEqual(sent.prepared_recipient_ids, [501,502]);
  await context.testing.setRecipientScope('all');
  assert.ok(element('preparedRecipientList').innerHTML.includes('two@example.test'));
  const checkbox = {dataset: {preparedRecipient: '501'}, checked: false};
  element('preparedRecipientList').listeners.change({target: {closest: () => checkbox}});
  await context.testing.saveRecipients();
  sent = JSON.parse(calls.filter(c => c.path.endsWith('/recipients') && c.options.method === 'POST').at(-1).options.body);
  assert.deepEqual(sent.prepared_recipient_ids, [502]);
  assert.equal(element('preparedRecipientCount').textContent, '1 выбрано');
  assert.ok(calls.filter(c => c.path.endsWith('/audience')).every(c => c.options.method === 'PATCH'));
  console.log('Prepared UI: revenue group preserves hidden recipients; restore all, escaping and manual deselection passed; no send endpoints called.');
})().catch(error => { console.error(error); process.exitCode = 1; });
