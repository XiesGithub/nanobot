(() => {
  'use strict';
  const catalog = window.STUDY_CATALOG;
  const sections = window.STUDY_SECTIONS;
  if (!catalog || !sections) return;
  const chapters = catalog.chapters;
  const pages = chapters.flatMap(chapter => chapter.pages.map(page => ({...page, chapter})));
  const identify = (chapterId, pageId) => pages.find(page => page.chapter.id === chapterId && page.id === pageId);
  const link = (href, title, kind) => ({href, title, kind});
  function sourceLink(context = {}) {
    if (/^q(?:0[1-9]|[1-3][0-9]|40)$/.test(context.fromInterview || '')) {
      return link(`chapters/11-interview/index.html#${context.fromInterview}`, `面试题 ${context.fromInterview.toUpperCase()}`, '返回原题');
    }
    const [chapter, page] = (context.fromLesson || '').split(':');
    const origin = identify(chapter, page);
    return origin ? link(origin.href, `${origin.chapter.shortTitle} · ${origin.title}`, '返回来源页') : null;
  }
  function plan(chapterId, pageId, sectionId = '', context = {}) {
    const page = identify(chapterId, pageId);
    if (!page) return null;
    const items = (sections[page.href] || []).filter(item => !context.visibleSectionIds || context.visibleSectionIds.includes(item.id));
    const sectionIndex = items.findIndex(item => item.id === sectionId);
    const pageIndex = pages.findIndex(item => item.href === page.href);
    const previousPage = pages[pageIndex - 1];
    const nextPage = pages[pageIndex + 1];
    const sectionLink = (item, kind) => link(page.href + '#' + item.id, item.title, kind);
    let previous = previousPage ? link(previousPage.href, previousPage.chapter.id === chapterId ? previousPage.title : previousPage.chapter.title, previousPage.chapter.id === chapterId ? '上一页' : '上一章') : link('index.html', '手册首页', '返回');
    if (sectionIndex > 0) previous = sectionLink(items[sectionIndex - 1], '上一节');
    else if (sectionIndex === 0) previous = link(page.href, page.title, '本页开头');
    const next = items[sectionIndex + 1] ? sectionLink(items[sectionIndex + 1], '下一节') : (nextPage ? link(nextPage.href, nextPage.chapter.id === chapterId ? nextPage.title : nextPage.chapter.title, nextPage.chapter.id === chapterId ? '下一页' : '下一章') : null);
    return {page, items, sectionIndex, previous, next, overview: link(page.chapter.href, page.chapter.shortTitle, '本章概览'), source: sourceLink(context)};
  }
  // The same pure route planner is exercised by the offline navigation checks.
  window.STUDY_READING = {plan, sourceLink};
  if (typeof document === 'undefined') return;
  const chapterId = document.body.dataset.chapter;
  const pageId = document.body.dataset.page;
  const initial = plan(chapterId, pageId);
  if (!initial) return;
  const studyRoot = new URL((document.body.dataset.studyRoot || '.') + '/', window.location.href);
  const context = Object.fromEntries(new URLSearchParams(window.location.search));
  const source = sourceLink(context);
  const resolve = href => new URL(href, studyRoot).href;
  const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const contextualHref = href => {
    const target = new URL(href, studyRoot);
    if (source) {
      const key = source.kind === '返回原题' ? 'fromInterview' : 'fromLesson';
      target.searchParams.set(key, context[key]);
    }
    return target.href;
  };
  const renderLink = (item, cls = '') => item ? `<a class="${cls}" title="${escape(item.kind + ' · ' + item.title)}" href="${escape(contextualHref(item.href))}"><small>${escape(item.kind)}</small><span>${escape(item.title)}</span></a>` : '<span class="reader-end"><small>阅读完成</small><span>已到全书末尾</span></span>';
  const dock = document.createElement('nav');
  dock.className = 'reading-dock';
  dock.setAttribute('aria-label', '常驻阅读导航');
  dock.innerHTML = `<div class="reader-top"><div class="reader-position"><small>第 ${escape(initial.page.chapter.number)} 章 · ${escape(initial.page.title)}</small><strong data-reader-position>本页开头</strong></div><details class="reader-toc"><summary>本页目录 <span aria-hidden="true">⌃</span></summary><nav aria-label="本页小节"><a href="${escape(contextualHref(initial.page.href))}">↑ 本页开头</a>${initial.items.map((item, index) => `<a data-reader-section="${escape(item.id)}" href="${escape(contextualHref(initial.page.href + '#' + item.id))}">${String(index + 1).padStart(2, '0')} · ${escape(item.title)}</a>`).join('')}</nav></details></div>${source ? `<a class="reader-source" href="${escape(resolve(source.href))}">← ${escape(source.kind)}：${escape(source.title)}</a>` : ''}<div class="reader-controls" data-reader-controls></div>`;
  document.body.append(dock);
  document.body.classList.add('has-reading-dock');
  // Keep the current chapter close at hand instead of burying it after 11 chapters.
  document.querySelectorAll('[data-study-sidebar]').forEach(sidebar => {
    const navs = sidebar.querySelectorAll(':scope > nav');
    const labels = sidebar.querySelectorAll(':scope > .nav-label');
    if (navs.length === 2 && labels.length === 2) {
      sidebar.prepend(navs[1]); sidebar.prepend(labels[1]);
      labels[1].textContent = `本章页面 · ${initial.page.chapter.shortTitle}`;
    }
  });
  // Remember the authored lesson entry without relying on browser history/referrer.
  document.querySelectorAll('.content a[href], .site-header a[href]').forEach(anchor => {
    if (anchor.closest('[data-study-page-nav], [data-study-breadcrumb], .interview-return')) return;
    const target = new URL(anchor.getAttribute('href'), window.location.href);
    const destination = pages.find(page => resolve(page.href) === target.origin + target.pathname || new URL(page.href, studyRoot).href === target.href.split(/[?#]/)[0]);
    if (!destination || destination.href === initial.page.href || !target.hash || target.searchParams.has('fromInterview')) return;
    target.searchParams.set('fromLesson', `${chapterId}:${pageId}`);
    anchor.href = target.href;
  });
  let current = undefined;
  const position = dock.querySelector('[data-reader-position]');
  const controls = dock.querySelector('[data-reader-controls]');
  const entries = initial.items.map(item => ({...item, element: document.getElementById(item.id)})).filter(item => item.element);
  if (source?.href === initial.page.href) dock.querySelector('.reader-source')?.remove();
  function setCurrent(id) {
    if (current === id) return;
    current = id;
    const state = plan(chapterId, pageId, id, {...context, visibleSectionIds: entries.filter(item => !item.element.hidden).map(item => item.id)});
    position.textContent = state.sectionIndex < 0 ? '本页开头' : `${state.sectionIndex + 1} / ${state.items.length} · ${state.items[state.sectionIndex].title}`;
    position.title = position.textContent;
    controls.innerHTML = renderLink(state.previous, 'reader-prev') + renderLink(state.overview, 'reader-overview') + renderLink(state.next, 'reader-next');
    if (!state.items.length && chapterId === 'interview') {
      position.textContent = '当前筛选没有匹配题目';
      position.title = position.textContent;
      controls.querySelector('.reader-end').innerHTML = '<small>暂无可导航小节</small><span>请调整筛选条件</span>';
    }
    dock.querySelectorAll('[data-reader-section]').forEach(anchor => {
      anchor.hidden = !state.items.some(item => item.id === anchor.dataset.readerSection);
      if (anchor.dataset.readerSection === id) anchor.setAttribute('aria-current', 'location');
      else anchor.removeAttribute('aria-current');
    });
  }
  function fromScroll() {
    // Ignore filtered-out interview groups and other non-visible lesson sections.
    const visible = entries.filter(item => item.element.getClientRects().length);
    const passed = visible.filter(item => item.element.getBoundingClientRect().top <= 140);
    setCurrent(passed.length ? passed[passed.length - 1].id : '');
  }
  function fromHash() {
    let id;
    try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { fromScroll(); return; }
    const target = document.getElementById(id);
    const owner = entries.find(item => item.id === id || (target && item.element.contains(target)));
    if (owner) setCurrent(owner.id); else fromScroll();
  }
  let scheduled = false;
  window.addEventListener('scroll', () => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; fromScroll(); });
  }, {passive: true});
  window.addEventListener('hashchange', fromHash);
  window.addEventListener('pageshow', fromHash);
  if (chapterId === 'interview') {
    const observer = new MutationObserver(() => { current = undefined; fromScroll(); });
    entries.forEach(item => observer.observe(item.element, {attributes: true, attributeFilter: ['hidden']}));
  }
  dock.querySelector('.reader-toc nav').addEventListener('click', event => {
    if (event.target.closest('a')) dock.querySelector('.reader-toc').open = false;
  });
  dock.addEventListener('keydown', event => {
    if (event.key === 'Escape') { dock.querySelector('.reader-toc').open = false; dock.querySelector('summary').focus(); }
  });
  setCurrent('');
  fromHash();
})();
