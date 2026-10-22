// Offline DOM interaction checks. Only synthetic metadata; no actual browser storage or HTTP.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const markup = fs.readFileSync('templates/local_accounts.html', 'utf8');
const script = fs.readFileSync('static/local-console/accounts.js', 'utf8');
const storageKey = 'chat2api.accounts.columns.v1';
const A = 'acct_' + 'a'.repeat(24), B = 'acct_' + 'b'.repeat(24);
const syntheticEmail = 'long.synthetic.account.for.layout@example.invalid';
let document;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {};
    this.className = ''; this.hidden = false; this.disabled = false; this.value = ''; this._text = ''; this.open = false; this.isConnected = true;
    this.classList = {toggle: (name, on) => { const values = new Set(this.className.split(' ').filter(Boolean)); on ? values.add(name) : values.delete(name); this.className = [...values].join(' '); }};
  }
  append(...nodes) { for (const node of nodes) this.children.push(...(node.tagName === 'fragment' ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(' '); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; } close() { this.open = false; }
  querySelectorAll(selector) {
    const all = this.children.flatMap((node) => [node, ...node.querySelectorAll('*')]);
    if (selector === '*') return all;
    const key = selector.startsWith('[data-') ? selector.slice(6, -1).replace(/-([a-z])/g, (_, char) => char.toUpperCase()) : null;
    return all.filter((node) => selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1)) : key ? key in node.dataset : node.tagName === selector);
  }
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
function boot(saved = null, storageError = false) {
  const ids = new Map([...markup.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  ids.set('accounts-logout', new Element('button'));
  const get = (id) => ids.get(id);
  document = {activeElement: null, hidden: false, getElementById: get, createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element('fragment')};
  const profile = {email: syntheticEmail, email_trust: 'unverified_claim', plan: 'plus', plan_trust: 'unverified_claim', token_type: 'access_token', authorization_status: 'verified_recently', expires_at: '2026-12-05T12:34:00Z', issued_at: '2026-10-22T01:00:00Z'};
  const quota = {remaining: 25, reset_at: '2026-12-01T00:00:00Z', status: 'known'};
  const account = (id) => ({id, label: id === A ? 'QA Alpha' : 'QA Beta', token_count: 1, credential_status: 'valid', status: 'stale', checked_at: '2026-10-22T01:00:00Z', refresh: {}, features: {reason: quota, image_gen: quota}, profile: id === A ? profile : {plan: 'unknown'}, local_usage: {windows: {last_24h: {attempts: 5, succeeded: 4, failed: 1, image_requests: 2, generated_images: 3}}}});
  let snapshot = {accounts: [account(A), account(B)], scheduler: {enabled: true, accounts: {}, events: []}};
  let stored = saved, failStore = storageError;
  const writes = [], requests = [], intervals = [], window = {
    location: {search: '', assign() {}}, listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval(fn, ms) { intervals.push({fn, ms}); }, ConsoleUI: {toast() {}},
    localStorage: {getItem(key) { assert.equal(key, storageKey); if (failStore) throw Error('storage denied'); return stored; }, setItem(key, value) { if (failStore) throw Error('storage denied'); writes.push({key, value}); stored = value; }},
  };
  vm.runInNewContext(script, {document, window, URLSearchParams, Intl, Date, Set, AbortController, TypeError, Error, fetch: async (path, options = {}) => {
    requests.push({path, options}); return {ok: true, status: 200, headers: {get() { return null; }}, json: async () => structuredClone(snapshot)};
  }});
  const click = async (item) => { const node = typeof item === 'string' ? get(item) : item; node.focus(); node.listeners.click(); await flush(); };
  const options = () => get('account-columns-options').querySelectorAll('[data-column-option]');
  const select = (id, on) => { const input = options().find((item) => item.dataset.columnOption === id); input.checked = on; input.listeners.change(); };
  const apply = async () => { get('account-columns-form').listeners.submit({preventDefault() {}}); await flush(); };
  return {get, click, options, select, apply, writes, requests, window, intervals,
    rows: () => get('accounts-rows').querySelectorAll('.account-row'),
    headers: () => get('accounts-table-head').children.map((item) => item.dataset.column),
    failStorage() { failStore = true; }, stored: () => stored,
    update(value) { snapshot = value; }, snapshot: () => snapshot};
}
(async () => {
  const env = boot(); await flush();
  assert.equal(env.headers().length, 11); assert.equal(env.headers()[0], 'identity'); assert.equal(env.headers().at(-1), 'actions');
  assert(!env.headers().includes('luna')); assert.equal(env.rows()[0].children.length, 11);
  assert(env.rows()[0].textContent.includes('凭据声明')); assert(env.rows()[0].textContent.includes('Plus'));
  assert(env.rows()[1].textContent.includes('等级未提供')); assert(!env.rows()[1].textContent.includes('Free'));
  const email = env.rows()[0].children.find((cell) => cell.dataset.column === 'email');
  assert.equal(email.querySelectorAll('.account-cell-primary')[0].title, syntheticEmail);
  const authorization = env.rows()[0].children.find((cell) => cell.dataset.column === 'authorization');
  assert(authorization.querySelectorAll('.account-cell-secondary')[0].title.includes('2026/12/05'));
  assert(authorization.querySelectorAll('.account-cell-secondary')[0].title.includes('不代表完整 Session'));
  const old = env.rows()[0].children.find((cell) => cell.dataset.column === 'reason_remaining').querySelectorAll('.quota-old-tag')[0];
  assert.equal(old.hidden, false); assert.equal(env.writes.length, 0);

  await env.click('accounts-columns'); assert.equal(env.get('account-columns-dialog').open, true);
  assert.equal(env.options().filter((input) => input.disabled).length, 2);
  env.select('email', false); await env.click('account-columns-cancel'); assert(env.headers().includes('email')); assert.equal(env.writes.length, 0);
  await env.click('accounts-columns'); await env.click('account-columns-all');
  assert.equal(env.headers().length, 11); await env.apply(); assert.equal(env.headers().length, 15);
  assert.equal(env.get('account-columns-dialog').open, false);
  assert.deepEqual(JSON.parse(env.stored()), env.headers().filter((id) => !['identity', 'actions'].includes(id)));
  assert(!env.stored().includes(syntheticEmail)); assert(!env.stored().includes(A));

  const checkbox = env.rows()[0].querySelectorAll('[data-select-account]')[0]; checkbox.checked = true; checkbox.listeners.change();
  await env.click(env.rows()[0].querySelectorAll('[data-focus-key]').find((item) => item.dataset.focusKey === `details:${A}`));
  assert.equal(env.get('accounts-rows').querySelectorAll('.account-detail-row')[0].children[0].colSpan, 15);
  await env.click('accounts-columns'); await env.click('account-columns-default');
  assert.equal(env.headers().length, 15);
  env.intervals.find((item) => item.ms === 5000).fn(); await flush();
  assert.equal(env.options().find((item) => item.dataset.columnOption === 'luna').checked, false);
  await env.apply(); assert.equal(env.headers().length, 11);
  assert.equal(env.rows()[0].querySelectorAll('[data-select-account]')[0].checked, true);
  assert.equal(env.get('accounts-rows').querySelectorAll('.account-detail-row')[0].children[0].colSpan, 11);
  assert(env.get('accounts-rows').textContent.includes('Token 声明到期'));

  await env.click('accounts-columns'); for (const item of env.options().filter((input) => !input.disabled)) env.select(item.dataset.columnOption, false);
  assert.equal(env.get('account-columns-apply').disabled, true); assert.equal(env.get('account-columns-error').hidden, false);
  const writesBefore = env.writes.length; await env.apply(); assert.equal(env.writes.length, writesBefore); assert.equal(env.headers().length, 11);
  env.select('email', true); env.failStorage(); await env.apply();
  assert.deepEqual(env.headers(), ['identity', 'email', 'actions']); assert.equal(env.get('accounts-columns-notice').hidden, false);
  assert(env.get('accounts-columns-notice').textContent.includes('无法保存')); assert.equal(env.get('account-columns-dialog').open, false);
  assert.equal(env.get('accounts-rows').querySelectorAll('.account-detail-row')[0].children[0].colSpan, 3);

  env.get('account-search').value = 'example.invalid'; env.get('account-search').listeners.input();
  assert.equal(env.rows().length, 1); assert.equal(env.window.location.search, '');
  assert(env.requests.every((item) => !item.path.includes('example.invalid')));

  const restored = boot(JSON.stringify(['email', 'unknown-column', 'email', syntheticEmail])); await flush();
  assert.deepEqual(restored.headers(), ['identity', 'email', 'actions']); assert.equal(restored.writes.length, 0);
  await restored.click('accounts-columns'); const cancelEvent = {preventDefault() { this.prevented = true; }};
  restored.get('account-columns-dialog').listeners.cancel(cancelEvent); assert(cancelEvent.prevented); assert.equal(restored.get('account-columns-dialog').open, false);
  for (const saved of ['[]', '{}', 'invalid JSON']) { const reset = boot(saved); await flush(); assert.equal(reset.headers().length, 11); }
  const blocked = boot(null, true); await flush(); assert.equal(blocked.headers().length, 11); assert.equal(blocked.get('accounts-columns-notice').hidden, false);
  console.log('PASS account columns: profile semantics, fixed/default/all/custom columns, apply/cancel, empty guard, filtered storage, failure fallback, dynamic colspan, preserved selection/details, polling-safe draft, private search.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
