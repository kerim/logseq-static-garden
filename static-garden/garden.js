/*! Copyright (c) 2026 Arney Nova. MIT License; see /licenses/MIT.txt. */
/* Optional enhancements. All page content and navigation are ordinary HTML. */
(() => {
  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    const icon = themeToggle.querySelector('.theme-toggle-icon');
    const label = themeToggle.querySelector('.theme-toggle-label');
    const isDark = () => (document.documentElement.getAttribute('data-theme')
      ? document.documentElement.getAttribute('data-theme') === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches);
    const render = () => {
      const dark = isDark();
      themeToggle.setAttribute('aria-pressed', String(dark));
      themeToggle.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
      if (icon) icon.textContent = dark ? '☀︎' : '☾';
      if (label) label.textContent = dark ? 'Light' : 'Dark';
    };
    render();
    themeToggle.addEventListener('click', () => {
      const next = isDark() ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (error) {}
      render();
    });
  }

  const explore = document.querySelector('.mobile-explore');
  if (explore) {
    document.addEventListener('click', event => {
      if (!explore.contains(event.target) || event.target.closest('a')) explore.open = false;
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && explore.open) {
        explore.open = false;
        explore.querySelector('summary').focus();
      }
    });
  }
  const openAnchor = () => {
    if (!location.hash.startsWith('#block-')) return;
    const target = document.getElementById(location.hash.slice(1));
    if (!target) return;
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    target.scrollIntoView({block: 'center'});
  };
  openAnchor();
  const legacy = async () => {
    if (/^#\/graph\/?$/.test(location.hash)) { location.replace('/graph/'); return; }
    const match = location.hash.match(/^#\/(?:page|page-block)\/(.+)$/);
    if (!match) return;
    try {
      const response = await fetch('/site/routes.json');
      if (!response.ok) throw new Error('Route index unavailable');
      const routes = await response.json();
      const name = decodeURIComponent(match[1]);
      const url = routes[name] || routes[name.toLowerCase()];
      location.replace(url || '/pages/?q=' + encodeURIComponent(name));
    } catch (error) {
      console.warn('Could not resolve the old garden link.', error);
    }
  };
  legacy();
  window.addEventListener('hashchange', () => { openAnchor(); legacy(); });

  const input = document.getElementById('search');
  if (!input) return;
  const results = document.getElementById('search-results');
  const status = document.getElementById('search-status');
  const original = results.cloneNode(true);
  let dataPromise;
  let generation = 0;
  const search = async () => {
    const current = ++generation;
    const query = input.value.trim().toLocaleLowerCase();
    if (!query) {
      results.replaceChildren(...Array.from(original.cloneNode(true).childNodes));
      status.textContent = 'Browse all pages below. Type to search.';
      return;
    }
    status.textContent = 'Searching…';
    try {
      if (!dataPromise) dataPromise = fetch('/site/search.json').then(response => {
        if (!response.ok) throw new Error('Search index unavailable');
        return response.json();
      }).catch(error => { dataPromise = null; throw error; });
      const data = await dataPromise;
      if (current !== generation) return;
      const terms = query.split(/\s+/);
      const matches = data.filter(item => terms.every(term => (item.title + ' ' + item.text).toLocaleLowerCase().includes(term)))
        .sort((a,b) => Number(b.title.toLocaleLowerCase().includes(query)) - Number(a.title.toLocaleLowerCase().includes(query)) || a.title.localeCompare(b.title));
      results.replaceChildren();
      for (const item of matches.slice(0, 80)) {
        const card = document.createElement('article');
        card.className = 'search-hit';
        const link = document.createElement('a');
        link.href = item.url;
        link.textContent = item.title;
        const excerpt = document.createElement('p');
        const position = item.text.toLocaleLowerCase().indexOf(terms[0]);
        const start = Math.max(0, position - 70);
        excerpt.textContent = (start ? '…' : '') + item.text.slice(start, start + 230) + '…';
        card.append(link, excerpt);
        results.append(card);
      }
      status.textContent = `${matches.length} matching pages${matches.length > 80 ? ' · showing the first 80' : ''}`;
    } catch (error) {
      if (current === generation) status.textContent = 'Search could not load. You can still browse all pages; try typing again to retry.';
    }
  };
  let timer;
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 120); });
  const query = new URLSearchParams(location.search).get('q');
  if (query) { input.value = query; search(); }
})();
