const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const script = readFileSync(require('node:path').join(__dirname, '../src/workbench/static/coordinator-poll.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

test('newest status wins without replacing unfinished input or focus', async () => {
  const pending = [];
  const input = { value: 'unfinished spec' };
  const button = { disabled: false };
  const display = { dataset: { jobId: 'a'.repeat(32), version: '7' }, textContent: '' };
  const listeners = {};
  let interval;
  const document = {
    hidden: false,
    activeElement: input,
    getElementById: () => display,
    querySelectorAll: () => [button],
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  vm.runInNewContext(script, {
    document, location: { href: 'http://localhost/coordinator/jobs/' + display.dataset.jobId },
    fetch: () => new Promise(resolve => pending.push(resolve)),
    AbortController, setInterval: fn => { interval = fn; }, clearInterval: () => {},
    addEventListener: (name, fn) => { listeners[name] = fn; },
  });
  assert.equal(pending.length, 1);
  interval();
  assert.equal(pending.length, 2);
  pending[1]({ ok: true, json: async () => ({ id: display.dataset.jobId, version: 9 }) });
  await settle();
  assert.match(display.textContent, /version 9/);
  pending[0]({ ok: true, json: async () => ({ id: display.dataset.jobId, version: 7 }) });
  await settle();
  assert.match(display.textContent, /version 9/);
  assert.equal(input.value, 'unfinished spec');
  assert.equal(document.activeElement, input);
  assert.equal(button.disabled, false);
  interval();
  pending[2]({ status: 401, ok: false });
  await settle();
  assert.match(display.textContent, /Authentication expired/);
  assert.equal(button.disabled, true);
  listeners.pagehide();
});
