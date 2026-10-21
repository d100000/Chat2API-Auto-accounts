(() => {
  'use strict';

  let toastTimer;
  let currentToast;
  let toastOrigin;
  const copyTimers = new WeakMap();

  function scheduleDismiss() {
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      if (!currentToast.matches(':hover, :focus-within')) currentToast.hidden = true;
    }, 5000);
  }

  function toast(message, {tone = 'info'} = {}) {
    if (!currentToast) {
      currentToast = document.createElement('div');
      currentToast.className = 'ui-toast';
      currentToast.setAttribute('role', 'status');
      currentToast.setAttribute('aria-live', 'polite');
      currentToast.setAttribute('aria-atomic', 'true');
      document.body.append(currentToast);
      currentToast.addEventListener('pointerenter', () => clearTimeout(toastTimer));
      currentToast.addEventListener('focusin', () => clearTimeout(toastTimer));
      currentToast.addEventListener('pointerleave', scheduleDismiss);
      currentToast.addEventListener('focusout', scheduleDismiss);
    }
    clearTimeout(toastTimer);
    const hadFocus = currentToast.contains(document.activeElement);
    if (!hadFocus) toastOrigin = document.activeElement;
    currentToast.dataset.tone = ['success', 'error', 'info'].includes(tone) ? tone : 'info';
    const content = document.createElement('span');
    content.textContent = String(message);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.textContent = '×';
    close.setAttribute('aria-label', '关闭提示');
    close.addEventListener('click', () => {
      if (document.activeElement === close && toastOrigin?.isConnected) toastOrigin.focus();
      currentToast.hidden = true;
    });
    currentToast.replaceChildren(content, close);
    currentToast.hidden = false;
    if (hadFocus) close.focus({preventScroll: true});
    scheduleDismiss();
  }

  async function copyText(text, button) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(String(text));
      if (button) {
        const previous = copyTimers.get(button);
        if (previous) clearTimeout(previous.timer);
        const label = button.querySelector('[data-copy-label]') || button;
        const original = previous?.original || Array.from(label.childNodes);
        label.textContent = '已复制';
        button.dataset.copyState = 'success';
        const timer = setTimeout(() => {
          label.replaceChildren(...original);
          delete button.dataset.copyState;
          copyTimers.delete(button);
        }, 1800);
        copyTimers.set(button, {timer, original});
      }
      toast('已复制到剪贴板', {tone: 'success'});
      return true;
    } catch (_) {
      toast('未能访问剪贴板，请选中文字后手动复制。', {tone: 'error'});
      return false;
    }
  }

  window.ConsoleUI = Object.freeze({toast, copyText});
})();
