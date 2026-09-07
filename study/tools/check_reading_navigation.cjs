const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const sandbox = {window: {}};
for (const name of ['catalog', 'sections', 'reading-navigation']) vm.runInNewContext(fs.readFileSync(path.join(root, `assets/${name}.js`), 'utf8'), sandbox);
const {STUDY_CATALOG: catalog, STUDY_SECTIONS: sections, STUDY_READING: reader} = sandbox.window;
const pages = catalog.chapters.flatMap(chapter => chapter.pages.map(page => ({...page, chapter})));
function valid(link) {
  if (!link) return;
  const [file, anchor] = link.href.split('#');
  const target = path.resolve(root, file);
  assert.ok(target.startsWith(root + path.sep));
  const html = fs.readFileSync(target, 'utf8');
  if (anchor) assert.ok(html.includes(`id="${anchor}"`), link.href);
}
let total = 0;
for (const [i, page] of pages.entries()) {
  const html = fs.readFileSync(path.join(root, page.href), 'utf8');
  assert.ok(html.includes('assets/reading-navigation.js'));
  assert.ok(html.includes('assets/sections.js'));
  const items = sections[page.href];
  assert.ok(items.length);
  assert.equal(new Set(items.map(s => s.id)).size, items.length);
  for (const [j, item] of [{id: ''}, ...items].entries()) {
    const state = reader.plan(page.chapter.id, page.id, item.id);
    for (const link of [state.previous, state.next, state.overview]) valid(link);
    assert.equal(state.overview.href, page.chapter.href);
    if (j < items.length) assert.equal(state.next.href, page.href + '#' + items[j].id);
    else assert.equal(state.next?.href, pages[i + 1]?.href);
    if (j > 1) assert.equal(state.previous.href, page.href + '#' + items[j - 2].id);
    total++;
  }
  if (page.id === 'workshop') {
    assert.equal(reader.plan(page.chapter.id, page.id, 'contract').next.href, page.href + '#dataflow');
  }
  valid(reader.sourceLink({fromLesson: page.chapter.id + ':' + page.id}));
}
assert.equal(reader.sourceLink({fromLesson: 'https://evil.invalid'}), null);
assert.equal(reader.sourceLink({fromInterview: 'q99'}), null);
valid(reader.sourceLink({fromInterview: 'q40'}));
assert.equal(reader.sourceLink({fromInterview: 'q22', fromLesson: 'bad'}).kind, '返回原题');
const interview = pages.at(-1);
const filtered = sections[interview.href].slice(2, 4).map(s => s.id);
assert.equal(reader.plan(interview.chapter.id, interview.id, '', {visibleSectionIds: filtered}).next.href, interview.href + '#' + filtered[0]);
assert.equal(reader.plan(interview.chapter.id, interview.id, filtered[1], {visibleSectionIds: filtered}).next, null);
console.log(`Reading routes passed: ${pages.length} pages, ${total} positions; section order, chapter boundaries, overview and source links.`);
