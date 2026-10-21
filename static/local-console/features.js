(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const labels = { implemented: '源码支持', conditional: '取决于上游', enabled: '已启用', disabled: '未启用', not_implemented: '无独立路由' };
  let loading = false;
  let currentSection = 'capabilities';
  const sections = ['capabilities', 'gateway', 'configuration', 'examples'];
  const anchors = {'#capabilities-title': 'capabilities', '#gateway-title': 'gateway', '#configuration-title': 'configuration', '#api-examples': 'examples'};
  function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
  function link(label, href, className = '') {
    const node = el('a', className, label); node.href = href;
    if (anchors[href]) node.addEventListener('click', () => selectSection(anchors[href]));
    return node;
  }
  function badge(state) { const node = el('span', 'feature-state', labels[state] || '状态未知'); node.dataset.state = state; return node; }
  function icon(name) {
    const paths = {
      accounts: 'M5 20v-7m7 7V4m7 16V9M3 20h18',
      tokens: 'M12 16V3m-4 4 4-4 4 4M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5',
      keys: 'M14 11a5 5 0 1 0-1-5M11 13l-8 8v-4l3-3h3l2-2',
      chat: 'M7 8h10M7 12h6M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z',
      multimodal: 'M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4M15 8h.01',
      images: 'M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4M15 8h.01',
      context: 'M4 7h12M12 3l4 4-4 4M20 17H8m4-4-4 4 4 4',
      'gpts-model': 'M12 3v3M9 3h6M5 6h14v14H5zM9 11h.01M15 11h.01M9 16h6M2 10v5m20-5v5',
      tools: 'm14 4 6 6-4 4-6-6zM10 8l-7 7v5h5l7-7',
    };
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ui-icon'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', paths[name] || paths.chat); svg.append(path); return svg;
  }
  function selectSection(name, focus = false) {
    if (!sections.includes(name)) return;
    currentSection = name;
    for (const section of sections) {
      const selected = section === name;
      const tab = byId(`features-tab-${section}`);
      tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
      byId(`features-panel-${section}`).hidden = !selected;
    }
    if (focus) byId(`features-tab-${name}`).focus();
  }
  function selectAnchor() { if (anchors[window.location.hash]) selectSection(anchors[window.location.hash]); }
  function replace(id, nodes) { byId(id).replaceChildren(...nodes); }
  function exampleText(example) {
    const lines = [`curl -X ${example.method} 'http://127.0.0.1:5005${example.path}'`];
    if (example.auth) lines.push(`  -H 'Authorization: Bearer ${example.auth}'`);
    if (example.encoding === 'form') {
      Object.entries(example.body).forEach(([name, value]) => lines.push(`  --data-urlencode '${name}=${value}'`));
    } else if (example.body !== undefined) {
      lines.push("  -H 'Content-Type: application/json'", `  -d '${JSON.stringify(example.body, null, 2)}'`);
    }
    return lines.join(' \\\n');
  }
  function render(data) {
    const openExamples = new Set(Array.from(byId('feature-examples').querySelectorAll('details[open]')).map((item) => item.dataset.exampleTitle));
    const openConditions = new Set(Array.from(byId('feature-capabilities').querySelectorAll('details[open]')).map((item) => item.dataset.featureId));
    replace('management-links', data.management.map((item) => {
      const node = link('', item.href, 'management-link');
      const symbol = el('span', 'management-link-icon'); symbol.append(icon(item.href.includes('accounts') ? 'accounts' : item.href.includes('tokens') ? 'tokens' : 'keys'));
      const content = el('span', 'management-link-content'); content.append(el('strong', '', item.title), el('span', 'management-link-description', item.description));
      node.append(symbol, content); return node;
    }));
    replace('feature-capabilities', data.capabilities.map((item) => {
      const card = el('article', 'feature-card'); const head = el('div', 'feature-card-heading');
      const title = el('div', 'feature-card-title'); title.append(icon(item.id), el('h3', '', item.title));
      head.append(title, badge(item.state));
      const footer = el('div', 'feature-card-footer'); const condition = el('details', 'feature-condition');
      condition.dataset.featureId = item.id; condition.open = openConditions.has(item.id);
      condition.append(el('summary', '', '使用条件'), el('p', 'field-hint', item.condition));
      footer.append(condition, link(`${item.action} ↗`, item.href, 'feature-action'));
      card.append(head, el('p', 'feature-card-description', item.description), footer);
      return card;
    }));
    const enabled = data.gateway_enabled === true;
    byId('gateway-state').textContent = enabled ? 'ENABLE_GATEWAY=true' : 'ENABLE_GATEWAY=false';
    byId('gateway-state').dataset.state = enabled ? 'enabled' : 'disabled';
    byId('gateway-help').textContent = enabled ? '网关路由已启用。打开原版网页入口后按原流程登录；这些入口不属于临时公网 API 通道。' : '当前关闭原版网页网关。要使用以下继承功能，在本机 .env 设置 ENABLE_GATEWAY=true 后重启；账号管理、聊天 API 和测试台不依赖该开关。';
    replace('gateway-features', data.inherited.map((item) => {
      const row = el('article', 'gateway-feature'); const content = el('div');
      content.append(el('h3', '', item.title), el('code', 'feature-route', item.path), el('p', 'field-hint', item.description));
      const actions = el('div', 'gateway-feature-actions'); actions.append(badge(item.state));
      if (item.href) actions.append(link(item.href.startsWith('#') ? '查看示例' : '打开入口 ↗', item.href, 'feature-action'));
      row.append(content, actions); return row;
    }));
    byId('configuration-note').textContent = data.configuration_note;
    replace('feature-configuration', data.configuration.map((item) => {
      const row = el('div'); const label = el('dt'); label.append(el('code', '', item.name), el('span', '', item.description));
      const value = el('dd'); value.append(badge(item.enabled ? 'enabled' : 'disabled')); row.append(label, value); return row;
    }));
    replace('feature-boundaries', data.boundaries.map((text) => el('li', '', text)));
    replace('feature-examples', data.examples.map((example) => {
      const block = el('details', 'feature-example'); block.dataset.exampleTitle = example.title; block.open = openExamples.has(example.title);
      const summary = el('summary'); summary.append(el('span', '', example.title), el('code', '', `${example.method} ${example.path}`));
      const body = el('div', 'feature-example-body'); const command = exampleText(example); const raw = el('pre', '', command); raw.tabIndex = 0;
      const copy = el('button', 'button button-secondary', '复制示例'); copy.type = 'button';
      copy.addEventListener('click', () => { void window.ConsoleUI.copyText(command, copy); });
      body.append(el('p', 'field-hint', example.note), raw, copy); block.append(summary, body); return block;
    }));
    byId('features-content').hidden = false;
    selectSection(currentSection);
  }
  async function load() {
    if (loading) return;
    loading = true; byId('features-reload').disabled = true; byId('features-reload').setAttribute('aria-busy', 'true'); byId('features-error').hidden = true;
    const controller = new AbortController(); const timer = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/local-console/features', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      if (response.status === 401) { window.location.assign('/login'); return; }
      if (!response.ok) throw new Error(`读取失败（HTTP ${response.status}）`);
      const data = await response.json();
      if (!data || !['management', 'capabilities', 'inherited', 'configuration', 'examples', 'boundaries'].every((name) => Array.isArray(data[name]))) throw new Error('服务返回了无法识别的功能数据');
      render(data);
    } catch (error) {
      byId('features-error').textContent = error.name === 'AbortError' ? '读取超时，请点击「更新状态」重试。' : `${error.message}。请确认本地服务正在运行后重试。`;
      byId('features-error').hidden = false;
    } finally { window.clearTimeout(timer); loading = false; byId('features-loading').hidden = true; byId('features-reload').disabled = false; byId('features-reload').setAttribute('aria-busy', 'false'); }
  }
  byId('features-reload').addEventListener('click', () => { void load(); });
  for (const name of sections) {
    byId(`features-tab-${name}`).addEventListener('click', () => selectSection(name));
    byId(`features-tab-${name}`).addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? sections.length - 1 : (sections.indexOf(currentSection) + (event.key === 'ArrowRight' ? 1 : sections.length - 1)) % sections.length;
      selectSection(sections[next], true);
    });
  }
  window.addEventListener('hashchange', selectAnchor);
  selectAnchor();
  byId('features-logout').addEventListener('click', async () => {
    byId('features-logout').disabled = true;
    try {
      const response = await fetch('/local-console/logout', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!response.ok && response.status !== 401) throw new Error('退出失败，请重试。');
      window.location.assign('/login');
    } catch (error) { byId('features-error').textContent = error.message; byId('features-error').hidden = false; byId('features-logout').disabled = false; }
  });
  void load();
})();
