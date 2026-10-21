(() => {
  'use strict';
  const $ = (id) => document.getElementById('dashboard-' + id);
  const statusNames = {succeeded: '成功', failed: '失败', cancelled: '取消', interrupted: '中断', in_progress: '处理中'};
  const query = new URLSearchParams(window.location.search);
  let zone;
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (_) { zone = 'Asia/Shanghai'; }
  let state = {data: null, loading: false, expired: false, days: ['7', '30', '90'].includes(query.get('days')) ? query.get('days') : '30', trend: 'requests', timezone: query.get('timezone') || zone || 'Asia/Shanghai', keys: [], pricing: null, pricingLoading: false, pricingSaving: false, customRates: [], dirty: false};
  const number = (value) => Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
  const percent = (value) => Number.isFinite(value) ? (value * 100).toFixed(1) + '%' : '—';
  const amount = (value) => Number.isFinite(Number(value)) && value !== null && value !== '' ? '$' + Number(value).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 6}) : '未估值';
  const cost = (row) => row?.requests === 0 ? amount(0) : row?.known_cost_requests > 0 ? amount(row.estimated_cost_usd) : '未估值';
  const duration = (value) => Number.isFinite(value) ? (value / 1000).toLocaleString('zh-CN', {maximumFractionDigits: 2}) + ' 秒' : '未知';
  function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = String(text); return node; }
  function replace(id, ...nodes) { $(id).replaceChildren(...nodes); }
  function date(value, full = false) {
    if (!value) return '尚未采集';
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return '未知';
    try { return parsed.toLocaleString('zh-CN', {timeZone: state.data?.range?.timezone || state.timezone, year: full ? 'numeric' : undefined, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}); } catch (_) { return parsed.toISOString(); }
  }
  function keyName(id) {
    if (id === 'direct') return '直接账号凭据';
    const saved = state.keys.find((key) => key.id === id);
    if (saved) return saved.name;
    if (String(id).startsWith('system_')) return '系统共享密钥 · ' + String(id).slice(-6);
    return id ? '历史密钥 · ' + String(id).slice(-8) : '未归属';
  }
  function keyLink(id) {
    const link = el('a', 'dashboard-key-link', keyName(id)), filter = new URLSearchParams({api_key_id: id || '', timezone: state.data?.range?.timezone || state.timezone});
    if (state.data?.range) { filter.set('date_from', state.data.range.date_from); filter.set('date_to', state.data.range.date_to); }
    for (const name of ['model', 'kind']) if (state.data?.filters?.[name]) filter.set(name, state.data.filters[name]);
    link.href = '/dashboard?' + filter; return link;
  }
  function error(message) { $('error-text').textContent = message; $('error').hidden = false; }
  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {...options, credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 401) {
        state = {...state, expired: true};
        $('login').hidden = false;
        $('error').hidden = true;
        throw new Error('登录已过期。');
      }
      let payload;
      try { payload = await response.json(); } catch (_) { throw new Error('服务没有返回有效的数据。'); }
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '读取失败（HTTP ' + response.status + '），请检查筛选条件后重试。');
      return payload;
    } finally { window.clearTimeout(timer); }
  }
  function setBusy() {
    for (const input of $('filters').querySelectorAll('input, select, button')) input.disabled = state.loading || state.expired;
    for (const id of ['refresh', 'retry']) $(id).disabled = state.loading || state.expired;
    $('refresh').setAttribute('aria-busy', String(state.loading));
    $('content').setAttribute('aria-busy', String(state.loading));
    $('price-add').disabled = !state.pricing || state.pricingSaving || state.expired || state.customRates.length >= 100;
    $('price-save').disabled = !state.pricing || state.pricingSaving || state.expired || !state.dirty;
    $('price-save').setAttribute('aria-busy', String(state.pricingSaving));
    for (const control of $('pricing-editor').querySelectorAll('input, select, button')) {
      control.disabled = state.pricingSaving || state.expired || control.dataset.inapplicable === 'true';
    }
  }
  function setRange(days) {
    state = {...state, days};
    $('custom-dates').hidden = days !== 'custom';
    for (const button of document.querySelectorAll('[data-days]')) button.setAttribute('aria-pressed', String(button.dataset.days === days));
  }
  function params() {
    const result = new URLSearchParams({days: state.days === 'custom' ? '30' : state.days, timezone: state.timezone});
    if (state.days === 'custom') {
      if (!$('start').value || !$('end').value) throw new Error('请选择开始和结束日期。');
      if ($('start').value > $('end').value) throw new Error('结束日期不能早于开始日期。');
      const span = (Date.parse($('end').value) - Date.parse($('start').value)) / 86400000;
      if (span > 365) throw new Error('自定义范围最多 366 天，请缩小日期范围。');
      result.set('date_from', $('start').value); result.set('date_to', $('end').value);
    }
    for (const [field, parameter] of [['key', 'api_key_id'], ['model', 'model'], ['kind', 'kind']]) if ($(field).value) result.set(parameter, $(field).value);
    return result;
  }
  function options(id, choices, label, selected) {
    const select = $(id), current = selected ?? select.value;
    const values = [...new Set([...choices, current].filter(Boolean))];
    select.replaceChildren(el('option', '', label));
    select.firstElementChild.value = '';
    for (const value of values) { const item = el('option', '', id === 'key' ? keyName(value) : value); item.value = value; select.append(item); }
    select.value = current || '';
  }
  function metric(title, value, lines, ratio) {
    const box = el('article', 'dashboard-metric');
    box.append(el('h2', '', title), el('p', 'dashboard-metric-value', value));
    for (const line of lines) box.append(el('p', 'field-hint', line));
    if (Number.isFinite(ratio)) { const rail = el('div', 'dashboard-coverage'); rail.setAttribute('aria-label', '估价覆盖率 ' + percent(ratio)); const fill = el('span'); fill.style.width = Math.min(100, Math.max(0, ratio * 100)) + '%'; rail.append(fill); box.append(rail); }
    return box;
  }
  function table(headers, rows) {
    if (!rows.length) return el('p', 'dashboard-chart-empty', '所选范围暂无数据。');
    const grid = el('table', 'dashboard-table'), head = el('thead'), body = el('tbody'), labels = el('tr');
    for (const label of headers) { const cell = el('th', '', label); cell.scope = 'col'; labels.append(cell); }
    head.append(labels);
    for (const cells of rows) {
      const row = el('tr');
      for (const item of cells) { const cell = el('td'); if (item && typeof item === 'object' && item.nodeType) cell.append(item); else cell.textContent = String(item ?? '—'); row.append(cell); }
      body.append(row);
    }
    grid.append(head, body); return grid;
  }
  function detail(primary, secondary, cls = '') {
    const node = el('span', cls);
    if (primary && typeof primary === 'object' && primary.nodeType) node.append(primary); else node.append(el('span', '', primary));
    if (secondary) node.append(el('small', 'dashboard-cell-sub', secondary));
    return node;
  }
  function status(value) { const badge = el('span', 'dashboard-state', statusNames[value] || '未知'); badge.dataset.status = value; return badge; }
  function svgNode(tag, attributes = {}, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function chart(id, rows, values, label, tooltip, money = false) {
    if (!rows.length) { replace(id, el('p', 'dashboard-chart-empty', '暂无可绘制的数据。')); return; }
    const M = {width: Math.max(320, ($(id).clientWidth || 768) - 48), height: 230, left: 54, right: 12, top: 12, bottom: 32};
    const height = M.height - M.top - M.bottom, width = M.width - M.left - M.right;
    const valid = values.filter((value) => Number.isFinite(value));
    const peak = Math.max(...valid, 0) || 1;
    const maximum = money ? peak : Math.ceil(peak / 2) * 2;
    const root = svgNode('svg', {viewBox: '0 0 ' + M.width + ' ' + M.height, role: 'group', 'aria-label': label});
    root.append(svgNode('desc', {}, label + '。使用左右方向键浏览，或查看数据表。'));
    for (let index = 0; index < 3; index++) {
      const fraction = index / 2, y = M.top + height * (1 - fraction), value = maximum * fraction;
      root.append(svgNode('line', {x1: M.left, y1: y, x2: M.width - M.right, y2: y, class: 'dashboard-grid-line'}));
      root.append(svgNode('text', {x: M.left - 8, y: y + 4, 'text-anchor': 'end', class: 'dashboard-axis-label'}, money ? '$' + value.toLocaleString('en-US', {maximumFractionDigits: 4}) : value.toLocaleString('zh-CN')));
    }
    const groups = [], step = width / rows.length;
    rows.forEach((row, index) => {
      const value = values[index], x = M.left + step * index, barHeight = Number.isFinite(value) ? value / maximum * height : 0;
      const point = svgNode('g', {tabindex: index === 0 ? 0 : -1, role: 'button', class: 'dashboard-chart-point', 'aria-label': tooltip(row)});
      point.append(svgNode('title', {}, tooltip(row)), svgNode('rect', {x, y: M.top, width: step, height, class: 'dashboard-bar-target'}));
      if (Number.isFinite(value)) point.append(svgNode('rect', {x: x + step * .18, y: M.top + height - barHeight, width: Math.max(.5, step * .64), height: Math.max(0, barHeight), rx: Math.min(3, step * .1), class: 'dashboard-bar'}));
      else point.append(svgNode('line', {x1: x + step * .2, y1: M.top + height - 4, x2: x + step * .8, y2: M.top + height - 4, class: 'dashboard-unknown-mark'}));
      const show = () => { $(id === 'trend-chart' ? 'trend-tooltip' : 'hours-tooltip').textContent = tooltip(row); };
      point.addEventListener('pointerenter', show); point.addEventListener('focus', show); point.addEventListener('click', show);
      point.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)));
        groups.forEach((item, position) => item.setAttribute('tabindex', position === next ? 0 : -1)); groups[next].focus();
      });
      groups.push(point); root.append(point);
      if (index === 0 || index === rows.length - 1 || (rows.length > 4 && index % Math.ceil(rows.length / 5) === 0)) {
        const caption = row.date ? row.date.slice(5) : String(row.hour).padStart(2, '0') + ':00';
        root.append(svgNode('text', {x: x + step / 2, y: M.height - 8, 'text-anchor': 'middle', class: 'dashboard-axis-label'}, caption));
      }
    });
    replace(id, root);
  }
  function renderTrend() {
    const rows = state.data.daily, money = state.trend === 'cost';
    $('trend-title').textContent = money ? '每日 API 等价估值' : '每日请求趋势';
    $('trend-description').textContent = money ? '只累计已知金额；虚线代表有请求但无法估值，非实际账单。' : '按请求开始日期统计，重试账号不重复计算客户端请求。';
    for (const button of document.querySelectorAll('[data-trend]')) button.setAttribute('aria-pressed', String(button.dataset.trend === state.trend));
    chart('trend-chart', rows, rows.map((row) => money ? (row.requests === 0 ? 0 : row.known_cost_requests > 0 ? Number(row.estimated_cost_usd) : null) : row.requests), $('trend-title').textContent, (row) => row.date + ' · ' + number(row.requests) + ' 请求 · ' + cost(row) + ' · ' + number(row.unknown_cost_requests) + ' 次未估值', money);
    $('trend-tooltip').textContent = money ? '金额仅为已知部分。选择日期查看未估值请求数。' : '选择图上的日期查看数值；键盘可使用左右方向键。';
  }
  function render() {
    const data = state.data, total = data.total, coverage = data.coverage || {};
    const hasTokens = coverage.token_known_requests > 0;
    replace('metrics',
      metric('客户端请求', number(total.requests), ['成功率 ' + percent(total.success_rate) + ' · 成功 ' + number(total.succeeded), '平均完成耗时 ' + duration(total.avg_duration_ms)]),
      metric('API 等价估值 · USD', cost(total), ['已知部分，非实际账单', '覆盖 ' + percent(coverage.known_cost_ratio) + ' · ' + number(coverage.unknown_cost_requests) + ' 次未估值'], coverage.known_cost_ratio),
      metric('图片请求', number(total.image_requests), ['成功生成 ' + number(total.generated_images) + ' 张', '请求次数与完成图片数分别统计']),
      metric('已知 Token', hasTokens ? number((total.prompt_tokens || 0) + (total.completion_tokens || 0)) : '未知', ['输入 ' + (hasTokens ? number(total.prompt_tokens) : '未知') + ' · 输出 ' + (hasTokens ? number(total.completion_tokens) : '未知'), number(coverage.token_unknown_requests) + ' 次请求未提供用量']));
    $('recording-warning').hidden = data.recording_status !== 'incomplete';
    $('collection-note').textContent = '采集起点：' + date(data.since, true) + '。' + (data.legacy?.message || '历史账号记录未回填为客户端请求。');
    $('updated').textContent = '视图更新于 ' + date(new Date().toISOString());
    $('range-caption').textContent = data.range.date_from + ' — ' + data.range.date_to + ' · ' + data.range.timezone;
    $('hours-description').textContent = '所选范围内按 ' + data.range.timezone + ' 的小时合计';
    $('empty').hidden = total.requests !== 0;
    options('key', [...(data.options?.keys || data.keys.map((row) => row.key_id)), ...state.keys.map((key) => key.id)], '全部密钥');
    options('model', data.options?.models || data.models.map((row) => row.model), '全部模型');
    renderTrend();
    chart('hours-chart', data.hourly, data.hourly.map((row) => row.requests), '24 小时请求分布', (row) => String(row.hour).padStart(2, '0') + ':00 · ' + number(row.requests) + ' 请求');
    $('hours-tooltip').textContent = '选择小时查看请求次数；分布汇总整个所选日期范围。';
    const top = data.models.slice(0, 8), max = Math.max(...top.map((row) => row.requests), 1);
    replace('model-ranking', ...(top.length ? top.map((row) => {
      const block = el('div', 'dashboard-rank-row'), label = el('div', 'dashboard-rank-label'), track = el('div', 'dashboard-rank-track'), fill = el('div', 'dashboard-rank-fill');
      label.append(el('code', '', row.model), el('span', '', number(row.requests)));
      fill.style.width = Math.max(0, row.requests / max * 100) + '%'; track.append(fill); block.append(label, track); return block;
    }) : [el('p', 'dashboard-chart-empty', '暂无模型调用。')]));
    replace('statuses', ...Object.entries(statusNames).map(([name]) => {
      const row = data.statuses.find((item) => item.status === name), count = row?.requests || 0;
      const line = el('div', 'dashboard-status-row'); line.append(status(name), el('span', '', number(count)), el('span', 'field-hint', percent(total.requests ? count / total.requests : 0))); return line;
    }));
    replace('daily-table', table(['日期', '请求', '成功', '已知估值 USD', '未估值请求', '图片请求', '生成张数'], data.daily.map((row) => [row.date, number(row.requests), number(row.succeeded), cost(row), number(row.unknown_cost_requests), number(row.image_requests), number(row.generated_images)])));
    replace('key-table', table(['API 密钥', '请求', '成功率', '已知估值 USD', '图片请求 / 生成张数', 'Token', '平均耗时'], data.keys.map((row) => [
      keyLink(row.key_id), number(row.requests), percent(row.success_rate), detail(cost(row), number(row.unknown_cost_requests) + ' 次未估值'),
      number(row.image_requests) + ' 次 / ' + number(row.generated_images) + ' 张',
      detail(row.requests > row.token_unknown_requests ? number((row.prompt_tokens || 0) + (row.completion_tokens || 0)) : '未知', number(row.token_unknown_requests) + ' 次用量未知'), duration(row.avg_duration_ms)])));
    replace('request-table', table(['开始时间 / 耗时', 'API 密钥', '请求模型 / 类型', '状态', 'Token 输入 / 输出', '生成张数', 'API 等价估值'], data.recent.map((row) => [
      detail(date(row.started_at), duration(row.duration_ms)), keyLink(row.key_id), detail(el('code', '', row.requested_model || '未知'), row.kind === 'image' ? '图片生成' : '聊天'),
      detail(status(row.status), row.http_status ? 'HTTP ' + row.http_status : ''), row.prompt_tokens === null || row.completion_tokens === null ? '未知' : number(row.prompt_tokens) + ' / ' + number(row.completion_tokens),
      number(row.generated_images), detail(amount(row.estimated_cost_usd), row.price_source === 'custom' ? '自定义价格' : row.price_source === 'official' ? '官方 API 价格参考' : row.pricing_status === 'known' ? '已估值' : '价格或用量缺失')])));
    $('request-count').textContent = '最近 ' + number(data.recent.length) + ' / 共 ' + number(total.requests) + ' 次';
    $('content').hidden = false;
  }
  async function load() {
    if (state.loading || state.expired) return;
    let filter;
    try { filter = params(); } catch (failure) { error(failure.message); return; }
    state = {...state, loading: true}; $('error').hidden = true; $('loading').hidden = Boolean(state.data); setBusy();
    try {
      const data = await request('/local-console/dashboard?' + filter);
      if (!data?.total || !data.range || !['daily', 'hourly', 'models', 'keys', 'statuses', 'recent'].every((key) => Array.isArray(data[key]))) throw new Error('服务返回了无法识别的统计数据。');
      state = {...state, data}; render();
      window.history.replaceState(null, '', '/dashboard?' + filter);
    } catch (failure) {
      if (!state.expired) error((failure.name === 'AbortError' ? '读取统计超时，请重试。' : failure.message) + (state.data ? ' 下方保留上次成功读取的数据。' : ''));
    } finally { state = {...state, loading: false}; $('loading').hidden = true; setBusy(); }
  }
  const priceFields = ['input_per_million_usd', 'cached_input_per_million_usd', 'output_per_million_usd', 'image_per_unit_usd'];
  const priceLabels = ['输入 / 百万 Token', '缓存输入 / 百万', '输出 / 百万 Token', '每张图片 / USD'];
  function dirty() { state = {...state, dirty: true}; $('price-feedback').textContent = '有未保存的修改'; setBusy(); }
  function rateEditor() {
    const scroll = el('div', 'dashboard-price-scroll'), editor = el('div');
    state.customRates.forEach((rate, index) => {
      const row = el('div', 'dashboard-price-row');
      function field(name, title, control) {
        const wrap = el('div', 'field'), label = el('label', '', title), id = 'dashboard-rate-' + index + '-' + name;
        label.htmlFor = id; control.id = id; control.value = rate[name] ?? '';
        control.addEventListener('input', () => { state = {...state, customRates: state.customRates.map((item, position) => position === index ? {...item, [name]: control.value || null} : item)}; dirty(); });
        wrap.append(label, control); row.append(wrap); return control;
      }
      const model = field('model', '模型标识', el('input')); model.maxLength = 100; model.placeholder = '与请求模型完全一致';
      const kind = el('select'); for (const [value, title] of [['text', '文本'], ['image', '图片']]) { const option = el('option', '', title); option.value = value; kind.append(option); }
      field('kind', '价格类型', kind);
      kind.addEventListener('change', () => { state = {...state, customRates: state.customRates.map((item, position) => position === index ? {...item, kind: kind.value} : item)}; dirty(); rateEditor(); });
      priceFields.forEach((name, position) => {
        const input = field(name, priceLabels[position], el('input')); input.inputMode = 'decimal'; input.placeholder = '未设置'; input.maxLength = 32;
        input.dataset.inapplicable = String(rate.kind === 'image' ? position !== 3 : position === 3);
        input.disabled = input.dataset.inapplicable === 'true';
      });
      const remove = el('button', 'button button-quiet dashboard-price-remove', '移除'); remove.type = 'button'; remove.setAttribute('aria-label', '移除第 ' + (index + 1) + ' 项自定义价格');
      remove.addEventListener('click', () => { state = {...state, customRates: state.customRates.filter((_, position) => position !== index)}; dirty(); rateEditor(); $('price-add').focus(); });
      row.append(remove); editor.append(row);
    });
    if (!state.customRates.length) editor.append(el('p', 'field-hint', '没有自定义价格，使用下方参考价格。未包含的模型保留未估值。'));
    scroll.append(editor);
    const reference = el('details', 'dashboard-price-reference'), summary = el('summary', '', '查看当前有效价格 · USD'), grid = el('div', 'dashboard-table-scroll');
    grid.append(table(['模型', '类型 / 来源', '输入 / 百万', '缓存输入 / 百万', '输出 / 百万', '每张图片'], (state.pricing?.rates || []).map((rate) => [el('code', '', rate.model), detail(rate.kind === 'image' ? '图片' : '文本', (rate.source === 'custom' ? '自定义' : '官方参考') + (rate.as_of ? ' · ' + rate.as_of : '')), ...priceFields.map((name) => rate[name] === null || rate[name] === undefined ? '—' : String(rate[name]))])));
    reference.append(summary, grid);
    replace('pricing-editor', scroll, reference);
    setBusy();
  }
  async function loadPrices() {
    if (state.pricingLoading || state.pricing || state.expired) return;
    state = {...state, pricingLoading: true}; $('pricing-loading').textContent = '正在读取价格…'; $('pricing-error').hidden = true;
    try {
      const data = await request('/local-console/dashboard/pricing');
      if (!Array.isArray(data.rates) || !Array.isArray(data.custom_rates)) throw new Error('价格配置格式不正确。');
      state = {...state, pricing: data, customRates: data.custom_rates.map((rate) => ({...rate}))};
      rateEditor(); $('price-feedback').textContent = '保存只影响之后开始的新请求，历史记录不重新估值。';
    } catch (failure) { if (!state.expired) { $('pricing-error').textContent = failure.message + ' 收起并重新展开可重试。'; $('pricing-error').hidden = false; } }
    finally { state = {...state, pricingLoading: false}; $('pricing-loading').hidden = true; setBusy(); }
  }
  async function savePrices() {
    if (state.pricingSaving || !state.dirty || state.expired) return;
    const rates = state.customRates.map((rate) => ({model: String(rate.model || '').trim(), kind: rate.kind, ...(typeof rate.label === 'string' ? {label: rate.label} : {}), ...Object.fromEntries(priceFields.map((name, index) => [name, (rate.kind === 'image' ? index !== 3 : index === 3) ? null : rate[name] === '' || rate[name] === undefined ? null : rate[name]]))}));
    if (rates.some((rate) => !rate.model)) { $('pricing-error').textContent = '请填写每项价格的模型标识。'; $('pricing-error').hidden = false; return; }
    state = {...state, pricingSaving: true}; $('pricing-error').hidden = true; setBusy();
    try {
      const data = await request('/local-console/dashboard/pricing', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({custom_rates: rates})});
      if (!Array.isArray(data.rates) || !Array.isArray(data.custom_rates)) throw new Error('保存结果无法确认，请刷新后检查配置。');
      state = {...state, pricing: data, customRates: data.custom_rates.map((rate) => ({...rate})), dirty: false};
      rateEditor(); $('price-feedback').textContent = '已保存。仅之后开始的新请求使用新价格。'; window.ConsoleUI?.toast('估价设置已保存', {tone: 'success'});
    } catch (failure) { if (!state.expired) { $('pricing-error').textContent = failure.message; $('pricing-error').hidden = false; } }
    finally { state = {...state, pricingSaving: false}; setBusy(); }
  }
  options('key', [query.get('api_key_id')], '全部密钥', query.get('api_key_id'));
  options('model', [query.get('model')], '全部模型', query.get('model'));
  $('kind').value = ['chat', 'image'].includes(query.get('kind')) ? query.get('kind') : '';
  if (query.get('date_from') && query.get('date_to')) { $('start').value = query.get('date_from'); $('end').value = query.get('date_to'); setRange('custom'); } else setRange(state.days);
  $('filters').addEventListener('submit', (event) => { event.preventDefault(); void load(); });
  for (const button of document.querySelectorAll('[data-days]')) button.addEventListener('click', () => { setRange(button.dataset.days); if (state.days !== 'custom') void load(); else $('start').focus(); });
  for (const button of document.querySelectorAll('[data-trend]')) button.addEventListener('click', () => { state = {...state, trend: button.dataset.trend}; if (state.data) renderTrend(); });
  $('refresh').addEventListener('click', () => { void load(); }); $('retry').addEventListener('click', () => { void load(); });
  $('reset').addEventListener('click', () => { $('key').value = ''; $('model').value = ''; $('kind').value = ''; setRange('30'); void load(); });
  $('pricing').addEventListener('toggle', () => { if ($('pricing').open) void loadPrices(); });
  $('price-add').addEventListener('click', () => {
    if (state.customRates.length >= 100) return;
    state = {...state, customRates: [...state.customRates, {model: '', kind: 'text', ...Object.fromEntries(priceFields.map((name) => [name, null]))}]}; dirty(); rateEditor();
    document.getElementById('dashboard-rate-' + (state.customRates.length - 1) + '-model').focus();
  });
  $('price-save').addEventListener('click', () => { void savePrices(); });
  window.addEventListener('beforeunload', (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
  let resizeFrame;
  window.addEventListener('resize', () => {
    if (!state.data || resizeFrame) return;
    resizeFrame = window.requestAnimationFrame(() => { resizeFrame = null; render(); });
  });
  $('logout').addEventListener('click', async () => {
    $('logout').disabled = true;
    try { await request('/local-console/logout', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); window.location.assign('/login'); }
    catch (failure) { if (state.expired) window.location.assign('/login'); else { error('退出失败，请重试。'); $('logout').disabled = false; } }
  });
  void request('/local-console/api-keys').then((data) => {
    state = {...state, keys: Array.isArray(data.keys) ? data.keys : []};
    if (state.data) render();
  }).catch(() => { /* Usage remains readable with anonymous key identifiers. */ });
  void load();
})();
