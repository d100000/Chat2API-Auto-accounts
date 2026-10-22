// Offline UI checks: synthetic accounts only; no HTTP or credential files.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let document;
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {}; this.className = ''; this.hidden = false; this.disabled = false; this.value = ''; this._text = ''; this.open = false; this.isConnected = true; this.classList = {toggle: (name, on) => { const values = new Set(this.className.split(' ').filter(Boolean)); on ? values.add(name) : values.delete(name); this.className = [...values].join(' '); }}; }
  append(...nodes) { for (const node of nodes) this.children.push(...(node.tagName === 'fragment' ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(' '); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { document.activeElement = this; }
  showModal() { this.open = true; } close() { this.open = false; }
  querySelectorAll(selector) { const all = this.children.flatMap((node) => [node, ...node.querySelectorAll('*')]); if (selector === '*') return all; const key = selector.startsWith('[data-') ? selector.slice(6, -1).replace(/-([a-z])/g, (_, char) => char.toUpperCase()) : null; return all.filter((node) => selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1)) : key ? key in node.dataset : node.tagName === selector); }
}
const markup = fs.readFileSync('templates/local_accounts.html', 'utf8');
const ids = new Map([...markup.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()])); ids.set('accounts-logout', new Element('button'));
for (const feature of ['reason', 'image_gen']) ids.get(`concurrency-${feature}-mode`).options = ['keep', 'inherit', 'custom'].map((value) => ({value}));
const get = (id) => ids.get(id);
document = {activeElement: null, hidden: false, getElementById: get, createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element('fragment')};
const A = 'acct_' + 'a'.repeat(24), B = 'acct_' + 'b'.repeat(24), C = 'acct_' + 'c'.repeat(24);
const quota = {remaining: 9, reset_at: '2026-12-01T00:00:00Z', status: 'known'};
const account = (id, label, check, present = true) => ({id, label, token_count: 2, status: 'fresh', credential_status: 'valid', checked_at: new Date().toISOString(), features: {reason: quota, image_gen: quota}, refresh: {}, local_usage: null, concurrency: {reason: {active: 1, limit: 3, source: 'default', state: 'available'}, image_gen: {in_flight: 3, limit: 2, source: 'account', state: 'draining'}}, model_catalog: {status: 'fresh', checked_at: new Date().toISOString(), models: present ? ['gpt-6-luna'] : ['auto'], checks: {'gpt-6-luna': check}}});
let snapshot = {accounts: [account(A, 'Alpha', {status: 'model_mismatch', requested_model: 'gpt-6-luna', sent_model: 'gpt-6-luna', reported_model: 'gpt-6'}), account(B, '<img src=x> Beta', {status: 'verified', reported_model: 'gpt-6-luna'}), account(C, 'Gamma', {status: 'unverified'}, false)], scheduler: {enabled: true, accounts: {}, events: []}};
let config = {revision: 2, defaults: {reason: 3, image_gen: 1}, bounds: {min: 1, max: 10}, accounts: Object.fromEntries([A, B, C].map((id) => [id, {overrides: {reason: null, image_gen: 2}, effective: {reason: 3, image_gen: 2}}]))};
let mode = 'ok', calls = [], intervals = [], timers = new Map(), sequence = 0;
const window = {localStorage: {getItem() { return null; }, setItem() {}}, location: {search: '', assign() {}}, listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }, setTimeout(fn, ms) { timers.set(++sequence, {fn, ms}); return sequence; }, clearTimeout(id) { timers.delete(id); }, setInterval(fn, ms) { intervals.push({fn, ms}); }, ConsoleUI: {toast() {}}};
const context = vm.createContext({document, window, URLSearchParams, Intl, Date, Set, AbortController, TypeError, Error, fetch: async (path, options = {}) => {
  calls.push({path, options}); let status = mode === 'expired' ? 401 : options.method === 'PATCH' && mode === 'conflict' ? 409 : options.method === 'PATCH' && mode === 'failed' ? 503 : 200;
  if (options.method === 'PATCH' && mode === 'timeout') throw Object.assign(new Error('timeout'), {name: 'AbortError'});
  let data = path === '/local-console/accounts/concurrency' || options.method === 'PATCH' ? config : snapshot;
  if (status === 200 && options.method === 'PATCH') { const body = JSON.parse(options.body); config = {...config, revision: config.revision + 1, defaults: body.limits ? {...config.defaults, ...body.limits} : config.defaults}; data = config; }
  return {ok: status < 400, status, headers: {get() { return null; }}, json: async () => structuredClone(data)};
}});
vm.runInContext(fs.readFileSync('static/local-console/accounts.js', 'utf8'), context);
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const rows = () => get('accounts-rows').querySelectorAll('.account-row');
const click = async (node) => { node = typeof node === 'string' ? get(node) : node; node.focus(); node.listeners.click(); await flush(); };
const change = (id, value) => { const node = get(id); node.value = value; node.listeners.change?.(); node.listeners.input?.(); };
const save = async () => { get('concurrency-form').listeners.submit({preventDefault() {}}); await flush(); };
(async () => {
  await flush(); await click('accounts-columns'); await click('account-columns-all'); get('account-columns-form').listeners.submit({preventDefault() {}}); await flush(); assert.equal(rows().length, 3); assert(rows()[0].textContent.includes('聊天 1 / 3')); assert(rows()[0].textContent.includes('待结束')); assert(rows()[0].textContent.includes('模型不一致')); assert(rows()[1].textContent.includes('模型标签一致')); assert(rows()[2].textContent.includes('目录未发现')); assert.equal(get('accounts-rows').querySelectorAll('img').length, 0);
  snapshot.accounts[1].model_catalog.checks['gpt-6-luna'].stale = true; await click('reload-accounts'); assert(rows()[1].textContent.includes('历史核查已过期')); assert(!rows()[1].textContent.includes('模型标签一致')); snapshot.accounts[1].model_catalog.checks['gpt-6-luna'].stale = false;
  await click('concurrency-defaults'); assert.equal(get('concurrency-dialog').open, true); assert.equal(get('concurrency-reason-value').value, '3'); assert.equal(get('concurrency-image_gen-value').value, '1');
  change('concurrency-reason-value', '0'); await save(); assert(get('concurrency-error').textContent.includes('1–10')); assert.equal(calls.filter((call) => call.options.method === 'PATCH').length, 0);
  change('concurrency-reason-value', '4'); await save(); assert.equal(get('concurrency-dialog').open, false); assert.deepEqual(JSON.parse(calls.findLast((call) => call.options.method === 'PATCH').options.body).limits, {reason: 4, image_gen: 1});
  const select = rows()[0].querySelectorAll('[data-select-account]')[0]; select.checked = true; select.listeners.change(); assert.equal(get('accounts-select-all').indeterminate, true);
  change('account-search', 'Gamma'); assert.equal(get('concurrency-batch').disabled, true); assert.equal(rows().length, 1);
  get('accounts-select-all').checked = true; get('accounts-select-all').listeners.change(); await click('concurrency-batch'); assert.equal(get('concurrency-reason-mode').value, 'keep');
  change('concurrency-image_gen-mode', 'custom'); change('concurrency-image_gen-value', '5');
  snapshot = {...snapshot, generated_at: Date.now()}; intervals.find((item) => item.ms === 5000).fn(); await flush(); assert.equal(get('concurrency-image_gen-value').value, '5');
  const leave = {preventDefault() { this.prevented = true; }}; window.listeners.beforeunload(leave); assert(leave.prevented);
  mode = 'failed'; await save(); assert.equal(get('concurrency-dialog').open, true); assert.equal(get('concurrency-image_gen-value').value, '5'); assert.equal(get('concurrency-save').disabled, false);
  mode = 'conflict'; await save(); assert.equal(get('concurrency-save').disabled, true); assert.equal(get('concurrency-image_gen-value').value, '5'); assert.equal(get('concurrency-reload').hidden, false);
  mode = 'ok'; await click('concurrency-reload'); change('concurrency-image_gen-mode', 'inherit'); await save();
  const batch = JSON.parse(calls.findLast((call) => call.options.method === 'PATCH').options.body); assert.deepEqual(batch.account_ids, [C]); assert.deepEqual(batch.overrides, {image_gen: null});
  await click('clear-filters'); const button = rows()[0].querySelectorAll('[data-concurrency-account]')[0]; await click(button); assert.equal(get('concurrency-reason-mode').value, 'inherit'); assert.equal(get('concurrency-image_gen-mode').value, 'custom');
  change('concurrency-image_gen-value', '4'); mode = 'timeout'; await save(); assert(get('concurrency-error').textContent.includes('尚未确认')); assert.equal(get('concurrency-save').disabled, true);
  mode = 'ok'; await click('concurrency-cancel'); await click('models-refresh-all'); assert(calls.some((call) => call.path === '/local-console/accounts/models/refresh')); assert(!calls.some((call) => call.path.includes('/v1/')));
  mode = 'expired'; await click('concurrency-defaults'); assert.equal(get('session-expired').hidden, false); assert.equal(get('concurrency-save').disabled, true);
  console.log('PASS concurrency UI: limits, defaults, independent overrides, filtered selection, frozen batch IDs, polling-safe draft, failed/409/timeout/401, unknown Luna vs mismatch, metadata-only refresh.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
