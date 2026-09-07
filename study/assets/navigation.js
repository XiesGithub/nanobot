(() => {
  const body = document.body;
  const catalog = window.STUDY_CATALOG;
  if (!catalog) return;

  const rootPath = body.dataset.studyRoot || '.';
  const chapterId = body.dataset.chapter || '';
  const pageId = body.dataset.page || 'home';
  const resolveHref = (href) => `${rootPath}/${href}`.replace(/\/\.\//g, '/');
  const escapeHtml = (value) => String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');

  const currentChapter = catalog.chapters.find((chapter) => chapter.id === chapterId);

  document.querySelectorAll('[data-study-sidebar]').forEach((sidebar) => {
    const chapters = catalog.chapters.map((chapter) => {
      const active = chapter.id === chapterId ? ' active' : '';
      const status = chapter.status === 'planned' ? '<span class="nav-status">规划中</span>' : '';
      return `<a class="chapter-link${active}" href="${escapeHtml(resolveHref(chapter.href))}"><span class="nav-num">${escapeHtml(chapter.number)}</span><span>${escapeHtml(chapter.shortTitle)}${status}</span></a>`;
    }).join('');

    const pages = currentChapter ? currentChapter.pages.map((page, index) => {
      const active = page.id === pageId ? ' active' : '';
      const label = page.id === 'overview' ? '概' : String(index).padStart(2, '0');
      return `<a class="page-link${active}" href="${escapeHtml(resolveHref(page.href))}"><span class="nav-num">${label}</span><span>${escapeHtml(page.title)}</span></a>`;
    }).join('') : '';

    sidebar.innerHTML = `
      <div class="nav-label">全书章节</div>
      <nav>
        <a class="chapter-link${pageId === 'home' ? ' active' : ''}" href="${escapeHtml(resolveHref('index.html'))}"><span class="nav-num">⌂</span><span>手册首页</span></a>
        ${chapters}
      </nav>
      ${pages ? `<div class="nav-label">本章小节</div><nav>${pages}</nav>` : ''}
    `;
  });

  document.querySelectorAll('[data-mobile-nav]').forEach((select) => {
    const options = [`<option value="${escapeHtml(resolveHref('index.html'))}"${pageId === 'home' ? ' selected' : ''}>手册首页</option>`];
    catalog.chapters.forEach((chapter) => {
      const items = chapter.pages.map((page) => {
        const selected = chapter.id === chapterId && page.id === pageId ? ' selected' : '';
        return `<option value="${escapeHtml(resolveHref(page.href))}"${selected}>${escapeHtml(chapter.number)} · ${escapeHtml(page.title)}</option>`;
      }).join('');
      options.push(`<optgroup label="${escapeHtml(chapter.title)}">${items}</optgroup>`);
    });
    select.innerHTML = options.join('');
  });

  document.querySelectorAll('[data-study-breadcrumb]').forEach((breadcrumb) => {
    const parts = [`<a href="${escapeHtml(resolveHref('index.html'))}">手册首页</a>`];
    if (currentChapter) {
      parts.push(`<a href="${escapeHtml(resolveHref(currentChapter.href))}">${escapeHtml(currentChapter.shortTitle)}</a>`);
      const page = currentChapter.pages.find((item) => item.id === pageId);
      if (page && page.id !== 'overview') parts.push(escapeHtml(page.title));
    }
    breadcrumb.innerHTML = parts.join(' / ');
  });

  document.querySelectorAll('[data-study-page-nav]').forEach((pageNav) => {
    if (!currentChapter) return;
    const index = currentChapter.pages.findIndex((page) => page.id === pageId);
    const chapterIndex = catalog.chapters.indexOf(currentChapter);
    const previousChapter = catalog.chapters[chapterIndex - 1];
    const nextChapter = catalog.chapters[chapterIndex + 1];
    const previous = index > 0 ? currentChapter.pages[index - 1] : previousChapter?.pages.at(-1);
    const next = index >= 0 && index < currentChapter.pages.length - 1
      ? currentChapter.pages[index + 1]
      : nextChapter?.pages[0];
    const previousHtml = previous
      ? `<a href="${escapeHtml(resolveHref(previous.href))}"><small>${index > 0 ? '上一页' : '上一章'}</small>← ${escapeHtml(index > 0 ? previous.title : previousChapter.title)}</a>`
      : `<a href="${escapeHtml(resolveHref('index.html'))}"><small>返回</small>← 手册首页</a>`;
    const nextHtml = next
      ? `<a href="${escapeHtml(resolveHref(next.href))}"><small>${index < currentChapter.pages.length - 1 ? '下一页' : '下一章'}</small>${escapeHtml(index < currentChapter.pages.length - 1 ? next.title : nextChapter.title)} →</a>`
      : `<a href="${escapeHtml(resolveHref('index.html'))}"><small>本章完成</small>回到手册首页 →</a>`;
    pageNav.innerHTML = previousHtml + `<a href="${escapeHtml(resolveHref(currentChapter.href))}"><small>本章概览</small>${escapeHtml(currentChapter.shortTitle)}</a>` + nextHtml;
  });

  // A bounded question ID links a lesson back to its originating interview card.
  // No caller-controlled URL or HTML is accepted from the query string.
  if (window.location?.search && chapterId !== 'interview') {
    const question = new URLSearchParams(window.location.search).get('fromInterview');
    if (question && /^q(?:0[1-9]|[1-3][0-9]|40)$/.test(question)) {
      const returnHref = resolveHref(`chapters/11-interview/index.html#${question}`);
      document.querySelectorAll('[data-study-breadcrumb]').forEach((breadcrumb) => {
        breadcrumb.insertAdjacentHTML('afterend', `<aside class="learning-bridge interview-return" aria-label="返回面试题"><p><a href="${escapeHtml(returnHref)}">← 返回面试题 ${question.toUpperCase()}</a> · 读完本页后，可回到原题继续作答。</p></aside>`);
      });
    }
  }
})();
