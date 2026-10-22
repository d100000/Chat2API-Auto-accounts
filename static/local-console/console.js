(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const ui = Object.fromEntries([
    'chat-form', 'prompt', 'model', 'max-tokens', 'stream', 'send-button', 'stop-button',
    'clear-button', 'quick-test', 'chat-messages', 'empty-state', 'turn-count', 'page-error',
    'request-state', 'request-time', 'request-http', 'first-token-time', 'service-status',
    'service-status-text', 'refresh-session', 'logout-button', 'request-options', 'copy-endpoint',
    'configuration-summary', 'request-mode', 'history-mode', 'image-url', 'system-prompt', 'advanced-options', 'image-mode-note',
    'mode-text', 'mode-image', 'fill-text-example', 'fill-image-example',
    'chat-clear-dialog', 'chat-clear-form', 'chat-clear-cancel',
  ].map((id) => [id, byId(id)]));
  const QUICK_PROMPT = '请只回复 OK。';
  const MAX_RAW_CHARS = 200000;
  let history = [];
  let active = null;
  let sessionReady = false;
  let refreshing = false;
  let requestSequence = 0;
  let selectedMode = 'text';
  let previousChatModel = ui.model.value.trim() || 'gpt-6-luna';

  byId('shortcut-label').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ Enter' : 'Ctrl + Enter';
  // Narrow screens start with the native disclosure collapsed. Reopen it when
  // returning to desktop so the parameter column never starts empty.
  const optionsMedia = window.matchMedia('(max-width: 760px)');
  ui['request-options'].open = !optionsMedia.matches;
  optionsMedia.addEventListener?.('change', (event) => { ui['request-options'].open = !event.matches; });
  ui['request-options'].querySelector('summary')?.addEventListener('click', (event) => {
    if (!optionsMedia.matches) event.preventDefault();
  });

  function updateConfigurationSummary() {
    const model = ui.model.value.trim() || '未设置模型';
    const label = `${ui['request-mode'].value === 'image' ? '生图' : '对话'} · ${model} · ${ui.stream.checked ? '流式' : '完整响应'}`;
    ui['configuration-summary'].textContent = label;
    ui['configuration-summary'].title = label;
    for (const mode of ['text', 'image']) {
      const selected = ui['request-mode'].value === mode;
      ui[`mode-${mode}`].setAttribute('aria-pressed', String(selected));
      ui[`mode-${mode}`].tabIndex = selected ? 0 : -1;
    }
  }

  function invalidConfiguration(field, message) {
    ui['request-options'].open = true;
    if (['max-tokens', 'history-mode', 'image-url', 'system-prompt'].includes(field.id)) ui['advanced-options'].open = true;
    field.setAttribute('aria-invalid', 'true');
    setError(message);
    field.focus();
  }

  function setError(message = '') {
    ui['page-error'].textContent = message;
    ui['page-error'].hidden = !message;
  }

  function updateControls() {
    const pending = Boolean(active);
    ui['send-button'].disabled = pending || !sessionReady || !ui.prompt.value.trim();
    ui['send-button'].textContent = pending ? (active.firstTokenAt === null ? '等待回复…' : '正在接收…') : ui['request-mode'].value === 'image' ? '请求生图 ↑' : '发送消息 ↑';
    ui['send-button'].setAttribute('aria-busy', String(pending));
    ui['quick-test'].disabled = pending || !sessionReady;
    ui['quick-test'].textContent = ui['request-mode'].value === 'image' ? '填入生图示例' : '运行最小测试';
    ui['clear-button'].disabled = pending || !ui['chat-messages'].querySelector('.exchange');
    ui['stop-button'].hidden = !pending;
    ui.prompt.readOnly = pending;
    for (const key of ['model', 'max-tokens', 'stream', 'request-mode', 'history-mode', 'image-url', 'system-prompt', 'mode-text', 'mode-image', 'fill-text-example', 'fill-image-example']) ui[key].disabled = pending;
    ui['chat-messages'].setAttribute('aria-busy', String(pending));
  }

  function setRequestState(label, state) {
    ui['request-state'].textContent = label;
    ui['request-state'].dataset.state = state;
  }

  function duration(milliseconds) {
    return `${(milliseconds / 1000).toFixed(1)} 秒`;
  }

  async function errorMessage(response) {
    const text = await response.text();
    let message = text;
    // Some upstream errors arrive as serialized JSON inside a detail string.
    for (let depth = 0; depth < 4; depth += 1) {
      try {
        const data = JSON.parse(message);
        const next = typeof data === 'string' ? data : data?.detail ?? data?.error?.message ?? data?.error ?? data?.message;
        if (typeof next !== 'string') break;
        message = next;
      } catch (_) { break; }
    }
    return message.slice(0, 1200) || `请求失败（HTTP ${response.status}）`;
  }

  async function refreshSession() {
    if (refreshing) return;
    refreshing = true;
    ui['refresh-session'].disabled = true;
    ui['refresh-session'].setAttribute('aria-busy', 'true');
    ui['refresh-session'].textContent = '正在刷新…';
    ui['service-status'].dataset.state = 'loading';
    ui['service-status-text'].textContent = '正在连接本地服务';
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/local-console/session', { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      if (response.status === 401) { window.location.assign('/login'); return; }
      if (!response.ok) throw new Error(await errorMessage(response));
      const data = await response.json();
      byId('current-user').textContent = data.username || 'admin';
      byId('token-count').textContent = Number.isInteger(data.token_count) ? String(data.token_count) : '—';
      byId('error-token-count').textContent = Number.isInteger(data.error_token_count) ? String(data.error_token_count) : '—';
      byId('base-url').textContent = data.base_url || 'http://127.0.0.1:5005/v1';
      sessionReady = true;
      ui['service-status'].dataset.state = 'ready';
      ui['service-status-text'].textContent = '本地服务已连接';
      setError();
    } catch (cause) {
      sessionReady = false;
      ui['service-status'].dataset.state = 'error';
      ui['service-status-text'].textContent = '本地服务未连接';
      setError(cause.name === 'AbortError' ? '连接本地服务超时，请点击「刷新状态」重试。' : `无法读取服务状态：${cause.message}。请确认本地服务正在运行。`);
    } finally {
      window.clearTimeout(timeout);
      refreshing = false;
      ui['refresh-session'].disabled = false;
      ui['refresh-session'].setAttribute('aria-busy', 'false');
      ui['refresh-session'].textContent = '刷新状态 ↻';
      updateControls();
    }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scrollToLatest(force = false) {
    const box = ui['chat-messages'];
    if (force || box.scrollHeight - box.scrollTop - box.clientHeight < 110) box.scrollTop = box.scrollHeight;
  }

  function addExchange(prompt, model) {
    ui['empty-state'].hidden = true;
    const exchange = element('article', 'exchange');
    const user = element('div', 'message message-user');
    user.append(element('span', 'message-avatar', '你'));
    const userBody = element('div', 'message-body');
    userBody.append(element('div', 'message-author', '你'), element('div', 'message-content', prompt));
    user.append(userBody);
    const assistant = element('div', 'message message-assistant');
    assistant.append(element('span', 'message-avatar', 'C2'));
    const assistantBody = element('div', 'message-body');
    const author = element('div', 'message-author', 'Chat2API');
    author.append(element('span', 'message-meta', model));
    const content = element('div', 'message-content', '等待上游响应…');
    content.dataset.pending = 'true';
    const state = element('p', 'message-state');
    state.hidden = true;
    const tools = element('div', 'message-tools');
    const copy = element('button', 'button button-quiet copy-button', '复制回复');
    copy.type = 'button';
    copy.disabled = true;
    const details = element('details', 'raw-response');
    details.append(element('summary', '', '查看原始响应'));
    const raw = element('pre', '', '等待响应…');
    raw.tabIndex = 0;
    const rawContent = element('div', 'raw-response-content');
    const rawToolbar = element('div', 'raw-response-toolbar');
    const copyRaw = element('button', 'button button-quiet raw-copy-button', '复制原始数据');
    copyRaw.type = 'button';
    copyRaw.disabled = true;
    copyRaw.setAttribute('aria-label', '复制显示的原始响应');
    rawToolbar.append(element('span', '', '响应数据'), copyRaw);
    rawContent.append(rawToolbar, raw);
    details.append(rawContent);
    tools.append(copy, details);
    assistantBody.append(author, content, state, tools);
    assistant.append(assistantBody);
    exchange.append(user, assistant);
    ui['chat-messages'].append(exchange);
    scrollToLatest(true);
    return { exchange, content, state, copy, raw, copyRaw };
  }

  function saveRaw(request, value) {
    if (request.rawChars >= MAX_RAW_CHARS) return;
    const rendered = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    const remaining = MAX_RAW_CHARS - request.rawChars;
    const entry = rendered.slice(0, remaining);
    request.rawEntries = [...request.rawEntries, entry];
    request.rawChars += entry.length;
    request.view.copyRaw.disabled = false;
    request.view.raw.textContent = request.rawEntries.join('\n\n') + (rendered.length > remaining ? '\n\n[原始响应较长，已截断显示]' : '');
  }

  function writeContent(request, text) {
    if (!text || active?.id !== request.id || request.controller.signal.aborted) return;
    const pinned = ui['chat-messages'].scrollHeight - ui['chat-messages'].scrollTop - ui['chat-messages'].clientHeight < 110;
    if (request.firstTokenAt === null) {
      request.firstTokenAt = performance.now();
      ui['first-token-time'].textContent = `首字 ${duration(request.firstTokenAt - request.startedAt)}`;
      setRequestState('正在接收', 'loading');
      updateControls();
    }
    request.answer += text;
    request.view.content.textContent = request.answer;
    request.view.content.dataset.pending = 'false';
    request.view.copy.disabled = false;
    if (pinned) scrollToLatest(true);
  }

  function readTextContent(content) {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) return content.map((part) => typeof part?.text === 'string' ? part.text : '').join('');
    return '';
  }

  function payloadError(data) {
    if (!data || !data.error) return null;
    return typeof data.error === 'string' ? data.error : data.error.message || '上游返回了错误。';
  }

  async function consumeStream(response, request) {
    if (!response.body) throw new Error('当前浏览器无法读取流式响应，请关闭流式响应后重试。');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let dataLines = [];
    let eventName = '';
    let done = false;
    let finishReason = null;

    function dispatch() {
      if (!dataLines.length) { eventName = ''; return; }
      const payload = dataLines.join('\n');
      dataLines = [];
      const currentEvent = eventName;
      eventName = '';
      if (payload.trim() === '[DONE]') { saveRaw(request, '[DONE]'); done = true; return; }
      let data;
      try { data = JSON.parse(payload); } catch (_) { saveRaw(request, payload); throw new Error('收到无法解析的流式数据，请展开原始响应查看详情。'); }
      saveRaw(request, data);
      const failure = payloadError(data);
      if (failure || currentEvent === 'error') throw new Error(failure || data.message || data.detail || '上游流式响应发生错误。');
      const choice = Array.isArray(data.choices) ? data.choices[0] : null;
      if (choice?.finish_reason) finishReason = choice.finish_reason;
      writeContent(request, readTextContent(choice?.delta?.content ?? choice?.message?.content ?? choice?.text));
    }

    function line(value) {
      if (value === '') { dispatch(); return; }
      if (value.startsWith(':')) return;
      const colon = value.indexOf(':');
      const field = colon < 0 ? value : value.slice(0, colon);
      let content = colon < 0 ? '' : value.slice(colon + 1);
      if (content.startsWith(' ')) content = content.slice(1);
      if (field === 'data') dataLines = [...dataLines, content];
      else if (field === 'event') eventName = content;
    }

    function drain(final = false) {
      while (!done) {
        const index = buffer.search(/[\r\n]/);
        if (index < 0) break;
        if (buffer[index] === '\r' && index === buffer.length - 1 && !final) break;
        const length = buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1;
        const value = buffer.slice(0, index);
        buffer = buffer.slice(index + length);
        line(value);
      }
      if (final && !done) {
        if (buffer) { line(buffer); buffer = ''; }
        dispatch();
      }
    }

    try {
      while (!done) {
        const chunk = await reader.read();
        if (chunk.done) { buffer += decoder.decode(); drain(true); break; }
        buffer += decoder.decode(chunk.value, { stream: true });
        drain();
      }
      if (!done && !finishReason) throw new Error('响应连接提前结束，未收到完成标记。可以重新发送。');
      return finishReason;
    } finally {
      try { await reader.cancel(); } catch (_) { /* Abort may already have closed the stream. */ }
      reader.releaseLock();
    }
  }

  function httpUrl(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch (_) { return null; }
  }

  function showImageLinks(request) {
    const links = [...request.answer.matchAll(/!\[(?:image|图片)[^\]]*\]\((https?:\/\/[^\s)]+)\)/gi)].map((match) => httpUrl(match[1])).filter(Boolean);
    if (!links.length) return;
    const gallery = element('div', 'response-images');
    [...new Set(links)].slice(0, 4).forEach((url, index) => {
      const figure = element('figure');
      const image = element('img'); image.src = url; image.alt = `上游返回的图片 ${index + 1}`; image.loading = 'lazy'; image.referrerPolicy = 'no-referrer';
      const caption = element('figcaption');
      const link = element('a', '', `打开图片 ${index + 1} ↗`); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      caption.append(link);
      image.addEventListener('error', () => { image.hidden = true; caption.append(element('span', 'field-hint', '图片预览未加载；链接可能已过期。')); });
      figure.append(image, caption); gallery.append(figure);
    });
    request.view.content.after(gallery);
  }

  function selectMode() {
    const image = ui['request-mode'].value === 'image';
    if (image && selectedMode !== 'image') {
      previousChatModel = ui.model.value.trim() || 'gpt-6-luna';
      ui.model.value = 'auto';
    } else if (!image && selectedMode === 'image') {
      ui.model.value = previousChatModel;
    }
    selectedMode = image ? 'image' : 'text';
    ui['image-mode-note'].hidden = !image;
    if (image) { ui['history-mode'].value = 'false'; ui['advanced-options'].open = true; }
    ui.prompt.placeholder = image ? '描述要生成的画面，例如：请生成一张蓝色圆形图案…' : '输入消息，开始测试…';
    updateConfigurationSummary(); updateControls();
  }

  async function sendMessage(quick = false) {
    if (active || !sessionReady) return;
    const prompt = quick ? QUICK_PROMPT : ui.prompt.value.trim();
    const model = ui.model.value.trim();
    const maxTokens = Number(ui['max-tokens'].value);
    if (!prompt) { ui.prompt.focus(); return; }
    if (!model) { invalidConfiguration(ui.model, '请输入聊天模型名称，例如 gpt-6-luna 或 auto。'); return; }
    if (['gpt-image-2', 'chatgpt-image-latest'].includes(model)) { invalidConfiguration(ui.model, '该图片模型使用 /v1/images/generations，不能发送到聊天接口。请到 API 接入页查看图片生成示例，或改用 gpt-6-luna / auto 测试对话。'); return; }
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 4096) { invalidConfiguration(ui['max-tokens'], '最大输出 Token 需要是 1–4096 的整数。'); return; }
    const imageUrl = ui['image-url'].value.trim();
    if (imageUrl && !httpUrl(imageUrl)) { invalidConfiguration(ui['image-url'], '图片地址需要是有效的 HTTP(S) URL，不能包含用户名或密码。'); return; }
    if (ui['request-mode'].value === 'image' && ui['history-mode'].value !== 'false') { invalidConfiguration(ui['history-mode'], '对话生图请明确选择「普通会话」，上游可能保留此会话。'); return; }
    if (quick && !ui.prompt.value.trim()) ui.prompt.value = QUICK_PROMPT;
    const originalInput = ui.prompt.value;
    const stream = ui.stream.checked;
    const content = imageUrl ? [{ type: 'text', text: prompt }, { type: 'image_url', image_url: { url: httpUrl(imageUrl), detail: 'auto' } }] : prompt;
    const currentMessage = { role: 'user', content };
    const system = ui['system-prompt'].value.trim();
    const messages = [...(system ? [{ role: 'system', content: system }] : []), ...history, currentMessage];
    const payload = { model, messages, stream, max_tokens: maxTokens, quota_feature: ui['request-mode'].value === 'image' ? 'image_gen' : 'auto' };
    if (ui['history-mode'].value !== 'default') payload.history_disabled = ui['history-mode'].value === 'true';
    const request = {
      id: ++requestSequence,
      controller: new AbortController(),
      startedAt: performance.now(),
      firstTokenAt: null,
      answer: '',
      rawEntries: [],
      rawChars: 0,
      view: addExchange(`${prompt}${imageUrl ? '\n[附带图片 URL]' : ''}`, model),
    };
    active = request;
    setError();
    setRequestState('等待上游', 'loading');
    ui['request-http'].textContent = 'HTTP —';
    ui['first-token-time'].textContent = '首字 —';
    const updateTime = () => { if (active?.id === request.id) ui['request-time'].textContent = duration(performance.now() - request.startedAt); };
    updateTime();
    const timer = window.setInterval(updateTime, 100);
    // A timeout is separate from a user stop so the two outcomes remain clear.
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; request.controller.abort(); }, 300000);
    updateControls();
    request.view.copy.addEventListener('click', async () => {
      const copied = await window.ConsoleUI.copyText(request.answer, request.view.copy);
      if (!copied) setError('自动复制失败，请选中回复文字后手动复制。');
    });
    request.view.copyRaw.addEventListener('click', async () => {
      const copied = await window.ConsoleUI.copyText(request.view.raw.textContent, request.view.copyRaw);
      if (!copied) setError('自动复制失败，请展开原始响应后手动复制。');
    });
    try {
      const response = await fetch('/local-console/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: stream ? 'text/event-stream' : 'application/json' },
        body: JSON.stringify(payload),
        signal: request.controller.signal,
      });
      if (active?.id !== request.id) return;
      ui['request-http'].textContent = `HTTP ${response.status}`;
      if (response.status === 401) { window.location.assign('/login'); return; }
      if (!response.ok) {
        const message = await errorMessage(response);
        saveRaw(request, { status: response.status, error: message });
        throw new Error(message);
      }
      let finishReason;
      if ((response.headers.get('content-type') || '').includes('text/event-stream')) {
        finishReason = await consumeStream(response, request);
      } else {
        const raw = await response.text();
        let data;
        try { data = JSON.parse(raw); } catch (_) { saveRaw(request, raw); throw new Error('接口没有返回可解析的 JSON，请查看原始响应。'); }
        saveRaw(request, data);
        const failure = payloadError(data);
        if (failure) throw new Error(failure);
        const choice = Array.isArray(data.choices) ? data.choices[0] : null;
        finishReason = choice?.finish_reason;
        writeContent(request, readTextContent(choice?.message?.content ?? choice?.text));
      }
      if (request.controller.signal.aborted) throw new DOMException('Request aborted', 'AbortError');
      if (active?.id !== request.id) return;
      if (!request.answer.trim()) throw new Error('请求已返回，但没有可显示的文本内容。请查看原始响应。');
      history = [...history, currentMessage, { role: 'assistant', content: request.answer }];
      showImageLinks(request);
      if (ui['image-url'].value.trim() === imageUrl) ui['image-url'].value = '';
      ui['turn-count'].textContent = `${history.length / 2} 轮对话`;
      setRequestState('请求成功', 'success');
      request.view.state.textContent = finishReason === 'length' ? '已完成 · 达到输出长度限制，可增加 Token 后重试' : '已完成 · 已加入本次会话上下文';
      request.view.state.hidden = false;
      if (ui.prompt.value === originalInput && (!quick || originalInput === QUICK_PROMPT)) ui.prompt.value = '';
    } catch (cause) {
      if (active?.id !== request.id) return;
      const stopped = cause.name === 'AbortError' || request.controller.signal.aborted;
      const label = timedOut ? '请求超时' : stopped ? '已停止接收' : '请求失败';
      const state = stopped && !timedOut ? 'stopped' : 'error';
      setRequestState(label, state);
      request.view.state.dataset.state = state;
      request.view.state.textContent = `${label} · 本轮未加入上下文`;
      request.view.state.hidden = false;
      if (!request.answer) {
        request.view.content.textContent = timedOut ? '等待超过 5 分钟，请稍后重试。' : stopped ? '已停止等待回复。' : '本次请求未完成。';
        request.view.content.dataset.pending = 'false';
      }
      if (!stopped || timedOut) {
        const message = cause instanceof TypeError ? '连接已中断，请确认本地服务正在运行。' : cause.message;
        setError(timedOut ? '本次请求等待超过 5 分钟，已停止接收。输入已保留，可重试。' : `${message} 输入已保留，可重试。`);
      }
      if (!request.rawEntries.length) request.view.raw.textContent = stopped ? '请求已停止，尚未收到响应数据。' : `未收到有效响应。\n${cause.message}`;
    } finally {
      window.clearInterval(timer);
      window.clearTimeout(timeout);
      if (active?.id === request.id) {
        ui['request-time'].textContent = duration(performance.now() - request.startedAt);
        active = null;
        updateControls();
        ui.prompt.focus({ preventScroll: true });
      }
    }
  }

  ui['chat-form'].addEventListener('submit', (event) => { event.preventDefault(); void sendMessage(); });
  ui.prompt.addEventListener('input', updateControls);
  ui.prompt.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      void sendMessage();
    }
  });
  for (const name of ['model', 'max-tokens', 'image-url', 'system-prompt']) {
    ui[name].addEventListener('input', () => {
      ui[name].removeAttribute('aria-invalid');
      updateConfigurationSummary();
    });
  }
  ui.stream.addEventListener('change', updateConfigurationSummary);
  ui['request-mode'].addEventListener('change', selectMode);
  for (const mode of ['text', 'image']) {
    const button = ui[`mode-${mode}`];
    button.addEventListener('click', () => {
      if (active) return;
      ui['request-mode'].value = mode;
      selectMode();
    });
    button.addEventListener('keydown', (event) => {
      if (active || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 'text' : event.key === 'End' ? 'image' : mode === 'text' ? 'image' : 'text';
      ui['request-mode'].value = next;
      selectMode();
      ui[`mode-${next}`].focus();
    });
  }
  for (const mode of ['text', 'image']) {
    ui[`fill-${mode}-example`].addEventListener('click', () => {
      if (active) return;
      ui['request-mode'].value = mode;
      ui.prompt.value = mode === 'image' ? '请生成一张白色背景上的蓝色圆形图案。' : QUICK_PROMPT;
      selectMode();
      ui.prompt.focus();
    });
  }
  ui['history-mode'].addEventListener('change', () => ui['history-mode'].removeAttribute('aria-invalid'));
  ui['copy-endpoint'].addEventListener('click', async () => {
    await window.ConsoleUI.copyText(byId('base-url').textContent, ui['copy-endpoint']);
  });
  ui['quick-test'].addEventListener('click', () => {
    if (ui['request-mode'].value === 'image') { ui.prompt.value = '请生成一张白色背景上的蓝色圆形图案。'; updateControls(); ui.prompt.focus(); return; }
    void sendMessage(true);
  });
  ui['refresh-session'].addEventListener('click', () => { void refreshSession(); });
  ui['stop-button'].addEventListener('click', () => {
    if (!active) return;
    ui['stop-button'].disabled = true;
    active.controller.abort();
    ui['stop-button'].disabled = false;
  });
  function clearConversation() {
    if (active) return;
    history = [];
    ui['chat-messages'].querySelectorAll('.exchange').forEach((node) => node.remove());
    ui['empty-state'].hidden = false;
    ui['turn-count'].textContent = '新会话';
    setRequestState('等待发送', 'idle');
    ui['request-time'].textContent = '—';
    ui['request-http'].textContent = 'HTTP —';
    ui['first-token-time'].textContent = '首字 —';
    setError();
    updateControls();
    ui.prompt.focus();
    window.ConsoleUI.toast('当前页面的对话已清空。');
  }
  ui['clear-button'].addEventListener('click', () => {
    if (active) return;
    if (typeof ui['chat-clear-dialog'].showModal === 'function') {
      ui['chat-clear-dialog'].showModal();
      ui['chat-clear-cancel'].focus();
    } else if (window.confirm('清空当前页面的对话记录？此操作不会删除平台账号或上游聊天记录。')) {
      clearConversation();
    }
  });
  ui['chat-clear-cancel'].addEventListener('click', () => ui['chat-clear-dialog'].close());
  ui['chat-clear-dialog'].addEventListener('close', () => ui['clear-button'].focus());
  ui['chat-clear-form'].addEventListener('submit', (event) => {
    event.preventDefault();
    ui['chat-clear-dialog'].close();
    clearConversation();
  });
  ui['logout-button'].addEventListener('click', async () => {
    ui['logout-button'].disabled = true;
    try {
      const response = await fetch('/local-console/logout', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!response.ok && response.status !== 401) throw new Error(await errorMessage(response));
      active?.controller.abort();
      window.location.assign('/login');
    } catch (cause) {
      setError(`退出失败：${cause.message}。请重试。`);
      ui['logout-button'].disabled = false;
    }
  });
  if (new URLSearchParams(window.location.search).get('mode') === 'image') ui['request-mode'].value = 'image';
  selectMode();
  void refreshSession();
})();
