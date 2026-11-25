(() => {
  const key = 'solbreaker-benchmarks-theme';
  const root = document.documentElement;
  try {
    if (localStorage.getItem(key) === 'dark') root.dataset.theme = 'dark';
  } catch {
    // The switch still works for this page when storage is blocked.
  }

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('dark-theme');
    button.setAttribute('aria-pressed', String(root.dataset.theme === 'dark'));
    button.hidden = false;
    button.addEventListener('click', () => {
      const dark = root.dataset.theme !== 'dark';
      root.dataset.theme = dark ? 'dark' : 'light';
      button.setAttribute('aria-pressed', String(dark));
      try {
        localStorage.setItem(key, root.dataset.theme);
      } catch {
        // Keep the selection on the current page.
      }
    });
  });
})();
