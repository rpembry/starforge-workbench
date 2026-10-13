import assert from 'node:assert/strict';
import test from 'node:test';
import { extractNote, parseArgs, renderMarkdown, validateConfig } from '../bin/keep-collector.mjs';

class Element {
  constructor(attributes = {}, text = '', children = [], checked) {
    this.attributes = attributes;
    this.textContent = this.innerText = text;
    if (checked !== undefined) this.checked = checked;
    this.children = children;
    for (const child of children) child.parentElement = this;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  matches(selector) {
    const attributes = [...selector.matchAll(/\[([^=\]]+)(?:="([^"]*)")?\]/g)];
    return attributes.length > 0 && attributes.every(([, name, value]) =>
      this.attributes[name] !== undefined && (value === undefined || this.attributes[name] === value));
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    const result = [];
    const visit = node => {
      for (const child of node.children) {
        if (selectors.some(item => child.matches(item))) result.push(child);
        visit(child);
      }
    };
    visit(this);
    return result;
  }
  closest(selector) {
    const selectors = selector.split(',').map(value => value.trim());
    for (let node = this; node; node = node.parentElement) {
      if (selectors.some(item => node.matches(item))) return node;
    }
    return null;
  }
}

const element = (attributes, text = '', children = [], checked) => new Element(attributes, text, children, checked);
const item = (text, checked, aria = checked ? 'true' : 'false') => element({'role': 'listitem'}, '', [
  element({'role': 'checkbox', 'aria-checked': aria}, '', [], checked),
  element({'contenteditable': 'true'}, text),
]);
const card = ({title, body = '', pinned = true, items = [], checklist = true, titleRole = 'heading'}) => {
  const titleNode = title === null ? [] : [titleRole === 'heading'
    ? element({'role': 'heading'}, title) : element({'contenteditable': 'true', 'aria-label': 'Title'}, title)];
  const marker = checklist && items.length === 0 ? [element({'aria-label': 'Add list item'})] : [];
  return element({'role': 'article'}, body, [
    ...titleNode, element({'aria-label': pinned ? 'Unpin note' : 'Pin note'}), ...items, ...marker,
  ]);
};
const documentWith = (...cards) => element({}, '', cards);
const config = (requirePinned = true) => ({noteTitle: 'Shopping', requirePinned});

test('accepts a local runtime note selector', () => {
  assert.deepEqual(validateConfig({ note_title: 'Daily checklist', require_pinned: true }), {
    noteTitle: 'Daily checklist', requirePinned: true,
  });
});

test('rejects an empty note selector', () => {
  assert.throws(() => validateConfig({ note_title: ' ' }), /note_title/);
});

test('parses collector options', () => {
  assert.deepEqual(parseArgs(['--config', '/tmp/selector.json', '--format', 'markdown']), {
    cdpUrl: 'http://127.0.0.1:9222', config: '/tmp/selector.json', format: 'markdown',
  });
});

test('renders checklist output without changing item state', () => {
  assert.equal(renderMarkdown({
    collected_at: '2026-09-12T12:00:00Z',
    note: { title: 'Checklist', pinned: true, items: [{ text: 'First item', checked: false }, { text: 'Done item', checked: true }] },
  }), '# Checklist\n\nCollected: 2026-09-12T12:00:00Z\nPinned: yes\n\n- [ ] First item\n- [x] Done item\n');
});

test('matches only an exact extracted title, not body or overlapping title text', () => {
  const result = extractNote(config(), documentWith(
    card({title: 'Errands', body: 'Remember the Shopping list', items: [item('Wrong', false)]}),
    card({title: 'Shopping tomorrow', items: [item('Also wrong', false)]}),
    card({title: 'Shopping', items: [item('Correct', false)]}),
  ));
  assert.deepEqual(result, {status: 'ok', note: {title: 'Shopping', pinned: true,
    items: [{text: 'Correct', checked: false}]}});
});

test('rejects duplicate exact titles instead of choosing page order', () => {
  assert.deepEqual(extractNote(config(), documentWith(
    card({title: 'Shopping', items: [item('One', false)]}),
    card({title: 'Shopping', items: [item('Two', true)]}),
  )), {status: 'ambiguous'});
});

test('applies pinned filtering before ambiguity checks', () => {
  const pinned = card({title: 'Shopping', pinned: true, items: [item('Pinned', false)]});
  const unpinned = card({title: 'Shopping', pinned: false, items: [item('Unpinned', false)]});
  assert.equal(extractNote(config(), documentWith(unpinned, pinned)).status, 'ok');
  assert.deepEqual(extractNote(config(false), documentWith(unpinned, pinned)), {status: 'ambiguous'});
});

test('does not fabricate a missing title or malformed checklist', () => {
  assert.deepEqual(extractNote(config(), documentWith(
    card({title: null, body: 'Shopping', items: [item('Wrong', false)]}),
  )), {status: 'not_found'});
  assert.deepEqual(extractNote(config(), documentWith(card({title: 'Shopping', checklist: false}))),
    {status: 'invalid_checklist'});
  const malformed = card({title: 'Shopping', items: [item('', false)]});
  assert.deepEqual(extractNote(config(), documentWith(malformed)), {status: 'invalid_checklist'});
});

test('distinguishes a valid empty checklist and preserves checked state', () => {
  assert.deepEqual(extractNote(config(), documentWith(card({title: 'Shopping'}))),
    {status: 'ok', note: {title: 'Shopping', pinned: true, items: []}});
  assert.deepEqual(extractNote(config(), documentWith(card({title: 'Shopping', titleRole: 'editable', items: [
    item('Unchecked', false), item('Checked', true),
  ]}))), {status: 'ok', note: {title: 'Shopping', pinned: true, items: [
    {text: 'Unchecked', checked: false}, {text: 'Checked', checked: true},
  ]}});
});
