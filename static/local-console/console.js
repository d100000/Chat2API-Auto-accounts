(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const ui = Object.fromEntries([
    'chat-form', 'prompt', 'model', 'max-tokens', 'stream', 'send-button', 'stop-button',
    'clear-button', 'quick-test', 'chat-messages', 'empty-state', 'turn-count', 'page-error',
    'request-state', 'request-time', 'request-http', 'first-token-time', 'service-status',
    'service-status-text', 'refresh-session', 'logout-button',
  ].map((id) => [id, byId(id)]));
  const QUICK_PROMPT = '请只回复 OK。';
  const MAX_RAW_CHARS = 200000;
  let history = [];
  let active = null;
  let sessionReady = false;
  let refreshing = false;
  let requestSequence = 0;

  byId('shortcut-label').textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘ Enter' : 'Ctrl + Enter';

  function setError(message = '') {
    ui['page-error'].textContent = message;
    ui['page-error'].hidden = !message;
  }

  function updateControls() {
    const pending = Boolean(active);
    ui['send-button'].disabled = pending || !sessionReady || !ui.prompt.value.trim();
    ui['send-button'].textContent = pending ? '正在接收…' : '发送消息 ↑';
    ui['quick-test'].disabled = pending || !sessionReady;
    ui['clear-button'].disabled = pending || !ui['chat-messages'].querySelector('.exchange');
    ui['stop-button'].hidden = !pending;
    ui.prompt.readOnly = pending;
    for (const key of ['model', 'max-tokens', 'stream']) ui[key].disabled = pending;
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
    details.append(raw);
    tools.append(copy, details);
    assistantBody.append(author, content, state, tools);
    assistant.append(assistantBody);
    exchange.append(user, assistant);
    ui['chat-messages'].append(exchange);
    scrollToLatest(true);
    return { exchange, content, state, copy, raw };
  }

  function saveRaw(request, value) {
    if (request.rawChars >= MAX_RAW_CHARS) return;
    const rendered = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    const remaining = MAX_RAW_CHARS - request.rawChars;
    const entry = rendered.slice(0, remaining);
    request.rawEntries = [...request.rawEntries, entry];
    request.rawChars += entry.length;
    request.view.raw.textContent = request.rawEntries.join('\n\n') + (rendered.length > remaining ? '\n\n[原始响应较长，已截断显示]' : '');
  }

  function writeContent(request, text) {
    if (!text || active?.id !== request.id || request.controller.signal.aborted) return;
    const pinned = ui['chat-messages'].scrollHeight - ui['chat-messages'].scrollTop - ui['chat-messages'].clientHeight < 110;
    if (request.firstTokenAt === null) {
      request.firstTokenAt = performance.now();
      ui['first-token-time'].textContent = `首字 ${duration(request.firstTokenAt - request.startedAt)}`;
      setRequestState('正在接收', 'loading');
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

  async function sendMessage(quick = false) {
    if (active || !sessionReady) return;
    const prompt = quick ? QUICK_PROMPT : ui.prompt.value.trim();
    const model = ui.model.value.trim();
    const maxTokens = Number(ui['max-tokens'].value);
    if (!prompt) { ui.prompt.focus(); return; }
    if (!model) { setError('请输入模型名称，例如 auto。'); ui.model.focus(); return; }
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 4096) { setError('最大输出 Token 需要是 1–4096 的整数。'); ui['max-tokens'].focus(); return; }
    if (quick && !ui.prompt.value.trim()) ui.prompt.value = QUICK_PROMPT;
    const originalInput = ui.prompt.value;
    const stream = ui.stream.checked;
    const request = {
      id: ++requestSequence,
      controller: new AbortController(),
      startedAt: performance.now(),
      firstTokenAt: null,
      answer: '',
      rawEntries: [],
      rawChars: 0,
      view: addExchange(prompt, model),
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
      try {
        await navigator.clipboard.writeText(request.answer);
        request.view.copy.textContent = '已复制';
        window.setTimeout(() => { request.view.copy.textContent = '复制回复'; }, 1800);
      } catch (_) {
        setError('自动复制失败，请选中回复文字后手动复制。');
      }
    });
    try {
      const response = await fetch('/local-console/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: stream ? 'text/event-stream' : 'application/json' },
        body: JSON.stringify({ model, messages: [...history, { role: 'user', content: prompt }], stream, max_tokens: maxTokens }),
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
      history = [...history, { role: 'user', content: prompt }, { role: 'assistant', content: request.answer }];
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
  ui['quick-test'].addEventListener('click', () => { void sendMessage(true); });
  ui['refresh-session'].addEventListener('click', () => { void refreshSession(); });
  ui['stop-button'].addEventListener('click', () => {
    if (!active) return;
    ui['stop-button'].disabled = true;
    active.controller.abort();
    ui['stop-button'].disabled = false;
  });
  ui['clear-button'].addEventListener('click', () => {
    if (active || !window.confirm('清空当前页面的对话记录？此操作不会删除平台账号或上游聊天记录。')) return;
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
  updateControls();
  void refreshSession();
})();
