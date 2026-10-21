(() => {
  'use strict';

  const byId = (id) => document.getElementById(id);
  const controls = Object.fromEntries(['refresh', 'copy-base', 'copy-public', 'reveal-key', 'copy-key', 'copy-model', 'copy-example', 'retry', 'logout'].map((name) => [name, byId(`connection-${name}`)]));
  let state = {loading: false, keyPending: false, expired: false, data: null, example: 'cloud', kind: 'chat', imageModel: 'chatgpt-image-latest', revealed: false, retryAction: null, section: 'overview', keys: [], keysLoaded: false, keysLoading: false, keysBusy: false, systemKey: null, revokeTarget: null};
  let revealTimer;

  function hideKey() {
    clearTimeout(revealTimer);
    byId('connection-api-key').value = state.data?.key_configured === false ? '未配置共享 API Key' : '••••••••••••••••••••••••';
    controls['reveal-key'].textContent = '显示';
    controls['reveal-key'].setAttribute('aria-pressed', 'false');
    state = {...state, revealed: false};
    byId('connection-key-status').textContent = '';
  }

  function updateControls() {
    const unavailable = state.loading || state.expired || !state.data;
    controls.refresh.disabled = state.loading || state.keyPending || state.expired;
    controls.refresh.setAttribute('aria-busy', String(state.loading));
    controls['copy-base'].disabled = unavailable;
    controls['copy-public'].disabled = unavailable || !publicBase();
    controls['copy-model'].disabled = unavailable;
    controls['copy-example'].disabled = unavailable;
    byId('connection-model-search').disabled = unavailable;
    byId('connection-catalog-title').closest('section').setAttribute('aria-busy', String(state.loading));
    for (const button of byId('connection-model-rows').querySelectorAll('button')) button.disabled = unavailable;
    for (const name of ['reveal-key', 'copy-key']) controls[name].disabled = unavailable || state.keyPending || !state.data?.key_configured;
    controls.retry.disabled = state.loading || state.keyPending || state.expired;
    byId('connection-parameters').setAttribute('aria-busy', String(state.loading));
    updateKeyControls();
  }

  function showError(message, retryAction) {
    state = {...state, retryAction};
    byId('connection-error-text').textContent = message;
    byId('connection-error').hidden = false;
    controls.retry.hidden = !retryAction;
  }

  function clearError() {
    state = {...state, retryAction: null};
    byId('connection-error').hidden = true;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {...options, credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 401) {
        state = {...state, expired: true};
        hideKey();
        closeCreatedKey();
        byId('connection-revoke-dialog').close();
        byId('connection-expired').hidden = false;
        byId('connection-error').hidden = true;
        byId('connection-status').dataset.state = 'error';
        byId('connection-status-text').textContent = '请重新登录';
        updateControls();
      }
      if (!response.ok) {
        const error = new Error('Request failed');
        error.status = response.status;
        throw error;
      }
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function publicBase() {
    return state.data?.public_status === 'running' && typeof state.data.public_base_url === 'string' && state.data.public_base_url.startsWith('https://') ? state.data.public_base_url : null;
  }

  function exampleText() {
    const local = state.example === 'local';
    const base = local ? state.data?.base_url : publicBase();
    const quotedBase = "'" + (base || '').replaceAll("'", "'\\''") + "'";
    const prefix = local ? '# 仅在运行 Chat2API 的这台 Mac 上执行\nexport CHAT2API_BASE_URL=' + quotedBase + '\n' : base ? '# 临时公网入口：Mac、本地服务与隧道须持续在线\nexport CHAT2API_BASE_URL=' + quotedBase + '\n' : '# 先设置 CHAT2API_BASE_URL 为已部署的 HTTPS 公网 Base URL\n# 地址以 /v1 结尾；当前本机地址不能从云服务器访问\n';
    const auth = '# 将 CHAT2API_API_KEY 设置为有效的应用密钥或系统密钥\n';
    const header = '  -H "Authorization: Bearer $' + '{CHAT2API_API_KEY}"';
    const root = '"$' + '{CHAT2API_BASE_URL}/';
    if (state.kind === 'models') return prefix + auth + ['curl ' + root + 'models"', header].join(' \\\n');
    const images = state.kind === 'images';
    const streaming = !images && (local || state.data?.public_streaming === true);
    const endpoint = images ? 'images/generations' : 'chat/completions';
    const body = images ? {model: state.imageModel, prompt: '生成一张简洁的蓝色圆形图案，白色背景。', n: 1, size: 'auto', quality: 'auto', output_format: 'png', response_format: 'b64_json'} : {model: 'auto', messages: [{role: 'user', content: '请只回复 OK。'}], stream: streaming};
    return prefix + auth + ['curl ' + (streaming ? '-N ' : '') + root + endpoint + '"', header, '  -H "Content-Type: application/json"', "  -d '" + JSON.stringify(body, null, 2) + "'"].join(' \\\n');
  }

  function renderExample() {
    byId('connection-curl').textContent = exampleText();
    byId('connection-example-panel').setAttribute('aria-labelledby', 'connection-kind-' + state.kind + ' connection-' + state.example + '-tab');
    const endpoints = {models: 'GET /v1/models', chat: 'POST /v1/chat/completions', images: 'POST /v1/images/generations'};
    byId('connection-example-endpoint').textContent = endpoints[state.kind] + ' · Bearer 认证';
    const local = state.example === 'local';
    const streamHint = state.data?.public_streaming === true ? '流式响应（stream: true）' : '非流式响应（stream: false）';
    byId('connection-example-hint').textContent = local ? (state.kind === 'chat' ? '本机示例使用流式响应，仅适用于运行服务的这台 Mac。' : '仅适用于运行服务的这台 Mac；接口返回完整 JSON。') : publicBase() ? (state.kind === 'chat' ? '公网聊天使用' + streamHint + '。' : '通过当前 HTTPS 公网入口调用，返回完整 JSON。') : '先设置 CHAT2API_BASE_URL 为已部署的 HTTPS 公网 Base URL。';
    const images = state.kind === 'images';
    byId('connection-image-options').hidden = !images;
    byId('connection-image-note').hidden = !images;
    byId('connection-image-note').textContent = state.imageModel === 'chatgpt-image-latest' ? '网页生图通过 ChatGPT 会话生成，实际可用性取决于账号；不保证底层原生 Image2 身份。固定 n=1、PNG，size / quality / background 仅 auto，仅返回 b64_json，请在调用方解码显示或保存。会话会启用历史。' : '原生 gpt-image-2 默认未启用，需要独立 Codex 授权，当前尚未验证成功。固定 n=1，仅返回 b64_json；支持 auto 或合规分辨率、auto / low / medium / high 质量及 PNG / JPEG / WebP。网页账号能聊天不代表能使用此模型。';
    for (const tab of document.querySelectorAll('[data-kind]')) {
      const selected = tab.dataset.kind === state.kind;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    for (const tab of document.querySelectorAll('[data-example]')) {
      const selected = tab.dataset.example === state.example;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
  }

  function renderCatalog() {
    const rows = state.data?.model_catalog?.data;
    const models = Array.isArray(rows) ? rows.filter((model) => model && typeof model.id === 'string' && model.id.length <= 100 && ['chat', 'image'].includes(model.type)) : [];
    const query = byId('connection-model-search').value.trim().toLowerCase();
    const visible = models.filter((model) => model.id.toLowerCase().includes(query));
    byId('connection-model-count').textContent = query ? visible.length + ' / ' + models.length : String(models.length);
    byId('connection-model-loading').hidden = true;
    byId('connection-model-empty').hidden = visible.length > 0;
    byId('connection-model-empty').textContent = models.length ? '没有匹配的模型，请调整搜索内容。' : '模型目录暂不可用，请刷新信息重试。';
    byId('connection-model-table-wrap').hidden = !visible.length;
    byId('connection-model-rows').replaceChildren(...visible.map((model) => {
      const row = node('tr');
      const identity = node('td', 'connection-model-identity'); identity.dataset.label = '模型';
      identity.append(node('code', '', model.id), node('p', 'field-hint', model.description || '可用性取决于账号。'));
      const source = node('td'); source.dataset.label = '类型与来源';
      source.append(node('span', 'connection-model-type', model.type === 'image' ? '图片' : '聊天'), node('p', 'field-hint', model.source === 'codex_images' ? 'Codex 图片权限' : 'ChatGPT 网页'));
      const endpoint = node('td', 'connection-model-endpoint'); endpoint.dataset.label = '接口';
      endpoint.append(node('code', '', model.type === 'image' ? '/v1/images/generations' : '/v1/chat/completions'));
      const action = node('td', 'connection-model-action');
      const copy = node('button', 'button button-quiet', '复制名称'); copy.type = 'button'; copy.setAttribute('aria-label', '复制模型 ' + model.id);
      copy.addEventListener('click', () => window.ConsoleUI.copyText(model.id, copy));
      action.append(copy); row.append(identity, source, endpoint, action); return row;
    }));
  }

  async function loadConnection() {
    if (state.loading || state.expired) return;
    hideKey();
    clearError();
    state = {...state, loading: true};
    updateControls();
    try {
      const data = await request('/local-console/connection');
      if (typeof data.base_url !== 'string' || !/^https?:\/\//.test(data.base_url) || typeof data.key_configured !== 'boolean') throw new Error('Invalid connection data');
      state = {...state, data};
      byId('connection-base-url').value = data.base_url;
      byId('connection-model').value = data.default_model || 'auto';
      byId('connection-status').dataset.state = data.key_configured ? 'ready' : 'error';
      byId('connection-status-text').textContent = data.key_configured ? '仅本机可访问' : '未配置共享密钥';
      byId('connection-key-hint').textContent = data.key_configured ? `系统密钥由 .env 中的 AUTHORIZATION 管理。${data.key_count > 1 ? '此处显示第一组系统密钥。' : '应用专用密钥请在「密钥管理」中创建。'}` : '请在服务环境变量 AUTHORIZATION 中配置系统密钥，重启服务后刷新。';
      const publicUrl = publicBase();
      byId('connection-public-url').value = publicUrl || '尚未启动公网入口';
      byId('connection-public-status').textContent = publicUrl ? '临时入口运行中' : '尚未开放公网';
      byId('connection-public-status').dataset.state = publicUrl ? 'running' : 'stopped';
      byId('connection-public-intro').textContent = publicUrl ? '使用下方 HTTPS 地址连接当前账号池。' : '当前服务仅在这台 Mac 上可访问。';
      byId('connection-public-description').textContent = publicUrl ? '该地址通过临时隧道连接本机 API，云服务器可以使用这里创建的应用密钥。' : '云服务器上的 127.0.0.1 指向云服务器自身，不能直接连接这台 Mac。';
      byId('connection-public-hint').textContent = publicUrl ? '转发模型查询、聊天与图片生成接口，统一使用这里管理的 API Key。' : '需要先启动 HTTPS 公网入口，再刷新此页获取实际地址。';
      byId('connection-public-streaming').textContent = data.public_streaming === true ? '支持流式 stream: true' : '非流式 stream: false';
      hideKey();
      renderCatalog();
      renderExample();
    } catch (error) {
      if (!state.expired) {
        byId('connection-status').dataset.state = 'error';
        byId('connection-status-text').textContent = '连接信息读取失败';
        if (!state.data) byId('connection-model-loading').textContent = '目录读取失败，请点击上方「重试」。';
        showError(error.name === 'AbortError' ? '读取连接信息超时，请重试。' : '无法读取连接信息，请检查本地服务后重试。', loadConnection);
      }
    } finally {
      state = {...state, loading: false};
      updateControls();
    }
  }

  async function accessKey(action) {
    if (state.keyPending || state.expired || !state.data?.key_configured) return;
    if (action === 'reveal' && state.revealed) { hideKey(); return; }
    clearError();
    state = {...state, keyPending: true};
    const button = controls[action === 'copy' ? 'copy-key' : 'reveal-key'];
    button.setAttribute('aria-busy', 'true');
    updateControls();
    try {
      const data = await request('/local-console/connection/key', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (typeof data.api_key !== 'string' || !data.api_key) throw new Error('Missing API key');
      if (document.visibilityState === 'hidden') return;
      if (action === 'copy') {
        const copied = await window.ConsoleUI.copyText(data.api_key, button);
        if (!copied) showError('复制失败。可以点击「显示」，选中密钥后手动复制。', null);
      } else {
        byId('connection-api-key').value = data.api_key;
        state = {...state, revealed: true};
        button.textContent = '隐藏';
        button.setAttribute('aria-pressed', 'true');
        byId('connection-key-status').textContent = '60 秒后自动隐藏；离开页面时立即隐藏。';
        revealTimer = setTimeout(hideKey, 60000);
      }
    } catch (error) {
      if (!state.expired) {
        if (error.status === 503) {
          state = {...state, data: {...state.data, key_configured: false}};
          hideKey();
          byId('connection-status').dataset.state = 'error';
          byId('connection-status-text').textContent = '未配置共享密钥';
          showError('服务未配置共享 API Key。设置 AUTHORIZATION 并重启后，刷新连接信息。', loadConnection);
        } else showError('无法读取 API Key，请重试。', () => accessKey(action));
      }
    } finally {
      state = {...state, keyPending: false};
      button.setAttribute('aria-busy', 'false');
      updateControls();
    }
  }

  function switchSection(section, focus = false) {
    if (!['overview', 'keys', 'examples'].includes(section)) return;
    state = {...state, section};
    for (const tab of document.querySelectorAll('[data-section]')) {
      const selected = tab.dataset.section === section;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      byId(`connection-${tab.dataset.section}-panel`).hidden = !selected;
    }
    if (focus) byId(`connection-section-${section}`).focus();
    if (section !== 'overview') hideKey();
  }

  function selectAnchor() {
    const sections = {'#overview': 'overview', '#keys': 'keys', '#api-key-management': 'keys', '#examples': 'examples'};
    if (sections[window.location.hash]) switchSection(sections[window.location.hash]);
  }

  function updateKeyControls() {
    const busy = state.expired || state.keysLoading || state.keysBusy;
    byId('connection-create-key').disabled = busy || !state.keysLoaded || !state.systemKey?.configured;
    byId('connection-create-key').setAttribute('aria-busy', String(state.keysBusy === 'create'));
    byId('connection-key-name').disabled = busy;
    byId('connection-keys-refresh').disabled = busy;
    byId('connection-keys-retry').disabled = busy;
    byId('connection-keys-refresh').setAttribute('aria-busy', String(state.keysLoading));
    byId('connection-revoke-confirm').disabled = Boolean(state.keysBusy) || state.expired;
    byId('connection-revoke-cancel').disabled = Boolean(state.keysBusy);
    byId('connection-revoke-confirm').setAttribute('aria-busy', String(state.keysBusy === 'revoke'));
    for (const button of byId('connection-key-rows').querySelectorAll('button')) button.disabled = busy;
    byId('connection-key-list-title').closest('section').setAttribute('aria-busy', String(state.keysLoading));
  }

  function keyError(message, retry = false) {
    byId('connection-keys-error-text').textContent = message;
    byId('connection-keys-error').hidden = false;
    byId('connection-keys-retry').hidden = !retry;
  }

  function clearKeyError() {
    byId('connection-keys-error').hidden = true;
    byId('connection-keys-feedback').hidden = true;
  }

  function keyFeedback(message) {
    byId('connection-keys-feedback').textContent = message;
    byId('connection-keys-feedback').hidden = false;
    window.ConsoleUI?.toast(message, {tone: 'success'});
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function keyDate(value) {
    if (!value) return '尚未使用';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间未提供';
    return date.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false});
  }

  function validKey(key) {
    return key && typeof key.id === 'string' && typeof key.name === 'string' && ['active', 'disabled', 'revoked'].includes(key.status);
  }

  function renderKeys() {
    const focusKey = document.activeElement?.dataset?.keyFocus;
    const fragment = document.createDocumentFragment();
    const labels = {active: '可用', disabled: '已停用', revoked: '已撤销'};
    for (const key of state.keys) {
      const row = node('tr');
      const identity = node('td', 'connection-key-identity');
      identity.dataset.label = '名称与密钥';
      identity.append(node('strong', '', key.name));
      identity.append(node('code', 'connection-masked-key', `${key.prefix || 'sk-c2a-'}••••${key.last4 || ''}`));
      identity.append(node('span', 'field-hint', `创建于 ${keyDate(key.created_at)}`));
      const status = node('td'); status.dataset.label = '状态';
      const badge = node('span', 'connection-key-badge', labels[key.status]);
      badge.dataset.status = key.status; status.append(badge);
      const lastUsed = node('td', 'connection-key-last-used', keyDate(key.last_used_at)); lastUsed.dataset.label = '最近调用';
      const usage = node('td', 'connection-key-usage', Number.isFinite(key.request_count) ? key.request_count.toLocaleString('zh-CN') : '—'); usage.dataset.label = '调用次数';
      const actions = node('td', 'connection-key-actions'); actions.dataset.label = '操作';
      if (key.status !== 'revoked') {
        const toggle = node('button', 'button button-secondary', key.status === 'active' ? '停用' : '启用');
        toggle.type = 'button'; toggle.dataset.keyFocus = `toggle:${key.id}`;
        toggle.setAttribute('aria-label', `${key.status === 'active' ? '停用' : '启用'}密钥 ${key.name}`);
        toggle.addEventListener('click', () => mutateKey(key, key.status === 'active' ? 'disabled' : 'active'));
        const revoke = node('button', 'button button-quiet connection-revoke-button', '撤销');
        revoke.type = 'button'; revoke.dataset.keyFocus = `revoke:${key.id}`;
        revoke.setAttribute('aria-label', `撤销密钥 ${key.name}`);
        revoke.addEventListener('click', () => {
          state = {...state, revokeTarget: key};
          byId('connection-revoke-name').textContent = key.name;
          byId('connection-revoke-dialog').showModal();
        });
        actions.append(toggle, revoke);
      } else actions.append(node('span', 'field-hint', '不可恢复'));
      row.append(identity, status, lastUsed, usage, actions);
      fragment.append(row);
    }
    byId('connection-key-rows').replaceChildren(fragment);
    byId('connection-keys-table-wrap').hidden = !state.keys.length;
    byId('connection-keys-empty').hidden = !state.keysLoaded || Boolean(state.keys.length);
    byId('connection-keys-count').textContent = String(state.keys.filter((key) => key.status !== 'revoked').length);
    const active = state.keys.filter((key) => key.status === 'active').length;
    const disabled = state.keys.filter((key) => key.status === 'disabled').length;
    const revoked = state.keys.filter((key) => key.status === 'revoked').length;
    byId('connection-key-summary').textContent = `${active} 个可用 · ${disabled} 个停用 · ${revoked} 个已撤销`;
    updateKeyControls();
    if (focusKey) {
      const button = Array.from(byId('connection-key-rows').querySelectorAll('button')).find((item) => item.dataset.keyFocus === focusKey);
      if (button) button.focus({preventScroll: true});
    }
  }

  async function loadKeys({keepFeedback = false} = {}) {
    if (state.keysLoading || state.expired || state.keysBusy) return;
    state = {...state, keysLoading: true};
    if (!keepFeedback) clearKeyError();
    byId('connection-keys-loading').hidden = state.keysLoaded;
    updateKeyControls();
    try {
      const data = await request('/local-console/api-keys');
      if (!Array.isArray(data.keys) || !data.keys.every(validKey) || typeof data.system_key?.configured !== 'boolean') throw new Error('Invalid API key metadata');
      state = {...state, keys: data.keys, keysLoaded: true, systemKey: data.system_key};
      byId('connection-create-hint').textContent = data.system_key.configured ? '创建后仅显示一次完整密钥，请保存到应用的环境变量中。' : '请先在 .env 中配置 AUTHORIZATION 系统密钥并重启服务，再创建应用密钥。';
      renderKeys();
    } catch (_) {
      if (!state.expired) keyError('无法读取应用密钥。已有数据会保留，请重新加载。', true);
    } finally {
      state = {...state, keysLoading: false};
      byId('connection-keys-loading').hidden = true;
      updateKeyControls();
    }
  }

  async function createKey(event) {
    event.preventDefault();
    if (state.keysBusy || state.keysLoading || state.expired) return;
    const name = byId('connection-key-name').value.trim();
    if (!name || name.length > 80) { keyError('请输入 1–80 个字符的密钥名称。'); byId('connection-key-name').focus(); return; }
    clearKeyError();
    state = {...state, keysBusy: 'create'};
    updateKeyControls();
    let created = false;
    try {
      const data = await request('/local-console/api-keys', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
      if (typeof data.api_key !== 'string' || !data.api_key || !validKey(data.key)) throw new Error('Invalid created key');
      state = {...state, keys: [data.key, ...state.keys]};
      byId('connection-key-name').value = '';
      byId('connection-created-name').textContent = name;
      byId('connection-created-description').textContent = '完整密钥仅显示这一次。关闭、离开或刷新页面后，将无法再次查看。';
      byId('connection-created-value').value = data.api_key;
      byId('connection-created-copy-error').hidden = true;
      byId('connection-created-dialog').showModal();
      renderKeys();
      created = true;
    } catch (error) {
      if (!state.expired) keyError(error.status === 503 ? '系统密钥未配置。请设置 AUTHORIZATION 并重启服务后再创建。' : '创建结果未确认。请先刷新列表检查是否已创建；若密钥已经生成但未保存，请撤销后重新创建。', true);
    } finally {
      state = {...state, keysBusy: false};
      updateKeyControls();
      if (created) keyFeedback('应用密钥已创建，请在弹窗中复制并保存。');
    }
  }

  function closeCreatedKey() {
    byId('connection-created-value').value = '';
    byId('connection-created-dialog').close();
  }

  async function mutateKey(key, status) {
    if (state.keysBusy || state.keysLoading || state.expired) return;
    clearKeyError();
    const revoked = status === 'revoked';
    const originalFocus = document.activeElement?.dataset?.keyFocus;
    state = {...state, keysBusy: revoked ? 'revoke' : key.id};
    updateKeyControls();
    try {
      const options = revoked ? {method: 'DELETE'} : {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status})};
      const data = await request(`/local-console/api-keys/${encodeURIComponent(key.id)}`, options);
      if (!validKey(data.key)) throw new Error('Invalid key update');
      state = {...state, keys: state.keys.map((item) => item.id === key.id ? data.key : item), revokeTarget: null};
      if (revoked) byId('connection-revoke-dialog').close();
      renderKeys();
      keyFeedback(revoked ? '密钥已撤销，后续调用将被拒绝。' : status === 'active' ? '密钥已启用。' : '密钥已停用，可以随时重新启用。');
    } catch (_) {
      if (!state.expired) {
        if (revoked) byId('connection-revoke-dialog').close();
        keyError('操作结果未确认，请刷新列表确认当前状态后再试。', true);
      }
    } finally {
      state = {...state, keysBusy: false};
      updateKeyControls();
      if (revoked) byId('connection-keys-refresh').focus({preventScroll: true});
      else if (originalFocus) {
        const button = Array.from(byId('connection-key-rows').querySelectorAll('button')).find((item) => item.dataset.keyFocus === originalFocus);
        if (button) button.focus({preventScroll: true});
      }
    }
  }

  controls.refresh.addEventListener('click', () => { loadConnection(); loadKeys(); });
  controls.retry.addEventListener('click', () => state.retryAction?.());
  controls['reveal-key'].addEventListener('click', () => accessKey('reveal'));
  controls['copy-key'].addEventListener('click', () => accessKey('copy'));
  controls['copy-base'].addEventListener('click', () => window.ConsoleUI.copyText(state.data?.base_url || '', controls['copy-base']));
  controls['copy-public'].addEventListener('click', () => { if (publicBase()) window.ConsoleUI.copyText(publicBase(), controls['copy-public']); });
  controls['copy-model'].addEventListener('click', () => window.ConsoleUI.copyText(state.data?.default_model || 'auto', controls['copy-model']));
  controls['copy-example'].addEventListener('click', () => window.ConsoleUI.copyText(exampleText(), controls['copy-example']));
  byId('connection-open-keys').addEventListener('click', () => switchSection('keys', true));
  byId('connection-model-search').addEventListener('input', renderCatalog);
  byId('connection-image-model').addEventListener('change', (event) => {
    if (!['chatgpt-image-latest', 'gpt-image-2'].includes(event.target.value)) return;
    state = {...state, imageModel: event.target.value}; renderExample();
  });
  byId('connection-create-form').addEventListener('submit', createKey);
  byId('connection-keys-refresh').addEventListener('click', () => loadKeys());
  byId('connection-keys-retry').addEventListener('click', () => loadKeys());
  byId('connection-created-close').addEventListener('click', closeCreatedKey);
  byId('connection-created-copy').addEventListener('click', async () => {
    const copied = await window.ConsoleUI.copyText(byId('connection-created-value').value, byId('connection-created-copy'));
    byId('connection-created-copy-error').hidden = copied;
  });
  byId('connection-created-dialog').addEventListener('cancel', (event) => {
    event.preventDefault();
    byId('connection-created-description').textContent = '请先复制并保存密钥，再点击「已保存，关闭」。关闭后无法再次查看。';
    byId('connection-created-close').focus();
  });
  byId('connection-revoke-cancel').addEventListener('click', () => {
    state = {...state, revokeTarget: null};
    byId('connection-revoke-dialog').close();
  });
  byId('connection-revoke-dialog').addEventListener('cancel', (event) => {
    if (state.keysBusy) event.preventDefault();
    else state = {...state, revokeTarget: null};
  });
  byId('connection-revoke-confirm').addEventListener('click', () => { if (state.revokeTarget) mutateKey(state.revokeTarget, 'revoked'); });
  for (const tab of document.querySelectorAll('[data-section]')) {
    tab.addEventListener('click', () => switchSection(tab.dataset.section));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const sections = ['overview', 'keys', 'examples'];
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (sections.indexOf(state.section) + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
      switchSection(sections[next], true);
    });
  }
  for (const tab of document.querySelectorAll('[data-example]')) {
    tab.addEventListener('click', () => { state = {...state, example: tab.dataset.example}; renderExample(); });
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const example = event.key === 'Home' ? 'cloud' : event.key === 'End' ? 'local' : state.example === 'cloud' ? 'local' : 'cloud';
      state = {...state, example};
      renderExample();
      byId(`connection-${example}-tab`).focus();
    });
  }
  const kinds = ['models', 'chat', 'images'];
  for (const tab of document.querySelectorAll('[data-kind]')) {
    tab.addEventListener('click', () => { state = {...state, kind: tab.dataset.kind}; renderExample(); });
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? kinds.length - 1 : (kinds.indexOf(state.kind) + (event.key === 'ArrowRight' ? 1 : kinds.length - 1)) % kinds.length;
      state = {...state, kind: kinds[next]}; renderExample();
      byId(`connection-kind-${state.kind}`).focus();
    });
  }
  controls.logout.addEventListener('click', async () => {
    hideKey();
    controls.logout.disabled = true;
    try {
      await request('/local-console/logout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      window.location.assign('/login');
    } catch (_) {
      if (state.expired) window.location.assign('/login');
      else { showError('退出失败，请重试。', null); controls.logout.disabled = false; }
    }
  });
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') hideKey(); });
  window.addEventListener('beforeunload', (event) => {
    if (!byId('connection-created-value').value) return;
    event.preventDefault();
    event.returnValue = '';
  });
  window.addEventListener('pagehide', () => { hideKey(); closeCreatedKey(); });
  window.addEventListener('hashchange', selectAnchor);
  selectAnchor();
  loadConnection();
  loadKeys();
})();
