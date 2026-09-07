// Execute real navigation scripts against each page's metadata without a browser.
// This checks generated links and selection; it does not claim to render layout.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const catalogSource = fs.readFileSync(path.join(root, 'assets/catalog.js'), 'utf8');
const navigationSource = fs.readFileSync(path.join(root, 'assets/navigation.js'), 'utf8');
const shared = { window: {} };
vm.runInNewContext(catalogSource, shared);
const catalog = shared.window.STUDY_CATALOG;
const pages = catalog.chapters.flatMap(chapter => chapter.pages.map(page => ({ ...page, chapter })));
assert.equal(new Set(pages.map(page => page.href)).size, pages.length, 'duplicate catalog page');
assert.equal(new Set(catalog.chapters.map(chapter => chapter.id)).size, catalog.chapters.length);

for (const page of [{ href: 'index.html', id: 'home' }, ...pages]) {
  const absolute = path.join(root, page.href);
  const html = fs.readFileSync(absolute, 'utf8');
  const attr = name => html.match(new RegExp(`${name}="([^"]+)"`))?.[1];
  assert.equal(attr('data-page'), page.id, `${page.href}: page ID differs from catalog`);
  assert.equal(attr('data-chapter'), page.chapter?.id, `${page.href}: chapter ID differs`);
  const containers = new Map(['[data-study-sidebar]', '[data-mobile-nav]', '[data-study-breadcrumb]', '[data-study-page-nav]'].map(selector => [selector, { innerHTML: '' }]));
  const document = {
    body: { dataset: { studyRoot: attr('data-study-root'), chapter: attr('data-chapter'), page: attr('data-page') } },
    querySelectorAll: selector => containers.has(selector) ? [containers.get(selector)] : [],
  };
  vm.runInNewContext(navigationSource, { window: shared.window, document });
  for (const element of containers.values()) {
    for (const match of element.innerHTML.matchAll(/(?:href|value)="([^"]+)"/g)) {
      const target = path.resolve(path.dirname(absolute), match[1]);
      assert.ok(target.startsWith(root + path.sep), `${page.href}: generated link leaves handbook`);
      assert.ok(fs.existsSync(target), `${page.href}: missing generated target ${match[1]}`);
    }
  }
  const options = containers.get('[data-mobile-nav]').innerHTML;
  assert.equal((options.match(/ selected/g) || []).length, 1, `${page.href}: selected mobile option`);
  if (page.chapter) {
    const links = containers.get('[data-study-page-nav]').innerHTML;
    assert.equal((links.match(/<a /g) || []).length, 3, `${page.href}: previous/overview/next navigation`);
    const pageIndex = pages.findIndex(item => item.href === page.href);
    const targets = [...links.matchAll(/href="([^"]+)"/g)].map(match => path.resolve(path.dirname(absolute), match[1]));
    assert.equal(targets[0], path.join(root, pages[pageIndex - 1]?.href || 'index.html'));
    assert.equal(targets[1], path.join(root, page.chapter.href));
    assert.equal(targets[2], path.join(root, pages[pageIndex + 1]?.href || 'index.html'));
    assert.ok(containers.get('[data-study-sidebar]').innerHTML.includes('page-link active'), `${page.href}: active lesson`);
  }
}

// Offline/privacy browser settings may deny storage. The theme must still toggle.
const button = { attrs: {}, setAttribute(name, value) { this.attrs[name] = value; }, addEventListener(name, fn) { this[name] = fn; } };
const document = {
  documentElement: { dataset: {} },
  querySelectorAll: selector => selector === '[data-theme-toggle]' ? [button] : [],
};
vm.runInNewContext(fs.readFileSync(path.join(root, 'assets/study.js'), 'utf8'), {
  document, window: { matchMedia: () => ({ matches: false }) },
  localStorage: { getItem() { throw Error('storage denied'); }, setItem() { throw Error('storage denied'); } },
});
assert.equal(document.documentElement.dataset.theme, 'light');
button.click();
assert.equal(document.documentElement.dataset.theme, 'dark');
assert.equal(button.attrs['aria-label'], '切换到浅色主题');
// Module lessons offer a bounded return link only for valid interview IDs.
for (const [query, expected] of [
  ['?fromInterview=q22', '#q22'],
  ['?fromInterview=q40', '#q40'],
  ['?fromInterview=q00', null],
  ['?fromInterview=q99', null],
  ['?fromInterview=https%3A%2F%2Fexample.com', null],
]) {
  const breadcrumb = { innerHTML: '', appended: '', insertAdjacentHTML(position, html) { this.appended += html; } };
  const doc = {
    body: { dataset: { studyRoot: '../..', chapter: 'memory-system', page: 'overview' } },
    querySelectorAll: selector => selector === '[data-study-breadcrumb]' ? [breadcrumb] : [],
  };
  vm.runInNewContext(navigationSource, { window: { ...shared.window, location: { search: query } }, document: doc, URLSearchParams });
  if (expected) {
    assert.ok(breadcrumb.appended.includes(`../../chapters/11-interview/index.html${expected}`));
    assert.ok(breadcrumb.appended.includes('返回面试题'));
  } else {
    assert.equal(breadcrumb.appended, '', 'invalid interview ID must not add a link');
  }
}
console.log(`Navigation passed: ${pages.length + 1} pages; catalog identity, generated links, selected lesson, offline theme, interview return links`);
