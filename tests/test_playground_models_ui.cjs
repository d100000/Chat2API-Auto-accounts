// Model defaults and endpoint boundaries using synthetic DOM/session/responses only.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('templates/local_console.html', 'utf8');
const script = fs.readFileSync('static/local-console/console.js', 'utf8');
let document;
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {}; this.value = ''; this.textContent = ''; this.className = ''; this.hidden = false; this.disabled = false; this.open = false; this.scrollHeight = this.clientHeight = this.scrollTop = 0; }
  append(...items) { this.children.push(...items); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  focus() { document.activeElement = this; }
  querySelectorAll(selector) { const all = this.children.flatMap((node) => [node, ...node.querySelectorAll('*')]); return selector === '*' ? all : all.filter((node) => selector.startsWith('.') ? node.className.split(' ').includes(selector.slice(1)) : node.tagName === selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function boot(search = '') {
  const ids = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map((match) => {
    const node = new Element(match[0].match(/^<(\w+)/)[1]); node.id = match[1];
    node.value = match[0].match(/\bvalue="([^"]*)"/)?.[1] || '';
    node.checked = /\bchecked\b/.test(match[0]);
    return [match[1], node];
  }));
  // These nodes are rendered by the shared shell; avoid loading any real session.
  for (const id of ['current-user', 'logout-button']) if (!ids.has(id)) ids.set(id, new Element());
  ids.get('request-mode').value = 'text'; ids.get('history-mode').value = 'default'; ids.get('stream').checked = false;
  const get = (id) => ids.get(id);
  document = {activeElement: null, getElementById: get, createElement: (tag) => new Element(tag)};
  const calls = [], window = {location: {search, assign() {}}, matchMedia() { return {matches: false, addEventListener() {}}; }, setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 2; }, clearInterval() {}, ConsoleUI: {toast() {}, copyText() { return true; }}};
  vm.runInNewContext(script, {document, window, navigator: {platform: 'test'}, performance: {now: () => 100}, URL, URLSearchParams, AbortController, Error, DOMException,
    fetch: async (path, options = {}) => {
      calls.push({path, options});
      return {status: 200, ok: true, headers: {get() { return 'application/json'; }}, json: async () => ({username: 'synthetic', token_count: 1, error_token_count: 0, default_model: 'gpt-6-luna'}), text: async () => JSON.stringify({choices: [{message: {content: 'Synthetic OK'}, finish_reason: 'stop'}]})};
    }});
  const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((resolve) => setImmediate(resolve)); };
  const click = async (id) => { get(id).listeners.click(); await flush(); };
  const send = async () => { get('prompt').value = 'Synthetic test'; get('chat-form').listeners.submit({preventDefault() {}}); await flush(); };
  const chats = () => calls.filter((item) => item.path === '/local-console/chat').map((item) => JSON.parse(item.options.body));
  return {get, calls, flush, click, send, chats};
}
(async () => {
  assert(html.includes('value="gpt-6-luna" list="chat-model-options"'));
  const datalist = html.match(/<datalist id="chat-model-options">([\s\S]*?)<\/datalist>/)[1];
  assert.deepEqual([...datalist.matchAll(/value="([^"]+)"/g)].map((match) => match[1]), ['gpt-6-luna', 'auto']);
  const env = boot(); await env.flush();
  assert.equal(env.get('model').value, 'gpt-6-luna');
  assert(env.get('configuration-summary').textContent.includes('gpt-6-luna'));
  await env.send(); assert.equal(env.chats().at(-1).model, 'gpt-6-luna'); assert.equal(env.get('request-state').textContent, '请求成功');
  env.get('model').value = 'auto'; await env.send(); assert.equal(env.chats().at(-1).model, 'auto');
  env.get('model').value = 'gpt-custom-chat'; await env.click('mode-image');
  assert.equal(env.get('model').value, 'auto'); assert.equal(env.get('history-mode').value, 'false'); assert.equal(env.get('image-mode-note').hidden, false);
  await env.send(); assert.equal(env.chats().at(-1).model, 'auto'); assert.equal(env.chats().at(-1).quota_feature, 'image_gen'); assert.equal(env.chats().at(-1).history_disabled, false);
  await env.click('mode-text'); assert.equal(env.get('model').value, 'gpt-custom-chat');
  await env.click('fill-image-example'); assert.equal(env.get('model').value, 'auto');
  await env.click('fill-text-example'); assert.equal(env.get('model').value, 'gpt-custom-chat');
  const before = env.chats().length;
  for (const model of ['gpt-image-2', 'chatgpt-image-latest']) {
    env.get('model').value = model; await env.send();
    assert.equal(env.chats().length, before); assert.equal(env.get('page-error').hidden, false);
    assert(env.get('page-error').textContent.includes('/v1/images/generations'));
    assert.equal(env.get('model').attrs['aria-invalid'], 'true');
  }
  assert(env.chats().every((body) => !['gpt-image-2', 'chatgpt-image-latest'].includes(body.model)));
  const directImage = boot('?mode=image'); await directImage.flush();
  assert.equal(directImage.get('model').value, 'auto'); assert.equal(directImage.get('history-mode').value, 'false');
  await directImage.click('mode-text'); assert.equal(directImage.get('model').value, 'gpt-6-luna');
  console.log('PASS playground models: Luna default, auto/manual chat, web-image auto + ordinary history, restore previous chat model, reject Images-only models before fetch, image deep link.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
