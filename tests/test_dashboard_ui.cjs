// Offline DOM interaction checks. All requests use synthetic in-memory fixtures.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let document;
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.nodeType = 1; this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.style = {}; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.open = false; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  get firstElementChild() { return this.children[0]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  focus() { document.activeElement = this; this.listeners.focus?.(); }
  querySelectorAll(selector) { const all = this.children.flatMap((child) => [child, ...child.querySelectorAll('*')]); return selector === '*' ? all : all.filter((child) => selector.split(', ').includes(child.tagName)); }
}
const markup = fs.readFileSync('templates/local_dashboard.html', 'utf8');
const ids = new Map([...markup.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
ids.set('dashboard-logout', new Element('button'));
const get = (name) => ids.get('dashboard-' + name);
const days = ['7', '30', '90', 'custom'].map((value) => { const node = new Element('button'); node.dataset.days = value; return node; });
const trends = ['requests', 'cost'].map((value) => { const node = new Element('button'); node.dataset.trend = value; return node; });
document = {
  getElementById(id) { return ids.get(id) || [...ids.values()].flatMap((node) => node.querySelectorAll('*')).find((node) => node.id === id); },
  createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  querySelectorAll: (selector) => selector === '[data-days]' ? days : selector === '[data-trend]' ? trends : [],
};
const knownKey = 'key_' + 'a'.repeat(24);
const aggregate = (changes = {}) => ({requests: 2, succeeded: 1, failed: 1, cancelled: 0, interrupted: 0, in_progress: 0, prompt_tokens: 100, completion_tokens: 50, generated_images: 1, image_requests: 1, estimated_cost_usd: 0.125, estimated_text_cost_usd: 0.125, estimated_image_cost_usd: 0, known_cost_requests: 1, unknown_cost_requests: 1, token_unknown_requests: 1, avg_duration_ms: 1200, success_rate: .5, ...changes});
let fixture = {
  range: {date_from: '2026-10-01', date_to: '2026-10-30', timezone: 'Asia/Shanghai', days: 30},
  filters: {}, total: aggregate(), coverage: {known_cost_ratio: .5, known_cost_requests: 1, unknown_cost_requests: 1, token_known_requests: 1, token_unknown_requests: 1},
  daily: [{date: '2026-10-01', ...aggregate({requests: 3})}, {date: '2026-10-02', ...aggregate({known_cost_requests: 0, estimated_cost_usd: 0, unknown_cost_requests: 2})}, {date: '2026-10-03', ...aggregate({requests: 0, known_cost_requests: 0, estimated_cost_usd: 0, unknown_cost_requests: 0})}],
  hourly: Array.from({length: 24}, (_, hour) => ({hour, ...aggregate({requests: hour === 12 ? 3 : 0})})),
  models: [{model: 'auto', ...aggregate()}], keys: [{key_id: knownKey, ...aggregate()}],
  statuses: [{status: 'succeeded', ...aggregate({requests: 1})}, {status: 'failed', ...aggregate({requests: 1})}],
  recent: [{id: 'req_synthetic', key_id: knownKey, requested_model: 'auto', kind: 'chat', status: 'failed', started_at: '2026-10-01T00:00:00+00:00', duration_ms: 1200, http_status: 502, prompt_tokens: null, completion_tokens: null, generated_images: 0, estimated_cost_usd: null, pricing_status: 'unknown'}],
  options: {models: ['auto', 'gpt-synthetic'], keys: [knownKey]}, since: '2026-10-01T00:00:00+00:00', legacy: {included: false, message: '历史记录未回填。'},
};
let prices = {currency: 'USD', rates: [], custom_rates: [], notice: 'Only estimates'};
let mode = 'ok', calls = [];
const window = {location: {search: '?api_key_id=' + knownKey, assign(value) { this.navigation = value; }}, history: {replaceState(_, __, path) { window.location.search = path.slice(path.indexOf('?')); }}, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }, setTimeout() { return 1; }, clearTimeout() {}, requestAnimationFrame(fn) { fn(); return 1; }, ConsoleUI: {toast() {}}};
const context = vm.createContext({window, document, URLSearchParams, AbortController, Date, Intl, Error, Number, fetch: async (path, options = {}) => {
  calls.push({path, options});
  let code = mode === 'expired' ? 401 : mode === 'failed' ? 503 : 200;
  let data = path === '/local-console/api-keys' ? {keys: [{id: knownKey, name: '<script>client</script>'}]} : path.includes('/pricing') ? prices : fixture;
  if (path.includes('/pricing') && options.method === 'PUT') {
    if (mode === 'price-failed') { code = 422; data = {detail: '价格无效'}; }
    else { const value = JSON.parse(options.body); prices = {...prices, custom_rates: value.custom_rates.map((rate) => ({...rate, label: rate.label || 'Synthetic price'}))}; data = prices; }
  }
  return {ok: code < 400, status: code, json: async () => structuredClone(data)};
}});
vm.runInContext(fs.readFileSync('static/local-console/dashboard.js', 'utf8'), context);
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const click = async (node) => { (typeof node === 'string' ? get(node) : node).listeners.click(); await flush(); };
const text = (node) => node.textContent + node.children.map(text).join(' ');
const put = (id, value) => { const node = document.getElementById(id); node.value = value; node.listeners.input?.(); return node; };
(async () => {
  await flush();
  assert.equal(get('content').hidden, false);
  for (const id of ['trend-chart', 'hours-chart']) {
    const labels = get(id).querySelectorAll('text').filter((node) => node.attrs['text-anchor'] === 'end').map((node) => node.textContent);
    assert.deepEqual(labels, ['0', '2', '4']);
    const bar = get(id).querySelectorAll('rect').find((node) => node.attrs.class === 'dashboard-bar' && Number(node.attrs.height) > 0);
    assert.equal(Number(bar.attrs.height), 139.5); // 3 requests / axis maximum 4 × 186px.
  }
  assert(calls[0].path === '/local-console/api-keys');
  assert(calls.some((call) => call.path.includes('api_key_id=' + knownKey)));
  assert(text(get('metrics')).includes('$0.125'));
  assert(text(get('metrics')).includes('1 次未估值'));
  assert.equal(get('recording-warning').hidden, true);
  fixture = {...fixture, recording_status: 'incomplete'}; await click('refresh');
  assert.equal(get('recording-warning').hidden, false);
  fixture = {...fixture, recording_status: 'ok'}; await click('refresh');
  assert.equal(get('recording-warning').hidden, true);
  assert(text(get('request-table')).includes('未估值'));
  assert(text(get('key-table')).includes('<script>client</script>'));
  assert(!get('key-table').querySelectorAll('*').some((node) => node.tagName === 'script'));
  await click(trends[1]);
  assert.equal(get('trend-title').textContent, '每日 API 等价估值');
  assert(get('trend-chart').querySelectorAll('line').some((node) => node.attrs.class === 'dashboard-unknown-mark'));
  const points = get('trend-chart').querySelectorAll('g');
  points[0].listeners.keydown({key: 'ArrowRight', preventDefault() {}});
  assert.equal(document.activeElement, points[1]);
  assert(get('trend-tooltip').textContent.includes('未估值'));
  assert(!get('trend-tooltip').textContent.includes('$0.00'));
  points[2].listeners.focus(); assert(get('trend-tooltip').textContent.includes('0 请求 · $0.00'));
  assert(text(get('daily-table')).includes('$0.00'));
  const moneyLabels = get('trend-chart').querySelectorAll('text').filter((node) => node.attrs['text-anchor'] === 'end').map((node) => node.textContent);
  assert.deepEqual(moneyLabels, ['$0', '$0.0625', '$0.125']);
  const beforeInvalid = calls.length;
  await click(days[3]);
  get('start').value = '2026-10-30'; get('end').value = '2026-10-01';
  get('filters').listeners.submit({preventDefault() {}}); await flush();
  assert.equal(calls.length, beforeInvalid); assert(get('error-text').textContent.includes('早于'));
  await click('reset');
  mode = 'failed'; await click('refresh');
  assert.equal(get('error').hidden, false); assert(text(get('metrics')).includes('$0.125'));
  assert(get('error-text').textContent.includes('保留'));
  mode = 'ok'; fixture = {...fixture, total: aggregate({requests: 0, known_cost_requests: 0, unknown_cost_requests: 0}), recent: [], daily: [], hourly: [], models: [], keys: [], statuses: [], coverage: {known_cost_requests: 0, unknown_cost_requests: 0, token_known_requests: 0, token_unknown_requests: 0, known_cost_ratio: 0}};
  await click('refresh'); assert.equal(get('empty').hidden, false); assert(text(get('metrics')).includes('$0.00')); assert(text(get('metrics')).includes('未知')); assert.equal(get('metrics').children[1].children[1].textContent, '$0.00');
  get('pricing').open = true; get('pricing').listeners.toggle(); await flush();
  assert.equal(get('price-add').disabled, false);
  await click('price-add');
  put('dashboard-rate-0-model', 'gpt-synthetic');
  put('dashboard-rate-0-input_per_million_usd', '0.123456789');
  put('dashboard-rate-0-output_per_million_usd', '1.5');
  const dirtyEvent = {preventDefault() { this.prevented = true; }}; window.listeners.beforeunload(dirtyEvent); assert(dirtyEvent.prevented);
  mode = 'price-failed'; await click('price-save'); assert.equal(get('pricing-error').hidden, false); assert.equal(document.getElementById('dashboard-rate-0-input_per_million_usd').value, '0.123456789');
  mode = 'ok'; await click('price-save');
  const saved = JSON.parse(calls.findLast((call) => call.options.method === 'PUT').options.body).custom_rates[0];
  assert.equal(saved.input_per_million_usd, '0.123456789'); assert.equal(saved.cached_input_per_million_usd, null); assert.equal(saved.image_per_unit_usd, null);
  assert.equal(get('price-save').disabled, true);
  put('dashboard-rate-0-output_per_million_usd', '1.6'); await click('price-save');
  assert.equal(JSON.parse(calls.findLast((call) => call.options.method === 'PUT').options.body).custom_rates[0].label, 'Synthetic price');
  const remove = get('pricing-editor').querySelectorAll('button')[0]; await click(remove); await click('price-save');
  assert.deepEqual(JSON.parse(calls.findLast((call) => call.options.method === 'PUT').options.body), {custom_rates: []});
  mode = 'expired'; await click('refresh'); assert.equal(get('login').hidden, false); assert.equal(get('refresh').disabled, true);
  console.log('PASS dashboard UI: safe rendering, key filter, known/unknown amounts, image and token separation, chart keyboard/unknown gaps, invalid dates, preserved errors, empty data, price CRUD/precision/nulls, save failure and session expiry.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
