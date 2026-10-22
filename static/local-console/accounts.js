(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const ui = Object.fromEntries(['accounts-error', 'session-expired', 'accounts-feedback', 'accounts-loading', 'accounts-empty', 'accounts-table-wrap', 'accounts-rows', 'account-search', 'account-status-filter', 'refresh-all', 'reload-accounts', 'accounts-logout'].map((id) => [id, byId(id)]));
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });
  const dateFormat = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const shortDateFormat = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  const statusLabels = { fresh: '最新快照', stale: '待刷新', unknown: '尚未查询', expired: '凭据过期', error: '查询失败' };
  const credentialLabels = { valid: '声明有效期内', expired: '已过期', unknown: '有效期未知', error: '凭据异常' };
  const featureNames = { reason: '大模型', image_gen: '生图', deep_research: '深度研究', file_upload: '文件上传', paste_text_to_file: '长文本转文件' };
  const schedulerStateLabels = {
    ready: '可选', available: '可选', active: '执行中', unknown: '额度待确认', stale: '额度待刷新',
    exhausted: '额度已用尽', cooldown: '暂时冷却', reset_pending: '等待重置核验', rechecking: '正在核验',
    expired: '凭据过期', auth_error: '凭据需更新', disabled: '暂停使用', concurrency_full: '并发已满', draining: '等待现有请求结束', reserved: '额度已预留', unavailable: '暂不可选',
  };
  const schedulerReasonLabels = {
    quota_available: '上游额度可用', available: '上游额度可用', selected: '按当前额度选中', quota_priority: '优先选择剩余额度较多的账号',
    quota_exhausted: '已达到该能力的额度限制', exhausted: '已达到该能力的额度限制', upstream_exhausted: '上游返回额度已耗尽',
    upstream_limit: '上游返回使用限制', quota_depleted: '调度估算额度已分配完', local_budget_exhausted: '调度估算额度已分配完',
    cooldown: '等待冷却时间结束', rate_limited: '上游暂时限流', upstream_rate_limit: '上游暂时限流',
    reset_pending: '已到重置时间，等待核验额度', reset_due: '已到重置时间，等待核验额度', reset_verified: '已核验重置后的额度',
    quota_recovered: '重新查询后确认额度可用', recovered: '重新查询后确认额度可用', restored: '重新查询后确认额度可用',
    unknown_quota: '上游未提供额度', quota_unknown: '上游未提供额度', stale_quota: '等待更新上游额度',
    credential_expired: '账号凭据已过期', auth_error: '上游认证失败', authentication_failed: '上游认证失败',
    upstream_error: '上游请求失败，暂时避开该账号', request_failed: '请求失败，暂时避开该账号',
    reservations: '额度已为当前请求预留', reserved: '额度已为当前请求预留', in_flight: '当前请求正在处理',
    retry: '为重试选择其他账号', fallback: '选择其他可用账号', initial: '首次选择账号',
    quota_empty: '上游额度已用尽', upstream_quota: '上游返回额度已耗尽', quota_refreshed: '重新查询后确认额度可用',
    rate_limit: '上游暂时限流', transient: '请求暂时失败，等待冷却后再试', auth: '上游认证失败',
    unknown_fallback: '没有已确认额度可用的账号时，才单路尝试该账号', restart: '服务重启，未完成请求先保留估算扣减',
    credentials: '账号凭据已过期或异常', cooldown_elapsed: '冷却结束，允许再次尝试',
    concurrency_limit: '已达到账号并发上限，等待现有请求结束', concurrency_full: '已达到账号并发上限',
    awaiting_quota_refresh: '估算可分配额度已用完，等待重新查询',
  };
  const schedulerEventLabels = {
    selected: '选中账号', switched: '切换账号', switch: '切换账号', exhausted: '额度耗尽',
    cooldown: '进入冷却', paused: '暂停使用', blocked: '暂时移出账号池',
    recovered: '重新加入账号池', restored: '重新加入账号池', resumed: '重新加入账号池', available: '重新加入账号池',
    reset_pending: '等待重置核验', rechecking: '重新核验额度', recheck: '重新核验额度',
    auth_error: '凭据需更新', expired: '凭据过期', unavailable: '暂无可用账号',
    rejoined: '重新加入账号池', interrupted: '恢复中断请求的状态',
  };
  let state = { snapshot: null, loadedAt: 0, loading: false, quietLoading: false, busy: null, expired: false, filter: 'all', query: '', expanded: new Set(), detailTabs: {}, disclosures: new Set(), blockedUntil: 0 };
  let timeUpdaters = [];
  let renderedIds = [];
  let renderedSignature = null;
  let pollTimer = null;
  let pollCount = 0;
  let loadWaiters = [];
  let schedulerEventsSignature = null;

  const concurrencyDialog = byId('concurrency-dialog');
  const features = ['reason', 'image_gen'];
  let selection = new Set();
  let concurrency = {config: null, mode: null, targets: [], loading: false, saving: false, dirty: false, conflict: false, opener: null, generation: 0};
  let modelBusy = false;
  const importedIds = new URLSearchParams(window.location.search).get('ids');
  let importFilter = importedIds ? new Set(importedIds.split(',').filter((id) => /^acct_[0-9a-f]{24}$/.test(id)).slice(0, 500)) : null;
  if (importFilter && !importFilter.size) importFilter = null;

  function runtimeConcurrency(account, feature) {
    return account.concurrency?.[feature] || state.snapshot?.scheduler?.accounts?.[account.id]?.concurrency?.[feature] || null;
  }
  function concurrencyContent(account, feature) {
    const value = runtimeConcurrency(account, feature), box = el('div', 'account-concurrency');
    const active = value?.in_flight ?? value?.active;
    box.append(el('span', 'account-concurrency-value', value ? `并发 ${count(active)} / ${count(value.limit)}` : '并发状态未提供'));
    if (value) {
      const waiting = value.state === 'draining' ? '等待现有请求结束' : value.state === 'full' ? '并发已满' : '';
      box.append(el('span', 'quota-note', [value.source === 'account' ? '账号自定义' : '继承默认', waiting].filter(Boolean).join(' · ')));
      box.dataset.state = value.state || 'unknown';
    }
    const conflict = value?.conflict || account.concurrency?.conflict || state.snapshot?.scheduler?.accounts?.[account.id]?.concurrency?.conflict || concurrency.config?.accounts?.[account.id]?.conflict;
    if (conflict) box.append(el('span', 'quota-note is-warning', '身份归并冲突，已采用较小上限'));
    return box;
  }
  function accountCatalog(account) {
    return account.model_catalog || state.snapshot?.model_catalog?.accounts?.find?.((item) => item.account_id === account.id) || {};
  }
  function lunaStatus(account) {
    const catalog = accountCatalog(account), check = catalog.checks?.['gpt-6-luna'];
    if (check?.stale) return {text: '历史核查已过期', tone: 'muted'};
    if (check?.status === 'model_mismatch' || (check?.status === 'verified' && ((check.reported_model && check.reported_model !== 'gpt-6-luna') || (check.sent_model && check.sent_model !== 'gpt-6-luna')))) return {text: '调用成功，但模型不一致', tone: 'warning'};
    if (check?.status === 'verified' && check.reported_model === 'gpt-6-luna') return {text: '调用成功 · 模型标签一致', tone: 'success'};
    if (['denied', 'error'].includes(check?.status)) return {text: check.status === 'denied' ? '调用被拒绝' : '调用失败', tone: 'error'};
    if (Array.isArray(catalog.models) && catalog.checked_at && ['fresh', 'stale'].includes(catalog.status)) {
      return {text: (catalog.status === 'stale' ? '旧' : '') + (catalog.models.includes('gpt-6-luna') ? '目录已列出 · 调用未核查' : '目录未发现'), tone: 'muted'};
    }
    return {text: '未核查', tone: 'muted'};
  }
  function lunaBadge(account) {
    const value = lunaStatus(account), node = el('span', 'account-luna-status', 'Luna · ' + value.text);
    node.dataset.tone = value.tone;
    const catalog = accountCatalog(account), checked = catalog.checks?.['gpt-6-luna']?.checked_at;
    node.title = `gpt-6-luna；调用核查 ${dateText(checked)}；目录更新 ${dateText(catalog.checked_at)}。目录出现不代表调用成功，响应标签不证明底层模型身份。`;
    return node;
  }
  function modelCatalogDetails(account) {
    const box = section('Luna 与账号模型目录'), catalog = accountCatalog(account), check = catalog.checks?.['gpt-6-luna'];
    box.append(lunaBadge(account), definition([['请求模型', check?.requested_model || 'gpt-6-luna'], ['发送模型', check?.sent_model], ['响应模型', check?.reported_model], ['调用核查时间', dateText(check?.checked_at)], ['目录更新时间', dateText(catalog.checked_at)]]));
    const models = Array.isArray(catalog.models) ? catalog.models.filter((id) => typeof id === 'string' && /^[A-Za-z0-9._-]{1,100}$/.test(id)) : [];
    box.append(el('p', 'detail-context-note', models.length ? '目录：' + models.join(' · ') : '尚无可展示的目录。'));
    box.append(el('p', 'field-hint', '刷新目录只读取上游元数据，不生成内容。目录列出与模型调用核查分别记录；标签一致也不证明底层模型身份。'));
    if (['stale', 'expired', 'error'].includes(catalog.status)) box.append(el('p', 'field-hint', '目录为旧快照或最近读取失败，请刷新后查看。'));
    return box;
  }
  function syncSelection() {
    const visible = filteredAccounts(), selectedVisible = visible.filter((account) => selection.has(account.id)).length;
    const all = byId('accounts-select-all'); all.checked = Boolean(visible.length) && selectedVisible === visible.length;
    all.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
    all.disabled = !visible.length || state.expired || concurrency.saving;
    byId('accounts-select-label').textContent = `全选当前筛选的 ${visible.length} 个账号`;
    const hidden = selection.size - selectedVisible;
    byId('accounts-selection-count').textContent = selection.size ? `已选 ${selection.size} 个${hidden ? ` · ${hidden} 个不在当前视图` : ''}` : '未选择账号';
    byId('concurrency-batch').disabled = !selection.size || state.expired || concurrency.saving;
    byId('accounts-clear-selection').disabled = !selection.size || concurrency.saving;
    byId('concurrency-defaults').disabled = state.expired || concurrency.saving;
    byId('models-refresh-all').disabled = !accounts().length || modelBusy || state.expired;
    byId('models-refresh-all').textContent = modelBusy ? '正在刷新目录…' : '刷新模型目录';
    ui['accounts-rows'].querySelectorAll('[data-concurrency-account]').forEach((button) => { button.disabled = state.expired || concurrency.saving; });
    ui['accounts-rows'].querySelectorAll('[data-select-account]').forEach((input) => { input.disabled = state.expired || concurrency.saving; });
  }
  function concurrencyError(message, reload = false) {
    byId('concurrency-error').textContent = message; byId('concurrency-error').hidden = !message;
    byId('concurrency-reload').hidden = !reload;
  }
  function concurrencyControls() {
    const locked = concurrency.loading || concurrency.saving || state.expired;
    byId('concurrency-form').setAttribute('aria-busy', String(concurrency.loading || concurrency.saving));
    byId('concurrency-loading').hidden = !concurrency.loading;
    byId('concurrency-fields').hidden = concurrency.loading || !concurrency.config;
    byId('concurrency-save').disabled = locked || !concurrency.config || !concurrency.dirty || concurrency.conflict;
    byId('concurrency-save').textContent = concurrency.saving ? '正在保存…' : '保存设置';
    byId('concurrency-cancel').disabled = concurrency.saving;
    byId('concurrency-reload').disabled = locked;
    for (const feature of features) {
      const mode = byId(`concurrency-${feature}-mode`), input = byId(`concurrency-${feature}-value`);
      mode.disabled = locked; input.disabled = locked || (concurrency.mode !== 'defaults' && mode.value !== 'custom');
    }
    syncSelection();
  }
  function populateConcurrency() {
    const config = concurrency.config, row = config.accounts?.[concurrency.targets[0]];
    for (const feature of features) {
      const mode = byId(`concurrency-${feature}-mode`), input = byId(`concurrency-${feature}-value`);
      mode.hidden = concurrency.mode === 'defaults';
      mode.replaceChildren(...(concurrency.mode === 'batch' ? ['keep', 'inherit', 'custom'] : ['inherit', 'custom']).map((value) => { const option = el('option', '', {keep: '保持不变', inherit: '继承系统默认', custom: '自定义'}[value]); option.value = value; return option; }));
      const override = row?.overrides?.[feature];
      mode.value = concurrency.mode === 'batch' ? 'keep' : concurrency.mode === 'defaults' || Number.isInteger(override) ? 'custom' : 'inherit';
      input.value = String(concurrency.mode === 'account' && Number.isInteger(override) ? override : config.defaults[feature]);
      byId(`concurrency-${feature}-hint`).textContent = `系统默认 ${config.defaults[feature]} · ${concurrency.mode === 'defaults' ? '仅影响继承此项的账号' : '选择继承后会跟随默认值变化'}`;
    }
    const names = concurrency.targets.map((id) => accounts().find((account) => account.id === id)?.label || '已移除账号');
    const description = concurrency.mode === 'defaults' ? '修改系统默认值，账号自定义值保持不变。' : concurrency.mode === 'batch' ? `本次固定修改 ${names.length} 个账号。保持不变不会覆盖该项现有设置。` : `为 ${names[0]} 设置独立上限，两项可分别继承默认。`;
    const conflict = row?.conflict;
    byId('concurrency-description').textContent = description + (conflict ? ' 身份归并时检测到设置冲突，已采用较小值，可在此重新确认。' : '');
    byId('concurrency-default-summary').textContent = `默认大模型 ${config.defaults.reason} · 生图 ${config.defaults.image_gen}；账号可分别覆盖。`;
    concurrency = {...concurrency, dirty: false, conflict: false}; concurrencyControls();
  }
  async function readConcurrency() {
    const generation = concurrency.generation;
    concurrency = {...concurrency, loading: true}; concurrencyError(''); concurrencyControls();
    try {
      const config = await request('/local-console/accounts/concurrency');
      if (!Number.isInteger(config.revision) || !features.every((name) => Number.isInteger(config.defaults?.[name]) && config.defaults[name] >= 1 && config.defaults[name] <= 10) || !config.accounts) throw new Error('无法识别并发配置，请重新载入。');
      if (generation !== concurrency.generation || !concurrencyDialog.open) return;
      concurrency = {...concurrency, config}; populateConcurrency();
    } catch (cause) { if (generation === concurrency.generation && concurrencyDialog.open) concurrencyError(cause instanceof SessionExpired ? '登录已过期，请重新登录。' : '无法读取并发配置，请重新载入。', !(cause instanceof SessionExpired)); }
    finally { if (generation === concurrency.generation) { concurrency = {...concurrency, loading: false}; concurrencyControls(); if (concurrency.config && concurrencyDialog.open && !state.expired) byId(concurrency.mode === 'defaults' ? 'concurrency-reason-value' : 'concurrency-reason-mode').focus(); } }
  }
  function openConcurrency(mode, ids = []) {
    if (state.expired || concurrency.saving) return;
    concurrency = {...concurrency, mode, targets: [...ids], config: null, dirty: false, conflict: false, opener: document.activeElement, generation: concurrency.generation + 1};
    byId('concurrency-title').textContent = mode === 'defaults' ? '系统默认并发' : mode === 'batch' ? `批量设置并发 · ${ids.length} 个账号` : '账号并发设置';
    byId('concurrency-description').textContent = '正在读取最新配置。';
    concurrencyDialog.showModal(); void readConcurrency();
  }
  function closeConcurrency() {
    if (concurrency.saving) return;
    concurrencyDialog.close(); concurrency = {...concurrency, dirty: false, generation: concurrency.generation + 1, loading: false};
    const opener = concurrency.opener?.isConnected ? concurrency.opener : byId('concurrency-defaults'); opener.focus();
  }
  async function saveConcurrency(event) {
    event.preventDefault(); if (concurrency.loading || concurrency.saving || !concurrency.config || !concurrency.dirty || state.expired || concurrency.conflict) return;
    const changes = {};
    for (const feature of features) {
      const mode = concurrency.mode === 'defaults' ? 'custom' : byId(`concurrency-${feature}-mode`).value;
      if (mode === 'keep') continue;
      if (mode === 'inherit') { changes[feature] = null; continue; }
      const input = byId(`concurrency-${feature}-value`), value = Number(input.value);
      if (!/^\d+$/.test(input.value) || !Number.isInteger(value) || value < 1 || value > 10) { concurrencyError('并发上限必须是 1–10 的整数。'); input.focus(); return; }
      changes[feature] = value;
    }
    if (!Object.keys(changes).length) { concurrencyError('请选择至少一项要修改的设置。'); return; }
    const path = concurrency.mode === 'defaults' ? '/local-console/accounts/concurrency/defaults' : concurrency.mode === 'batch' ? '/local-console/accounts/concurrency/batch' : `/local-console/accounts/${encodeURIComponent(concurrency.targets[0])}/concurrency`;
    const body = {revision: concurrency.config.revision, [concurrency.mode === 'defaults' ? 'limits' : 'overrides']: changes, ...(concurrency.mode === 'batch' ? {account_ids: [...concurrency.targets]} : {})};
    concurrency = {...concurrency, saving: true}; concurrencyError(''); concurrencyControls();
    try {
      const saved = await request(path, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      if (features.every((name) => Number.isInteger(saved?.defaults?.[name]))) { concurrency = {...concurrency, config: saved}; byId('concurrency-default-summary').textContent = `默认大模型 ${saved.defaults.reason} · 生图 ${saved.defaults.image_gen}；账号可分别覆盖。`; }
      concurrency = {...concurrency, saving: false, dirty: false}; closeConcurrency();
      feedback('并发设置已保存。新请求使用新上限，执行中请求会自然结束。');
      window.ConsoleUI?.toast('并发设置已保存', {tone: 'success'});
      await loadAccounts();
    } catch (cause) {
      const uncertain = cause.name === 'AbortError' || cause instanceof TypeError;
      concurrency = {...concurrency, conflict: cause.status === 409 || uncertain};
      concurrencyError(cause.status === 409 ? '配置已被其他操作更新。输入已保留，请载入最新配置后重新编辑。' : uncertain ? '连接中断，保存结果尚未确认。输入已保留，请载入最新配置核对结果后再操作。' : cause instanceof SessionExpired ? '登录已过期，输入已保留，请重新登录。' : cause.message, !state.expired);
    } finally { concurrency = {...concurrency, saving: false}; concurrencyControls(); }
  }
  async function refreshModels() {
    if (modelBusy || state.expired || !accounts().length) return;
    modelBusy = true; syncSelection(); error();
    try { applySnapshot(await request('/local-console/accounts/models/refresh', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}, 180000)); feedback('模型目录已更新；目录查询不等于实际模型调用验证。'); }
    catch (cause) { if (!(cause instanceof SessionExpired)) error('模型目录刷新未完成，请稍后更新本地视图检查结果。'); }
    finally { modelBusy = false; syncSelection(); }
  }

  byId('display-timezone').textContent = `时间按 ${dateFormat.resolvedOptions().timeZone} 显示。`;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
  function count(value) { return finite(value) ? numberFormat.format(value) : '—'; }
  function timestamp(value) {
    if (value === null || value === undefined || value === '') return null;
    const parsed = typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function dateText(value) { const time = timestamp(value); return time === null ? '未提供' : dateFormat.format(time); }
  function duration(seconds) {
    if (!finite(seconds) || seconds < 0) return '未提供';
    if (seconds % 86400 === 0) return `${seconds / 86400} 天`;
    if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
    if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
    return `${numberFormat.format(seconds)} 秒`;
  }
  function countdown(milliseconds) {
    const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.ceil(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时 ${minutes % 60} 分钟`;
    return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
  }
  function accounts() { return state.snapshot?.accounts || []; }
  function error(message = '') { ui['accounts-error'].textContent = message; ui['accounts-error'].hidden = !message; }
  function feedback(message = '') { ui['accounts-feedback'].textContent = message; }
  function timed(update) { timeUpdaters = [...timeUpdaters, update]; update(); }

  function schedulerFeature(account, feature) {
    const value = state.snapshot?.scheduler?.accounts?.[account.id]?.[feature];
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  }

  function schedulerState(value) {
    if (state.snapshot?.scheduler?.enabled === false) return 'disabled';
    if (!value) return 'unknown';
    return Object.hasOwn(schedulerStateLabels, value.state) ? value.state : value.available === true ? 'available' : 'unknown';
  }

  function schedulerReason(value) {
    const reasonBlocked = typeof value === 'string' && value.startsWith('reason_');
    const code = reasonBlocked ? value.slice(7) : value;
    const description = Object.hasOwn(schedulerReasonLabels, code) ? schedulerReasonLabels[code] : '后台已更新该账号的调度状态';
    return reasonBlocked ? `大模型额度侧：${description}` : description;
  }

  function schedulerBadge(value) {
    const status = schedulerState(value);
    const reasonBlocked = typeof value?.reason === 'string' && value.reason.startsWith('reason_');
    const fallback = value?.available === true && ['unknown', 'stale'].includes(status);
    const text = reasonBlocked ? `大模型：${schedulerStateLabels[status]}` : `${schedulerStateLabels[status]}${fallback ? ' · 兜底' : ''}`;
    const badge = el('span', 'scheduler-state', text);
    badge.dataset.tone = ['ready', 'available', 'active'].includes(status) ? 'ready' : ['expired', 'auth_error'].includes(status) ? 'error' : ['exhausted', 'cooldown', 'reset_pending', 'rechecking', 'reserved', 'concurrency_full', 'draining'].includes(status) ? 'waiting' : 'unknown';
    return badge;
  }

  function schedulerContent(account, feature) {
    const box = el('div', 'scheduler-status-line');
    const value = schedulerFeature(account, feature);
    box.append(schedulerBadge(value));
    if (finite(value?.active) && value.active > 0) box.append(el('span', 'quota-note', `${count(value.active)} 个请求执行中`));
    const next = timestamp(value?.retry_at ?? (['exhausted', 'reset_pending'].includes(value?.state) ? value?.reset_at : null));
    if (next !== null) {
      const hint = el('span', 'quota-note'); box.append(hint);
      timed(() => { hint.textContent = Date.now() >= next ? '已到时间 · 待后台核验' : `${countdown(next - Date.now())}后可复查`; });
    }
    return box;
  }

  function renderSchedulerSummary() {
    const scheduler = state.snapshot?.scheduler;
    const enabled = scheduler?.enabled === true;
    const badge = byId('scheduler-status');
    badge.textContent = !state.snapshot ? '正在载入' : !scheduler ? '状态未提供' : enabled ? '按剩余额度自动选择' : '未启用';
    badge.dataset.state = enabled ? 'fresh' : 'unknown';
    byId('scheduler-incomplete').hidden = scheduler?.persistence_error !== true;
    byId('scheduler-updated').textContent = enabled ? '每 5 秒更新本地调度状态' : '跟随账号数据更新';
    [['reason', 'reason'], ['image_gen', 'image']].forEach(([feature, element]) => {
      const values = accounts().map((account) => schedulerFeature(account, feature));
      const available = values.filter((value) => value?.available === true).length;
      const active = values.reduce((sum, value) => sum + (finite(value?.active) ? Math.max(0, value.active) : 0), 0);
      const unknown = values.filter((value) => !value || ['unknown', 'stale'].includes(schedulerState(value))).length;
      byId(`scheduler-${element}-count`).textContent = scheduler ? `${enabled ? count(available) : '0'} / ${count(accounts().length)}` : '—';
      byId(`scheduler-${element}-detail`).textContent = !scheduler ? '等待调度状态' : !enabled ? '自动调度尚未启用' : `${count(active)} 个请求执行中${unknown ? ` · ${count(unknown)} 个待确认` : ''}`;
    });
  }

  function schedulerAccountName(id) {
    return accounts().find((account) => account.id === id)?.label || (id ? '已移除账号' : '账号池');
  }

  function schedulerEventText(event) {
    const title = Object.hasOwn(schedulerEventLabels, event.type) ? schedulerEventLabels[event.type] : '状态更新';
    const feature = event.feature === 'image_gen' ? '生图' : event.feature === 'reason' ? '大模型' : '账号';
    if (event.from_account_id && event.to_account_id) return `${feature} · ${title}：${schedulerAccountName(event.from_account_id)} → ${schedulerAccountName(event.to_account_id)}`;
    return `${feature} · ${title} · ${schedulerAccountName(event.to_account_id || event.account_id)}`;
  }

  function renderSchedulerEvents() {
    const rawEvents = state.snapshot?.scheduler?.events;
    const events = Array.isArray(rawEvents) ? rawEvents.filter((event) => event && typeof event === 'object').sort((left, right) => (timestamp(right.at) || 0) - (timestamp(left.at) || 0)).slice(0, 20) : [];
    const signature = JSON.stringify(events.map((event) => [event.at, schedulerEventText(event), event.reason ? schedulerReason(event.reason) : '']));
    byId('scheduler-events-empty').hidden = events.length > 0;
    byId('scheduler-latest-event').textContent = events.length ? schedulerEventText(events[0]) : state.snapshot ? '尚无调度记录' : '等待调度记录';
    if (signature === schedulerEventsSignature) return;
    schedulerEventsSignature = signature;
    const list = byId('scheduler-event-list');
    const previousScroll = list.scrollTop;
    const fragment = document.createDocumentFragment();
    events.forEach((event) => {
      const item = el('li', 'scheduler-event');
      const time = timestamp(event.at);
      const stamp = el('time', '', time === null ? '时间未提供' : shortDateFormat.format(time));
      if (time !== null) { stamp.dateTime = new Date(time).toISOString(); stamp.title = dateText(time); }
      const content = el('div'); content.append(el('p', '', schedulerEventText(event)));
      if (event.reason) content.append(el('p', 'field-hint', schedulerReason(event.reason)));
      item.append(stamp, content); fragment.append(item);
    });
    list.replaceChildren(fragment);
    list.scrollTop = previousScroll;
  }

  function featureExpired(feature) {
    const reset = timestamp(feature?.reset_at);
    return feature?.status === 'expired' || (reset !== null && reset <= Date.now());
  }

  function accountStatus(account) {
    if (account.credential_status === 'expired' || account.status === 'expired') return 'expired';
    if (account.status === 'error' || account.refresh?.status === 'failed') return 'error';
    const checked = timestamp(account.checked_at);
    const lifetime = state.snapshot?.stale_after_seconds;
    if (account.status === 'stale' || Object.values(account.features || {}).some(featureExpired)) return 'stale';
    if (checked !== null && finite(lifetime) && Date.now() - checked >= lifetime * 1000) return 'stale';
    return statusLabels[account.status] ? account.status : 'unknown';
  }

  function staleValue(account, feature) {
    return featureExpired(feature) || ['stale', 'expired', 'error'].includes(accountStatus(account));
  }

  function cooldownUntil(account) {
    const exact = timestamp(account.refresh?.next_refresh_at);
    if (exact !== null) return exact;
    const retry = account.refresh?.retry_after_seconds;
    return finite(retry) ? state.loadedAt + retry * 1000 : 0;
  }

  function refreshReason(account) {
    if (state.expired) return '请重新登录';
    if (state.loading && !state.quietLoading) return '载入中…';
    if (state.busy) return state.busy === '*' || state.busy === account.id ? '刷新中…' : '请稍候';
    if (account.refresh?.status === 'in_progress') return '刷新中…';
    if (account.credential_status === 'expired') return '凭据已过期';
    if (account.refresh?.status === 'unsupported') return '暂不支持';
    const until = Math.max(cooldownUntil(account), state.blockedUntil);
    if (until > Date.now()) return `${countdown(until - Date.now())}后刷新`;
    return '';
  }

  function filteredAccounts() {
    return accounts().filter((account) => (!importFilter || importFilter.has(account.id)) && (state.filter === 'all' || accountStatus(account) === state.filter) && String(account.label || '').toLocaleLowerCase().includes(state.query.toLocaleLowerCase()));
  }

  function renderSummary() {
    const list = accounts();
    byId('summary-accounts').textContent = state.snapshot ? count(list.length) : '—';
    byId('summary-fresh').textContent = state.snapshot ? count(list.filter((account) => accountStatus(account) === 'fresh').length) : '—';
    byId('summary-attention').textContent = state.snapshot ? count(list.filter((account) => accountStatus(account) !== 'fresh').length) : '—';
    const localCounts = list.map((account) => account.local_usage?.windows?.last_24h?.attempts);
    byId('summary-calls').textContent = state.snapshot && localCounts.every(finite) ? count(localCounts.reduce((sum, value) => sum + value, 0)) : '—';
    byId('usage-started').textContent = timestamp(state.snapshot?.usage_started_at) === null ? '本功能启用时' : dateText(state.snapshot.usage_started_at);
    byId('snapshot-updated').textContent = state.snapshot ? `页面读取：${dateText(state.snapshot.generated_at)}` : '尚未载入';
    byId('usage-incomplete').hidden = state.snapshot?.usage_recording_status !== 'incomplete';
    byId('cache-incomplete').hidden = !state.snapshot?.cache_persistence_error;
    const interval = state.snapshot?.auto_refresh_seconds;
    byId('automatic-refresh').textContent = finite(interval) ? interval > 0 ? `后台每 ${duration(interval)} 更新` : '后台自动刷新已关闭' : '';
    renderSchedulerSummary();
  }

  function updateControls() {
    if (concurrencyDialog.open) concurrencyControls(); else syncSelection();
    const list = accounts();
    ui['refresh-all'].disabled = !state.snapshot || list.length === 0 || list.every((account) => Boolean(refreshReason(account)));
    ui['refresh-all'].textContent = state.busy === '*' ? '正在刷新额度…' : state.busy ? '账号刷新中…' : '刷新全部额度 ↻';
    ui['reload-accounts'].disabled = (state.loading && !state.quietLoading) || Boolean(state.busy) || state.expired;
    ui['reload-accounts'].textContent = state.loading && !state.quietLoading ? '载入中…' : '更新本地视图';
    ui['refresh-all'].setAttribute('aria-busy', String(state.busy === '*'));
    ui['reload-accounts'].setAttribute('aria-busy', String(state.loading && !state.quietLoading));
    ui['accounts-rows'].querySelectorAll('[data-refresh-account]').forEach((button) => {
      const account = list.find((item) => item.id === button.dataset.refreshAccount);
      if (!account) return;
      const reason = refreshReason(account);
      button.disabled = Boolean(reason);
      button.textContent = reason || '刷新额度';
      button.setAttribute('aria-busy', String(state.busy === '*' || state.busy === account.id || account.refresh?.status === 'in_progress'));
    });
  }

  function appendReset(container, feature) {
    const note = el('span', 'quota-note');
    const reset = timestamp(feature?.reset_at);
    if (reset === null) { note.textContent = '重置时间未提供'; container.append(note); return; }
    const absolute = el('time', 'quota-note', `重置 ${shortDateFormat.format(reset)}`);
    absolute.dateTime = new Date(reset).toISOString();
    absolute.title = dateText(reset);
    container.append(absolute, note);
    timed(() => {
      const passed = Date.now() >= reset;
      note.classList.toggle('is-warning', passed);
      note.textContent = passed ? '已到重置时间 · 待刷新' : `距重置 ${countdown(reset - Date.now())}`;
    });
  }

  function quotaContent(account, feature) {
    const box = el('div', 'quota-content');
    const value = el('div', 'quota-value');
    const number = el('span', '', finite(feature?.remaining) ? count(feature.remaining) : '未返回');
    const old = el('span', 'quota-old-tag', '旧值');
    value.append(number, old);
    value.classList.toggle('is-unknown', !finite(feature?.remaining));
    box.append(value);
    timed(() => {
      const stale = staleValue(account, feature);
      value.classList.toggle('is-stale', stale);
      old.hidden = !stale || !finite(feature?.remaining);
    });
    if (finite(feature?.total) || finite(feature?.used)) {
      const parts = [finite(feature?.total) ? `总量 ${count(feature.total)}` : null, finite(feature?.used) ? `已用 ${count(feature.used)}` : null].filter(Boolean);
      box.append(el('span', 'quota-note', parts.join(' · ')));
    }
    if (feature?.blocked === true) box.append(el('span', 'quota-note is-warning', '上游标记受限'));
    appendReset(box, feature);
    return box;
  }

  function definition(entries) {
    const list = el('dl', 'detail-definition');
    entries.forEach(([label, value]) => {
      const row = el('div');
      row.append(el('dt', '', label), el('dd', '', value === null || value === undefined || value === '' ? '未提供' : value));
      list.append(row);
    });
    return list;
  }

  function section(title, className = '') {
    const node = el('section', `account-detail-section ${className}`.trim());
    node.append(el('h3', '', title));
    return node;
  }

  const fieldLabels = {
    feature_name: '能力', name: '名称', model: '模型', model_slug: '模型标识', type: '类型', limit_type: '限制类型',
    block_reason: '受限原因', remaining: '剩余', total: '总量', limit: '限制量', used: '已用', used_percent: '已用比例',
    window_seconds: '额度窗口', limit_window_seconds: '额度窗口', reset_after_seconds: '重置间隔', reset_after: '重置时间',
    reset_at: '重置时间', reset: '重置时间', resets_at: '重置时间', blocked: '受限', allowed: '允许', limit_reached: '达到限制',
    window: '窗口', windows: '窗口', primary_window: '主要窗口', secondary_window: '次要窗口',
    rate_limit: '速率额度', limits: '限制', max_requests: '请求上限', is_blocked: '受限',
  };

  function restriction(value, depth = 0) {
    if (value === null || value === undefined) return el('p', 'field-hint', '未提供');
    if (typeof value !== 'object') return el('p', 'field-hint', String(value));
    if (depth > 4) return el('p', 'field-hint', '嵌套限制较多，请查看上游说明。');
    const group = el('div', 'detail-subsection');
    if (Array.isArray(value)) {
      if (!value.length) group.append(el('p', 'field-hint', '未返回相关限制。'));
      value.forEach((item) => group.append(restriction(item, depth + 1)));
      return group;
    }
    const entries = [];
    const nested = [];
    Object.entries(value).forEach(([key, item]) => {
      if (!(key in fieldLabels)) return;
      if (item !== null && typeof item === 'object') { nested.push([fieldLabels[key], item]); return; }
      let displayed = item;
      if (item === null || item === undefined) displayed = '未提供';
      else if (typeof item === 'boolean') displayed = item ? '是' : '否';
      else if (['window_seconds', 'limit_window_seconds', 'reset_after_seconds'].includes(key)) displayed = duration(item);
      else if (['reset_after', 'reset_at', 'reset', 'resets_at'].includes(key)) displayed = dateText(item);
      else if (key === 'used_percent' && finite(item)) displayed = `${count(item)}%`;
      entries.push([fieldLabels[key], displayed]);
    });
    if (entries.length) group.append(definition(entries));
    nested.forEach(([label, item]) => { group.append(el('h4', '', label), restriction(item, depth + 1)); });
    if (!entries.length && !nested.length) group.append(el('p', 'field-hint', '未提供可显示的限制字段。'));
    return group;
  }

  function usageTable(usage) {
    const table = el('table', 'detail-table');
    const head = el('thead');
    const heading = el('tr');
    ['本地范围', '调用', '成功', '失败', '图片'].forEach((label) => { const cell = el('th', '', label); cell.scope = 'col'; heading.append(cell); });
    head.append(heading);
    const body = el('tbody');
    [['近 5 小时', usage?.windows?.last_5h], ['近 24 小时', usage?.windows?.last_24h], ['近 7 天', usage?.windows?.last_7d], ['累计', usage?.totals]].forEach(([label, values]) => {
      const row = el('tr');
      row.append(el('td', '', label), ...['attempts', 'succeeded', 'failed', 'generated_images'].map((key) => el('td', '', count(values?.[key]))));
      body.append(row);
    });
    table.append(head, body);
    return table;
  }

  function detailOverview(account) {
    const grid = el('div', 'account-detail-overview');
    const identity = section('凭据与快照');
    const credential = el('p', 'detail-credential');
    credential.append(el('strong', '', credentialLabels[account.credential_status] || '有效期未知'));
    credential.append(el('span', '', `${finite(account.token_count) ? count(account.token_count) : '—'} 份关联凭据`));
    identity.append(credential, definition([
      ['声明到期', dateText(account.credential_expires_at)],
      ['上次成功查询', dateText(account.checked_at)], ['最近查询尝试', dateText(account.refresh?.last_attempt_at)],
      ['额度来源', 'ChatGPT 网页接口'],
    ]), el('p', 'field-hint', '有效期来自凭据声明，不代表已通过实时认证。'));
    if (account.refresh?.error) identity.append(el('p', 'detail-error', `${account.refresh.error.message || '查询失败'}${finite(account.refresh.error.http_status) ? `（HTTP ${account.refresh.error.http_status}）` : ''}`));
    const local = section('本地调用统计');
    const table = usageTable(account.local_usage);
    table.setAttribute('aria-label', '该账号的本地调用统计');
    local.append(table, el('p', 'field-hint', '本机滚动统计，不是官方额度窗口。'));
    const totals = account.local_usage?.totals;
    const activity = el('dl', 'detail-activity');
    [['处理中', totals?.in_progress], ['生图调用', totals?.image_requests], ['累计取消', totals?.cancelled], ['累计中断', totals?.interrupted]].forEach(([label, value]) => {
      const item = el('div'); item.append(el('dt', '', label), el('dd', '', count(value))); activity.append(item);
    });
    local.append(activity);
    grid.append(identity, local);
    const footer = el('div', 'detail-overview-footer');
    footer.append(el('p', '', `开始记录 ${dateText(account.local_usage?.since)}`), el('p', '', `最近调用 ${dateText(totals?.last_request_at)}`));
    return [grid, footer];
  }

  function detailScheduling(account) {
    const grid = el('div', 'scheduler-detail-grid');
    ['reason', 'image_gen'].forEach((feature) => {
      const value = schedulerFeature(account, feature);
      const block = section(featureNames[feature], 'detail-scheduler');
      const heading = el('div', 'detail-scheduler-heading');
      heading.append(el('strong', 'detail-scheduler-value', finite(value?.effective_remaining) ? count(value.effective_remaining) : '未知'), el('span', '', '估算可分配'), schedulerBadge(value));
      block.append(heading, definition([
        ['可接收新请求', value ? (value.available === true && state.snapshot?.scheduler?.enabled === true ? '是' : '否') : '待确认'],
        ['已预留 / 待核对扣减', `${count(value?.reserved)} / ${count(value?.provisional)}`], ['执行中', count(value?.active)],
        ['下次复查', dateText(value?.retry_at)], ['上游重置', dateText(value?.reset_at)],
      ]), el('p', 'detail-scheduler-reason', value?.reason ? schedulerReason(value.reason) : '调度原因未提供'));
      grid.append(block);
    });
    const note = el('div', 'detail-context-note');
    note.append(el('p', '', '调度估算、预留和待核对扣减不等于上游实时余额。生图同时检查大模型与生图额度；重置后仍需后台核验。'), el('p', '', `最近选择 ${dateText(state.snapshot?.scheduler?.accounts?.[account.id]?.last_selected_at)}`));
    return [grid, note];
  }

  function hasRestriction(value) {
    if (Array.isArray(value)) return value.some(hasRestriction);
    if (value && typeof value === 'object') return Object.entries(value).some(([key, item]) => Object.hasOwn(fieldLabels, key) && hasRestriction(item));
    return value !== null && value !== undefined && value !== '';
  }

  function detailQuotas(account) {
    const features = el('div', 'detail-extra-quotas');
    const extra = Object.entries(account.features || {}).filter(([key]) => !['reason', 'image_gen'].includes(key));
    extra.forEach(([name, feature]) => {
      const block = section(featureNames[name] || name, 'detail-extra-quota');
      if (featureNames[name]) block.append(el('p', 'detail-feature-code', name));
      block.append(quotaContent(account, feature)); features.append(block);
    });
    if (!extra.length) features.append(el('p', 'detail-empty', '上游尚未返回其他能力额度。大模型与生图余额见账号列表。'));
    const raw = el('details', 'detail-diagnostics');
    const disclosureKey = `limits:${account.id}`;
    raw.open = state.disclosures.has(disclosureKey);
    raw.addEventListener('toggle', () => {
      if (!raw.isConnected) return;
      const disclosures = raw.open ? new Set([...state.disclosures, disclosureKey]) : new Set([...state.disclosures].filter((key) => key !== disclosureKey));
      state = { ...state, disclosures };
    });
    const summary = el('summary', '', '查看上游窗口与限制'); summary.dataset.focusKey = `limits:${account.id}`;
    raw.append(summary);
    const restrictions = el('div', 'detail-restrictions');
    [['模型限制', account.model_limits], ['额度窗口', account.limits_progress], ['额外窗口', account.windows], ['受限能力', account.blocked_features]].filter(([, values]) => hasRestriction(values)).forEach(([label, values]) => {
      const block = section(label); block.append(restriction(values)); restrictions.append(block);
    });
    if (!restrictions.children.length) restrictions.append(el('p', 'field-hint', '上游未返回可显示的窗口或限制。'));
    raw.append(restrictions, el('p', 'field-hint', '只显示接口明确返回的字段；未返回时不推断 5 小时或每周额度。'));
    return [features, raw];
  }

  function detailModels(account) {
    const models = account.local_usage?.models;
    if (!Array.isArray(models) || !models.length) return [modelCatalogDetails(account), el('p', 'detail-empty', '还没有模型调用记录。通过此服务调用后，记录会显示在这里。')];
    const wrapper = el('div', 'detail-model-table-wrap');
    const table = el('table', 'detail-table detail-model-table');
    table.setAttribute('aria-label', '该账号的本地模型调用记录');
    const head = el('thead'); const heading = el('tr');
    ['请求模型', '上游声明模型', '调用结果', '生成图片', '最近调用'].forEach((label) => { const cell = el('th', '', label); cell.scope = 'col'; heading.append(cell); });
    head.append(heading);
    const body = el('tbody');
    models.forEach((model) => {
      const row = el('tr');
      const requested = el('td');
      requested.append(el('span', 'feature-name', model.requested_model || '未记录模型'), el('span', 'quota-note', `发送上游：${model.sent_model || '未记录'}`));
      const upstream = el('td', 'detail-code', model.upstream_model || '未记录');
      const attempts = el('td'); attempts.append(el('span', '', `${count(model.attempts)} 次`), el('span', 'quota-note', `成功 ${count(model.succeeded)} · 失败 ${count(model.failed)}`));
      row.append(requested, upstream, attempts, el('td', '', count(model.generated_images)), el('td', '', dateText(model.last_request_at)));
      body.append(row);
    });
    table.append(head, body); wrapper.append(table);
    return [modelCatalogDetails(account), wrapper, el('p', 'detail-context-note', '记录仅覆盖本地调用；上游声明的模型名称不能证明实际底层模型身份。')];
  }

  function accountDetails(account, index) {
    const box = el('div', 'account-detail-shell');
    const tabs = [['overview', '概览', detailOverview], ['scheduling', '调度', detailScheduling], ['quotas', '更多额度', detailQuotas], ['models', '模型记录', detailModels]];
    const selected = tabs.some(([key]) => key === state.detailTabs[account.id]) ? state.detailTabs[account.id] : 'overview';
    const tablist = el('div', 'detail-tabs');
    tablist.dataset.motionTabs = '';
    tablist.setAttribute('role', 'tablist'); tablist.setAttribute('aria-label', `${account.label || '账号'} 详情`);
    const panels = el('div', 'detail-panels');
    const renderedPanels = new Set([selected]);
    function activate(key) {
      state = { ...state, detailTabs: { ...state.detailTabs, [account.id]: key } };
      const controls = tablist.querySelectorAll('button');
      tabs.forEach(([name, , content], panelIndex) => {
        const active = name === key;
        const control = controls[panelIndex];
        control.setAttribute('aria-selected', String(active)); control.tabIndex = active ? 0 : -1;
        const panel = panels.children[panelIndex]; panel.hidden = !active;
        if (active && !renderedPanels.has(name)) { panel.append(...content(account)); renderedPanels.add(name); }
        if (active) control.focus({ preventScroll: true });
      });
      // Preserve the tab strip so the shared indicator can move continuously.
      // A later identical poll also keeps these visited panels and their focus.
      renderedSignature = rowsSignature();
    }
    tabs.forEach(([key, label, content], tabIndex) => {
      const active = selected === key;
      const tab = el('button', 'detail-tab', label);
      const tabId = `detail-tab-${index}-${key}`;
      const panelId = `detail-panel-${index}-${key}`;
      tab.id = tabId; tab.type = 'button'; tab.tabIndex = active ? 0 : -1;
      tab.dataset.focusKey = `tab:${account.id}:${key}`;
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(active)); tab.setAttribute('aria-controls', panelId);
      tab.addEventListener('click', () => { activate(key); });
      tab.addEventListener('keydown', (event) => {
        const next = event.key === 'ArrowRight' ? (tabIndex + 1) % tabs.length : event.key === 'ArrowLeft' ? (tabIndex - 1 + tabs.length) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault(); activate(tabs[next][0]);
      });
      const panel = el('div', 'detail-tabpanel'); panel.id = panelId; panel.hidden = !active;
      panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tabId); panel.tabIndex = 0;
      panel.dataset.focusKey = `panel:${account.id}:${key}`;
      if (active) panel.append(...content(account));
      tablist.append(tab); panels.append(panel);
    });
    box.append(tablist, panels);
    return box;
  }

  function rowsSignature(filtered = filteredAccounts()) {
    return JSON.stringify([filtered, state.snapshot?.scheduler?.accounts, state.snapshot?.scheduler?.enabled,
      state.query, state.filter, [...selection], concurrency.saving, concurrency.config?.revision, state.busy, state.expired, [...state.expanded], state.detailTabs, [...state.disclosures]]);
  }

  function renderRows(revealedId = null) {
    const focusKey = document.activeElement?.dataset?.focusKey;
    const filtered = filteredAccounts();
    const signature = rowsSignature(filtered);
    // Local polling often returns identical rows. Keep their DOM and focus intact;
    // countdowns, aggregate status and the activity feed still update normally.
    if (!revealedId && renderedSignature === signature) {
      timeUpdaters.forEach((update) => update()); renderSummary(); renderSchedulerEvents(); updateControls(); return;
    }
    renderedSignature = signature;
    timeUpdaters = [];
    const fragment = document.createDocumentFragment();
    renderedIds = filtered.map((account) => account.id);
    filtered.forEach((account, index) => {
      const row = el('tr', 'account-row');
      const isExpanded = state.expanded.has(account.id);
      row.classList.toggle('is-expanded', isExpanded);
      const cells = ['账号', '快照状态', '大模型剩余 · reason', '生图剩余 · image_gen', '本地调用 · 近24小时', '上游更新', '操作'].map((label) => { const cell = el('td'); cell.dataset.label = label; return cell; });
      const identity = el('div', 'account-identity');
      const avatar = el('span', 'account-avatar', String(accounts().findIndex((item) => item.id === account.id) + 1).padStart(2, '0')); avatar.setAttribute('aria-hidden', 'true');
      const identityText = el('div', 'account-identity-text');
      identityText.append(el('span', 'account-name', account.label || `账号 ${index + 1}`));
      if (finite(account.token_count)) identityText.append(el('span', 'account-meta', `${count(account.token_count)} 份关联凭据`));
      identityText.append(lunaBadge(account));
      const checkLabel = el('label', 'account-select-control account-row-select'), checkbox = el('input');
      checkbox.type = 'checkbox'; checkbox.checked = selection.has(account.id); checkbox.dataset.selectAccount = account.id; checkbox.dataset.focusKey = `select:${account.id}`;
      checkbox.setAttribute('aria-label', `选择 ${account.label || '账号'}`);
      checkbox.addEventListener('change', () => { selection = checkbox.checked ? new Set([...selection, account.id]) : new Set([...selection].filter((id) => id !== account.id)); syncSelection(); });
      checkLabel.append(checkbox); identity.append(checkLabel, identityText); cells[0].append(identity);
      const badge = el('span', 'account-state');
      const statusNote = el('span', 'quota-note');
      cells[1].append(badge, statusNote);
      timed(() => {
        const status = accountStatus(account);
        const pending = state.busy === '*' || state.busy === account.id || account.refresh?.status === 'in_progress';
        badge.textContent = pending ? '刷新中' : statusLabels[status]; badge.dataset.state = pending ? 'unknown' : status;
        statusNote.textContent = status === 'error' ? '展开查看原因' : status === 'stale' ? '保留上次返回值' : status === 'expired' ? '请更新账号凭据' : '';
        statusNote.hidden = !statusNote.textContent;
      });
      cells[2].append(quotaContent(account, account.features?.reason), schedulerContent(account, 'reason'), concurrencyContent(account, 'reason'));
      cells[3].append(quotaContent(account, account.features?.image_gen), schedulerContent(account, 'image_gen'), concurrencyContent(account, 'image_gen'));
      const usage = account.local_usage?.windows?.last_24h;
      cells[4].append(el('div', 'usage-primary', `${count(usage?.attempts)} 次调用`), el('p', 'usage-secondary', `成功 ${count(usage?.succeeded)} · 失败 ${count(usage?.failed)}`), el('p', 'usage-secondary', `生成图片 ${count(usage?.generated_images)} 张`));
      const updated = el('time', '', timestamp(account.checked_at) === null ? '未查询' : shortDateFormat.format(timestamp(account.checked_at)));
      if (timestamp(account.checked_at) !== null) { updated.dateTime = new Date(timestamp(account.checked_at)).toISOString(); updated.title = dateText(account.checked_at); }
      cells[5].append(updated);
      const actions = el('div', 'row-actions');
      const refreshButton = el('button', 'button button-quiet refresh-account', '刷新额度');
      refreshButton.type = 'button'; refreshButton.dataset.refreshAccount = account.id; refreshButton.dataset.focusKey = `refresh:${account.id}`;
      refreshButton.setAttribute('aria-label', `刷新 ${account.label || '该账号'} 的额度`);
      refreshButton.addEventListener('click', () => { void refreshAccounts(account.id); });
      const detailsButton = el('button', 'button button-secondary details-toggle');
      const chevron = el('span', 'details-chevron'); chevron.setAttribute('aria-hidden', 'true');
      detailsButton.append(el('span', '', isExpanded ? '收起详情' : '展开详情'), chevron);
      detailsButton.type = 'button'; detailsButton.dataset.focusKey = `details:${account.id}`;
      detailsButton.setAttribute('aria-expanded', String(isExpanded)); detailsButton.setAttribute('aria-controls', `account-details-${index}`);
      detailsButton.addEventListener('click', () => {
        const expanded = state.expanded.has(account.id) ? new Set([...state.expanded].filter((id) => id !== account.id)) : new Set([...state.expanded, account.id]);
        state = { ...state, expanded }; renderRows(expanded.has(account.id) ? account.id : null);
      });
      const settings = el('button', 'button button-quiet', '设置并发'); settings.type = 'button'; settings.dataset.concurrencyAccount = account.id; settings.dataset.focusKey = `concurrency:${account.id}`; settings.setAttribute('aria-label', `设置 ${account.label || '账号'} 的并发`); settings.addEventListener('click', () => openConcurrency('account', [account.id]));
      actions.append(refreshButton, settings, detailsButton); cells[6].append(actions); row.append(...cells); fragment.append(row);
      const detailRow = el('tr', 'account-detail-row'); detailRow.id = `account-details-${index}`; detailRow.hidden = !isExpanded;
      if (isExpanded) {
        const cell = el('td'); cell.colSpan = 7;
        const details = accountDetails(account, index);
        details.classList.toggle('is-entering', account.id === revealedId);
        cell.append(details); detailRow.append(cell);
      }
      fragment.append(detailRow);
    });
    ui['accounts-rows'].replaceChildren(fragment);
    ui['accounts-table-wrap'].hidden = filtered.length === 0;
    ui['accounts-empty'].hidden = !state.snapshot || filtered.length > 0;
    const resultCount = state.snapshot ? `${filtered.length} / ${accounts().length} 个账号` : '尚未载入';
    if (byId('accounts-count').textContent !== resultCount) byId('accounts-count').textContent = resultCount;
    const activeFilters = [importFilter ? `本次导入 ${importFilter.size} 个账号` : '', state.query ? `标签含「${state.query}」` : '', state.filter !== 'all' ? statusLabels[state.filter] : ''].filter(Boolean);
    byId('account-filter-feedback').hidden = activeFilters.length === 0;
    byId('active-account-filters').textContent = activeFilters.join(' · ');
    const noAccounts = accounts().length === 0;
    byId('empty-title').textContent = noAccounts ? '还没有导入账号' : '没有匹配的账号';
    byId('empty-description').textContent = noAccounts ? '导入账号凭据后，即可查询额度与查看本地使用情况。' : '试试其他关键词，或清除状态筛选。';
    byId('empty-import').hidden = !noAccounts; byId('clear-filters').hidden = noAccounts;
    renderSummary(); renderSchedulerEvents(); updateControls();
    if (focusKey) [...ui['accounts-rows'].querySelectorAll('[data-focus-key]')].find((button) => button.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
  }

  class SessionExpired extends Error {}
  async function request(path, options = {}, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal });
      if (response.status === 401) {
        state = { ...state, expired: true }; ui['session-expired'].hidden = false; updateControls();
        throw new SessionExpired('登录已过期。');
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        if (response.status === 429) {
          const retry = Number(response.headers.get('Retry-After'));
          if (Number.isFinite(retry) && retry > 0) state = { ...state, blockedUntil: Date.now() + retry * 1000 };
        }
        throw Object.assign(new Error(typeof data?.detail === 'string' ? data.detail : `请求失败（HTTP ${response.status}），请稍后重试。`), {status: response.status});
      }
      return data;
    } finally { window.clearTimeout(timer); }
  }

  function applySnapshot(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.accounts) || !snapshot.accounts.every((account) => account && typeof account === 'object' && typeof account.id === 'string' && account.id.length > 0)) throw new Error('服务返回了无法识别的账号数据，请重试。');
    const currentIds = new Set(snapshot.accounts.map((account) => account.id));
    const removed = [...selection].filter((id) => !currentIds.has(id));
    selection = new Set([...selection].filter((id) => currentIds.has(id)));
    if (removed.length) feedback(`${removed.length} 个账号已移除，已取消选择。`);
    const defaults = snapshot.scheduler?.concurrency?.defaults;
    if (features.every((feature) => Number.isInteger(defaults?.[feature]))) byId('concurrency-default-summary').textContent = `默认大模型 ${defaults.reason} · 生图 ${defaults.image_gen}；账号可分别覆盖。`;
    state = { ...state, snapshot, loadedAt: Date.now() };
    renderRows();
    schedulePoll();
  }

  function schedulePoll() {
    if (pollTimer) window.clearTimeout(pollTimer);
    if (state.expired || !accounts().some((account) => account.refresh?.status === 'in_progress')) { pollCount = 0; return; }
    if (pollCount >= 60) { feedback('后台刷新仍未完成，可稍后点击「更新本地视图」查看结果。'); return; }
    pollTimer = window.setTimeout(() => { pollCount += 1; void loadAccounts(true); }, 2000);
  }

  async function loadAccounts(quiet = false) {
    if (!quiet && state.loading && state.quietLoading) await new Promise((resolve) => { loadWaiters = [...loadWaiters, resolve]; });
    if (state.loading || state.busy || state.expired) return;
    state = { ...state, loading: true, quietLoading: quiet };
    if (!quiet) error();
    ui['accounts-loading'].hidden = Boolean(state.snapshot); updateControls();
    try { applySnapshot(await request('/local-console/accounts')); }
    catch (cause) {
      if (!(cause instanceof SessionExpired)) error(cause.name === 'AbortError' ? '本地服务响应超时，请重新载入。' : cause instanceof TypeError ? '无法连接本地服务，请确认服务正在运行。' : cause.message);
      if (!state.snapshot) byId('accounts-count').textContent = '载入失败';
    } finally {
      state = { ...state, loading: false, quietLoading: false }; ui['accounts-loading'].hidden = true; updateControls();
      const waiting = loadWaiters; loadWaiters = []; waiting.forEach((resolve) => resolve());
    }
  }

  async function refreshAccounts(accountId = null) {
    if (state.loading && state.quietLoading) await new Promise((resolve) => { loadWaiters = [...loadWaiters, resolve]; });
    if (state.busy || state.loading || state.expired) return;
    const selected = accountId ? accounts().find((account) => account.id === accountId) : null;
    if (accountId && (!selected || refreshReason(selected))) return;
    if (!accountId && accounts().every((account) => refreshReason(account))) return;
    state = { ...state, busy: accountId || '*' };
    error(); feedback(accountId ? '正在查询该账号的上游额度…' : '正在查询账号额度，旧快照会保留至查询完成。'); renderRows();
    try {
      const snapshot = await request('/local-console/accounts/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(accountId ? { account_id: accountId } : {}) }, 180000);
      applySnapshot(snapshot);
      const results = accountId ? accounts().filter((account) => account.id === accountId) : accounts();
      const failures = results.filter((account) => account.refresh?.status === 'failed').length;
      if (results.length && results.every((account) => account.refresh?.status === 'cooldown')) feedback('尚未到下次刷新时间，请等待倒计时后再试。');
      else if (results.some((account) => account.refresh?.status === 'in_progress')) feedback('后台正在刷新，页面会自动载入结果。');
      else feedback(failures ? `刷新结果已载入，${failures} 个账号查询失败。展开详情可查看原因。` : '刷新结果已载入。');
    } catch (cause) {
      if (!(cause instanceof SessionExpired)) error(cause.name === 'AbortError' ? '等待刷新结果超时。后台可能仍在处理，请稍后重新载入。' : cause instanceof TypeError ? '连接中断，请稍后重新载入查看刷新结果。' : cause.message);
      feedback();
    } finally {
      state = { ...state, busy: null }; renderRows();
    }
  }

  ui['account-search'].addEventListener('input', () => { selection = new Set(); state = { ...state, query: ui['account-search'].value.trim() }; renderRows(); });
  ui['account-status-filter'].addEventListener('change', () => { selection = new Set(); state = { ...state, filter: ui['account-status-filter'].value }; renderRows(); });
  function clearFilters() {
    selection = new Set(); importFilter = null;
    state = { ...state, query: '', filter: 'all' };
    ui['account-search'].value = ''; ui['account-status-filter'].value = 'all';
    renderRows(); ui['account-search'].focus();
  }
  byId('clear-filters').addEventListener('click', clearFilters);
  byId('reset-account-filters').addEventListener('click', clearFilters);
  ui['reload-accounts'].addEventListener('click', () => { pollCount = 0; void loadAccounts(); });
  ui['refresh-all'].addEventListener('click', () => { void refreshAccounts(); });
  ui['accounts-logout'].addEventListener('click', async () => {
    ui['accounts-logout'].disabled = true;
    try {
      await request('/local-console/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      window.location.assign('/login');
    } catch (cause) {
      if (cause instanceof SessionExpired) window.location.assign('/login');
      else { error('退出失败，请稍后重试。'); ui['accounts-logout'].disabled = false; }
    }
  });
  window.setInterval(() => {
    if (!state.snapshot) return;
    const ids = filteredAccounts().map((account) => account.id);
    if (ids.length !== renderedIds.length || ids.some((id, index) => id !== renderedIds[index])) { renderRows(); return; }
    timeUpdaters.forEach((update) => update()); renderSummary(); updateControls();
  }, 1000);
  window.setInterval(() => {
    if (!document.hidden && state.snapshot?.scheduler?.enabled === true && !state.busy && !state.loading && !state.expired) void loadAccounts(true);
  }, 5000);
  window.setInterval(() => {
    if (!document.hidden && state.snapshot?.scheduler?.enabled !== true && state.snapshot?.auto_refresh_seconds > 0 && !state.busy && !state.loading && !state.expired) void loadAccounts(true);
  }, 30000);
  byId('concurrency-defaults').addEventListener('click', () => openConcurrency('defaults'));
  byId('concurrency-batch').addEventListener('click', () => openConcurrency('batch', [...selection]));
  byId('accounts-select-all').addEventListener('change', () => {
    const ids = filteredAccounts().map((account) => account.id);
    selection = byId('accounts-select-all').checked ? new Set([...selection, ...ids]) : new Set([...selection].filter((id) => !ids.includes(id)));
    renderRows();
  });
  byId('accounts-clear-selection').addEventListener('click', () => { selection = new Set(); renderRows(); });
  byId('concurrency-form').addEventListener('submit', saveConcurrency);
  byId('concurrency-cancel').addEventListener('click', closeConcurrency);
  byId('concurrency-reload').addEventListener('click', () => { void readConcurrency(); });
  concurrencyDialog.addEventListener('cancel', (event) => { event.preventDefault(); closeConcurrency(); });
  for (const feature of features) for (const [suffix, type] of [['mode', 'change'], ['value', 'input']]) byId(`concurrency-${feature}-${suffix}`).addEventListener(type, () => { concurrency = {...concurrency, dirty: true}; concurrencyControls(); });
  byId('models-refresh-all').addEventListener('click', () => { void refreshModels(); });
  window.addEventListener('beforeunload', (event) => { if (concurrencyDialog.open && concurrency.dirty) { event.preventDefault(); event.returnValue = ''; } });
  void loadAccounts();
})();
