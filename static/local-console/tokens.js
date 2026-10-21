(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  const form = byId('uploadForm');
  const input = byId('token-input');
  const upload = byId('token-upload');
  const clear = byId('token-clear');
  const errorButton = byId('errorButton');
  const feedback = byId('tokens-feedback');
  const error = byId('tokens-error');
  const dialog = byId('clear-dialog');
  const confirm = byId('clear-confirm');
  const cancel = byId('clear-cancel');
  const reveal = byId('error-token-reveal');
  const copy = byId('error-token-copy');
  const seedClear = byId('seed-clear');
  const seedDialog = byId('seed-dialog');
  const seedCancel = byId('seed-cancel');
  const seedConfirm = byId('seed-confirm');
  const prefix = document.body.dataset.apiPrefix || '';
  const base = prefix ? `/${prefix}/tokens` : '/tokens';
  let state = { pending: false, action: null, errors: [], revealed: false, sessionExpired: false };

  function setPending(pending, action = null) {
    state = { ...state, pending, action };
    form.setAttribute('aria-busy', String(pending && action === 'upload'));
    dialog.setAttribute('aria-busy', String(pending && action === 'clear'));
    for (const control of [input, upload, clear, errorButton, reveal, copy, confirm, cancel, seedClear, seedCancel, seedConfirm, byId('tokens-logout')]) {
      control.disabled = pending || state.sessionExpired;
    }
    upload.textContent = pending && action === 'upload' ? '正在导入…' : '导入凭据 ↑';
    confirm.textContent = pending && action === 'clear' ? '正在清空…' : '确认清空';
    seedConfirm.textContent = pending && action === 'seed' ? '正在清空…' : '确认清空映射';
    seedDialog.setAttribute('aria-busy', String(pending && action === 'seed'));
    errorButton.textContent = pending && action === 'errors' ? '正在读取…' : byId('error-tokens-section').hidden ? '查看错误凭据' : '刷新错误列表';
  }

  function clearNotices() {
    error.hidden = true;
    feedback.hidden = true;
    byId('clear-dialog-error').hidden = true;
    byId('seed-dialog-error').hidden = true;
  }

  function showError(cause, target = error) {
    target.textContent = cause.name === 'AbortError'
      ? '本地服务响应超时，请确认操作结果后再试。'
      : cause instanceof TypeError ? '无法连接本地服务，请确认服务正在运行后重试。' : cause.message;
    target.hidden = false;
  }

  function showSuccess(message) {
    feedback.textContent = message;
    feedback.hidden = false;
    window.ConsoleUI?.toast(message, { tone: 'success' });
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path, {
        ...options, method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { Accept: 'application/json', ...(options.headers || {}) },
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        state = { ...state, sessionExpired: true, errors: [], revealed: false };
        renderErrors();
        byId('tokens-session-expired').hidden = false;
      }
      if (!response.ok) {
        const messages = {
          401: '登录已过期，请重新登录后继续。',
          403: '请求来源校验失败，请刷新本地页面后重试。',
          413: '本次导入内容过多，请分批导入。',
          429: '操作过于频繁，请稍后再试。',
        };
        // Server error bodies may contain submitted credentials; never render them.
        throw new Error(messages[response.status] || `操作未完成（HTTP ${response.status}），请稍后重试。`);
      }
      return data;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function updateLineCount() {
    const count = input.value.split(/\r?\n/).filter((line) => line.trim() && !line.trimStart().startsWith('#')).length;
    byId('token-line-count').textContent = `${count} 条待导入`;
    return count;
  }

  function updateCount(data) {
    if (Number.isSafeInteger(data.tokens_count) && data.tokens_count >= 0) {
      byId('token-count').textContent = String(data.tokens_count);
    }
  }

  function renderErrors() {
    const list = byId('error-token-list');
    const fragment = document.createDocumentFragment();
    state.errors.forEach((token, index) => {
      const item = document.createElement('li');
      const number = document.createElement('span');
      number.className = 'token-list-number';
      number.textContent = String(index + 1).padStart(2, '0');
      const code = document.createElement('code');
      code.textContent = state.revealed ? token : '••••••••••••••••';
      if (!state.revealed) code.setAttribute('aria-label', `第 ${index + 1} 条凭据，内容已隐藏`);
      item.append(number, code);
      fragment.append(item);
    });
    list.replaceChildren(fragment);
    byId('error-token-count').textContent = `${state.errors.length} 条`;
    byId('error-tokens-empty').hidden = state.errors.length > 0;
    byId('error-token-actions').hidden = state.errors.length === 0;
    list.hidden = state.errors.length === 0;
    reveal.textContent = state.revealed ? '隐藏凭据' : '显示凭据';
    reveal.setAttribute('aria-pressed', String(state.revealed));
  }

  input.addEventListener('input', updateLineCount);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.pending || state.sessionExpired || !form.reportValidity()) return;
    clearNotices();
    const lines = input.value.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith('#'));
    if (!lines.length) {
      showError(new Error('请输入至少一条凭据，每行一条。'));
      input.focus();
      return;
    }
    setPending(true, 'upload');
    try {
      const data = await request(`${base}/upload`, { body: new URLSearchParams({ text: lines.join('\n') }) });
      if (data.status !== 'success') throw new Error('服务未确认导入成功，输入内容已保留，请检查后重试。');
      updateCount(data);
      input.value = '';
      updateLineCount();
      showSuccess('凭据已导入，可前往账号额度页检查状态。');
    } catch (cause) {
      showError(cause);
    } finally {
      setPending(false);
    }
  });

  errorButton.addEventListener('click', async () => {
    if (state.pending || state.sessionExpired) return;
    clearNotices();
    setPending(true, 'errors');
    try {
      const data = await request(`${base}/error`);
      if (!Array.isArray(data.error_tokens) || !data.error_tokens.every((token) => typeof token === 'string')) {
        throw new Error('错误凭据列表格式异常，请稍后重试。');
      }
      state = { ...state, errors: [...data.error_tokens], revealed: false };
      renderErrors();
      byId('error-tokens-section').hidden = false;
      errorButton.setAttribute('aria-expanded', 'true');
      showSuccess(state.errors.length ? `已读取 ${state.errors.length} 条错误凭据。` : '没有记录到错误凭据。');
    } catch (cause) {
      showError(cause);
    } finally {
      setPending(false);
    }
  });

  reveal.addEventListener('click', () => {
    state = { ...state, revealed: !state.revealed };
    renderErrors();
  });

  copy.addEventListener('click', async () => {
    if (!state.errors.length || state.pending) return;
    const copied = await window.ConsoleUI?.copyText(state.errors.join('\n'), copy);
    if (!copied) showError(new Error('复制未完成，请点击“显示凭据”后手动选择复制。'));
  });

  clear.addEventListener('click', () => {
    if (state.pending || state.sessionExpired) return;
    clearNotices();
    dialog.showModal();
    cancel.focus();
  });

  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', (event) => {
    if (state.pending) event.preventDefault();
  });
  dialog.addEventListener('close', () => clear.focus());

  byId('clearForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.pending || state.sessionExpired) return;
    clearNotices();
    setPending(true, 'clear');
    try {
      const data = await request(`${base}/clear`);
      if (data.status !== 'success') throw new Error('服务未确认清空成功，请检查账号池状态后再试。');
      updateCount(data);
      state = { ...state, errors: [], revealed: false };
      renderErrors();
      input.value = '';
      updateLineCount();
      dialog.close();
      showSuccess('全部凭据及错误记录已清空。重新导入凭据后可恢复账号池。');
    } catch (cause) {
      if (state.sessionExpired) dialog.close();
      showError(cause, state.sessionExpired ? error : byId('clear-dialog-error'));
    } finally {
      setPending(false);
      if (!dialog.open && !state.sessionExpired) clear.focus();
    }
  });

  byId('tokens-logout').addEventListener('click', async () => {
    if (state.pending) return;
    clearNotices();
    setPending(true, 'logout');
    try {
      await request('/local-console/logout', { headers: { 'Content-Type': 'application/json' }, body: '{}' });
      input.value = '';
      state = { ...state, errors: [], revealed: false };
      renderErrors();
      window.location.assign('/login');
    } catch (cause) {
      showError(cause);
      setPending(false);
    }
  });

  seedClear.addEventListener('click', () => {
    if (state.pending || state.sessionExpired) return;
    clearNotices();
    seedDialog.showModal();
    seedCancel.focus();
  });
  seedCancel.addEventListener('click', () => seedDialog.close());
  seedDialog.addEventListener('cancel', (event) => {
    if (state.pending) event.preventDefault();
  });
  seedDialog.addEventListener('close', () => seedClear.focus());
  byId('seedForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.pending || state.sessionExpired) return;
    clearNotices();
    setPending(true, 'seed');
    try {
      const data = await request(`${prefix ? `/${prefix}` : ''}/seed_tokens/clear`);
      if (data.status !== 'success') throw new Error('服务未确认清空结果，请检查后再试。');
      seedDialog.close();
      showSuccess('Seed 与会话映射已清空，账号凭据已保留。');
    } catch (cause) {
      if (state.sessionExpired) seedDialog.close();
      showError(cause, state.sessionExpired ? error : byId('seed-dialog-error'));
    } finally {
      setPending(false);
    }
  });
})();
