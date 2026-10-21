/* Apply the visual preference before styles paint. Never store credentials. */
(() => {
  'use strict';
  let preference = null;
  try { preference = localStorage.getItem('chat2api-theme'); } catch (_) { /* Browser storage may be disabled. */ }
  document.documentElement.dataset.theme = preference === 'dark' ? 'dark' : 'light';
})();
