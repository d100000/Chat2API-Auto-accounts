(() => {
  'use strict';
  const form = document.getElementById('login-form');
  const button = document.getElementById('login-submit');
  const error = document.getElementById('login-error');
  const password = document.getElementById('password');
  const passwordToggle = document.getElementById('password-toggle');
  let pending = false;

  passwordToggle.addEventListener('click', () => {
    const visible = password.type === 'password';
    password.type = visible ? 'text' : 'password';
    passwordToggle.textContent = visible ? '隐藏' : '显示';
    passwordToggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
    passwordToggle.setAttribute('aria-pressed', String(visible));
  });

  form.addEventListener('input', () => {
    if (!error.hidden) error.hidden = true;
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending || !form.reportValidity()) return;
    pending = true;
    form.setAttribute('aria-busy', 'true');
    button.disabled = true;
    passwordToggle.disabled = true;
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
          password: password.value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        const fallback = response.status === 429 ? '尝试次数较多，请稍后再试。' : '登录失败，请检查用户名和密码。';
        throw new Error(typeof data.detail === 'string' ? data.detail : fallback);
      }
      password.value = '';
      password.type = 'password';
      button.textContent = '登录成功，正在进入…';
      const next = new URLSearchParams(window.location.search).get('next');
      window.location.assign(next === '/dashboard' ? '/dashboard' : '/playground');
    } catch (cause) {
      error.textContent = cause.name === 'AbortError' ? '本地服务响应超时，请稍后重试。' : cause instanceof TypeError ? '无法连接本地服务，请确认服务正在运行。' : cause.message;
      error.hidden = false;
      pending = false;
      button.disabled = false;
      passwordToggle.disabled = false;
      button.textContent = '登录工作区 →';
    } finally {
      window.clearTimeout(timeout);
      form.setAttribute('aria-busy', 'false');
    }
  });
})();
