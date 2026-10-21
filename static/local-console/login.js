(() => {
  'use strict';
  const form = document.getElementById('login-form');
  const button = document.getElementById('login-submit');
  const error = document.getElementById('login-error');
  let pending = false;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending || !form.reportValidity()) return;
    pending = true;
    button.disabled = true;
    button.textContent = '正在登录…';
    error.hidden = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/local-console/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        credentials: 'same-origin',
        signal: controller.signal,
        body: JSON.stringify({
          username: document.getElementById('username').value.trim(),
          password: document.getElementById('password').value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        const fallback = response.status === 429 ? '尝试次数较多，请稍后再试。' : '登录失败，请检查用户名和密码。';
        throw new Error(typeof data.detail === 'string' ? data.detail : fallback);
      }
      document.getElementById('password').value = '';
      window.location.assign('/playground');
    } catch (cause) {
      error.textContent = cause.name === 'AbortError' ? '本地服务响应超时，请稍后重试。' : cause instanceof TypeError ? '无法连接本地服务，请确认服务正在运行。' : cause.message;
      error.hidden = false;
      pending = false;
      button.disabled = false;
      button.textContent = '登录测试台 →';
    } finally {
      window.clearTimeout(timeout);
    }
  });
})();
