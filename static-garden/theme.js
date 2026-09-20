/*! Copyright (c) 2026 Arney Nova. MIT License; see /licenses/MIT.txt. */
/* Applies a saved theme choice before first paint, to avoid a flash of the wrong theme. */
(() => {
  try {
    const saved = localStorage.getItem('theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved);
  } catch (error) {}
})();
