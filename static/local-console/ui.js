(() => {
  'use strict';

  let toastTimer;
  let currentToast;
  let toastOrigin;
  const copyTimers = new WeakMap();
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function setupTheme() {
    const root = document.documentElement;
    const controls = document.querySelectorAll('[data-theme-toggle]');
    function apply(theme, persist = false) {
      root.dataset.theme = theme === 'dark' ? 'dark' : 'light';
      for (const control of controls) {
        const label = root.dataset.theme === 'dark' ? '切换为浅色模式' : '切换为深色模式';
        control.setAttribute('aria-label', label);
        control.setAttribute('aria-pressed', String(root.dataset.theme === 'dark'));
        control.title = label;
      }
      if (persist) {
        try { localStorage.setItem('chat2api-theme', root.dataset.theme); } catch (_) { /* Preference storage is optional. */ }
      }
    }
    apply(root.dataset.theme);
    for (const control of controls) control.addEventListener('click', () => apply(root.dataset.theme === 'dark' ? 'light' : 'dark', true));
    window.addEventListener('storage', (event) => {
      if (event.key === 'chat2api-theme') apply(event.newValue);
    });
  }

  function setupNavigation() {
    const sidebar = document.getElementById('workspace-sidebar');
    const toggle = document.getElementById('workspace-menu-toggle');
    const close = document.getElementById('workspace-menu-close');
    const backdrop = document.getElementById('workspace-menu-backdrop');
    if (!sidebar || !toggle || !close || !backdrop) return;
    const mobile = window.matchMedia('(max-width: 760px)');
    const content = [document.querySelector('.workspace'), document.querySelector('.topbar')].filter(Boolean);
    let open = false;

    function setOpen(value, returnFocus = false) {
      open = mobile.matches && value;
      if (open) document.body.dataset.navOpen = '';
      else delete document.body.dataset.navOpen;
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? '关闭导航' : '打开导航');
      backdrop.hidden = !open;
      sidebar.inert = mobile.matches && !open;
      sidebar.setAttribute('aria-hidden', String(mobile.matches && !open));
      for (const element of content) element.inert = open;
      if (open) {
        sidebar.scrollTop = 0;
        sidebar.setAttribute('role', 'dialog');
        sidebar.setAttribute('aria-modal', 'true');
        close.focus({preventScroll: true});
      } else {
        sidebar.removeAttribute('role');
        sidebar.removeAttribute('aria-modal');
        if (returnFocus && mobile.matches) toggle.focus({preventScroll: true});
      }
    }
    toggle.addEventListener('click', () => setOpen(!open, open));
    close.addEventListener('click', () => setOpen(false, true));
    backdrop.addEventListener('click', () => setOpen(false, true));
    sidebar.addEventListener('click', (event) => {
      if (event.target.closest('a[href]') && mobile.matches) setOpen(false);
    });
    document.addEventListener('keydown', (event) => {
      if (!open) return;
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false, true); return; }
      if (event.key !== 'Tab') return;
      const focusable = [...sidebar.querySelectorAll('a[href], button:not([disabled])')].filter((element) => element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
        event.preventDefault(); first?.focus();
      }
    });
    mobile.addEventListener('change', () => setOpen(false));
    setOpen(false);
  }

  const tabMotion = new WeakMap();
  function setupMotionTabs(scope = document) {
    const groups = [...scope.querySelectorAll('[data-motion-tabs]')];
    if (scope.matches?.('[data-motion-tabs]')) groups.unshift(scope);
    for (const group of groups) {
      if (tabMotion.has(group)) continue;
      const indicator = document.createElement('span');
      indicator.className = 'motion-tab-indicator';
      indicator.setAttribute('aria-hidden', 'true');
      indicator.dataset.initial = '';
      group.append(indicator);
      group.classList.add('motion-tabs-ready');
      let frame = null;
      const update = () => {
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          const selected = group.querySelector('[role="tab"][aria-selected="true"], button[aria-pressed="true"]');
          const box = selected?.getBoundingClientRect();
          if (!box?.width || !box.height) { indicator.hidden = true; return; }
          indicator.hidden = false;
          const parent = group.getBoundingClientRect();
          indicator.style.width = `${box.width}px`;
          indicator.style.height = `${box.height}px`;
          indicator.style.transform = `translate(${box.left - parent.left + group.scrollLeft}px, ${box.top - parent.top + group.scrollTop}px)`;
          if ('initial' in indicator.dataset) requestAnimationFrame(() => { delete indicator.dataset.initial; });
        });
      };
      const changes = new MutationObserver(update);
      changes.observe(group, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['aria-selected', 'aria-pressed']});
      const size = new ResizeObserver(update);
      size.observe(group);
      tabMotion.set(group, {update, changes, size});
      group.addEventListener('scroll', update, {passive: true});
      update();
    }
  }

  function setupMotion() {
    setupMotionTabs();
    // Dynamic account details use the same primitive, without reanimating rows
    // every time quota data is polled.
    const observer = new MutationObserver((records) => {
      for (const record of records) for (const node of record.addedNodes) {
        if (node.nodeType === 1) setupMotionTabs(node);
      }
    });
    observer.observe(document.body, {childList: true, subtree: true});
    document.addEventListener('toggle', (event) => {
      if (reducedMotion.matches || event.target.tagName !== 'DETAILS' || !event.target.open) return;
      const content = event.target.querySelector('.disclosure-content, .feature-example-body, .raw-response pre');
      content?.animate?.([{opacity: .3, transform: 'translateY(-4px)'}, {opacity: 1, transform: 'translateY(0)'}],
        {duration: 220, easing: 'cubic-bezier(.22,1,.36,1)'});
    }, true);
  }

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
  setupTheme();
  setupNavigation();
  setupMotion();
})();
