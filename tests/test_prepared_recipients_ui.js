// Synthetic recipient selection only. No server, SMTP or webhook is contacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', innerHTML: '', textContent: '', hidden: false, disabled: false,
    listeners: {}, classList: {add() {}, remove() {}},
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
        review_record_id: null, recipient_name: '<img src=x>', email: 'one@example.test'}],
    };
    if (path.endsWith('/bulk-status')) data = {started: false};
    if (options.method === 'POST') data = {selected: 1, with_email: 1, without_email: 0, registry_selected: 0};
    return {ok: true, headers: {get: () => 'application/json'}, json: async () => data};
  },
};
vm.runInNewContext(fs.readFileSync('app/static/mailings.js', 'utf8') +
  '\nglobalThis.testing = {openMailing, saveRecipients};', context);
(async () => {
  await context.testing.openMailing(99);
  assert.equal(element('preparedRecipientsPanel').hidden, false);
  assert.equal(element('preparedRecipientCount').textContent, '1 выбрано');
  assert.ok(element('preparedRecipientList').innerHTML.includes('&lt;img src=x&gt;'));
  await context.testing.saveRecipients();
  let sent = JSON.parse(calls.filter(c => c.path.endsWith('/recipients') && c.options.method === 'POST').at(-1).options.body);
  assert.deepEqual(sent.prepared_recipient_ids, [501]);
  const checkbox = {dataset: {preparedRecipient: '501'}, checked: false};
  element('preparedRecipientList').listeners.change({target: {closest: () => checkbox}});
  await context.testing.saveRecipients();
  sent = JSON.parse(calls.filter(c => c.path.endsWith('/recipients') && c.options.method === 'POST').at(-1).options.body);
  assert.deepEqual(sent.prepared_recipient_ids, []);
  assert.equal(element('preparedRecipientCount').textContent, '0 выбрано');
  console.log('Prepared recipient UI: load, escaping, save and deselection passed; no send endpoints called.');
})().catch(error => { console.error(error); process.exitCode = 1; });
