(() => {
  const root = document.documentElement;
  let savedTheme;
  try { savedTheme = localStorage.getItem('nanobot-study-theme'); } catch { /* Offline privacy settings may disable storage. */ }
  const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = savedTheme || (prefersDark ? 'dark' : 'light');

  function updateThemeLabel() {
    document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
      const dark = root.dataset.theme === 'dark';
      button.textContent = dark ? '☀' : '☾';
      button.setAttribute('aria-label', dark ? '切换到浅色主题' : '切换到深色主题');
    });
  }

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('nanobot-study-theme', root.dataset.theme); } catch { /* Theme still works for this page. */ }
      updateThemeLabel();
    });
  });
  updateThemeLabel();

  document.querySelectorAll('[data-mobile-nav]').forEach((select) => {
    select.addEventListener('change', () => {
      if (select.value) window.location.href = select.value;
    });
  });

  document.querySelectorAll('[data-stepper]').forEach((stepper) => {
    const nodes = [...stepper.querySelectorAll('[data-step]')];
    const panels = [...stepper.querySelectorAll('[data-step-panel]')];
    const count = stepper.querySelector('[data-step-count]');
    const previous = stepper.querySelector('[data-step-prev]');
    const next = stepper.querySelector('[data-step-next]');
    let current = 0;

    function render(index) {
      current = Math.max(0, Math.min(index, nodes.length - 1));
      nodes.forEach((node, i) => {
        node.classList.toggle('active', i === current);
        node.setAttribute('aria-pressed', i === current ? 'true' : 'false');
      });
      panels.forEach((panel, i) => panel.classList.toggle('active', i === current));
      if (count) count.textContent = `${current + 1} / ${nodes.length}`;
      if (previous) previous.disabled = current === 0;
      if (next) next.disabled = current === nodes.length - 1;
    }

    nodes.forEach((node, i) => node.addEventListener('click', () => render(i)));
    if (previous) previous.addEventListener('click', () => render(current - 1));
    if (next) next.addEventListener('click', () => render(current + 1));
    render(0);
  });
})();
