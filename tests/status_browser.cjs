const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.handlers = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; this.textContent = ''; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  fire(name) { this.handlers[name]?.(); }
  allText() { return this.textContent + this.children.map(child => child.allText()).join(' '); }
}

async function run() {
  let clock = Date.parse('2026-10-03T12:00:00Z');
  class ClockDate extends Date { static now() { return clock; } }
  const boxes = ['attention', 'jobs', 'hosts', 'allowances'].map(name => {
    const box = new Element('input'); box.dataset.widget = name; return box;
  });
  const cards = new Element('main');
  const connection = new Element('p');
  const refresh = new Element('button');
  const defaults = new Element('button');
  const appUpdate = new Element('button');
  const document = {
    hidden: false,
    body: {dataset: {timezone: 'America/Indiana/Indianapolis'}},
    handlers: {},
    querySelectorAll: () => boxes,
    getElementById: id => ({cards, connection, refresh, defaults, 'app-update': appUpdate})[id],
    querySelector: selector => cards.children.find(child => selector === `[data-card="${child.dataset.card}"]`),
    createElement: tag => new Element(tag),
    addEventListener(name, handler) { this.handlers[name] = handler; },
    fire(name) { this.handlers[name]?.(); }
  };
  const window = {handlers: {}, intervals: [], addEventListener(name, handler) { this.handlers[name] = handler; },
    fire(name, event) { this.handlers[name]?.(event); }, setInterval(handler) { this.intervals.push(handler); }};
  const localStorage = {getItem: () => '["allowances"]', setItem: () => {}};
  const navigator = {onLine: true};
  const fetches = [];
  let configStatus = 200;
  let currentProfile = 'synthetic-fixture';
  const fetch = (url, options) => {
    fetches.push({url, options});
    if (url === '/api/status/config') return Promise.resolve({ok: configStatus === 200, status: configStatus,
      json: () => Promise.resolve({profile: currentProfile, timezone: 'America/Indiana/Indianapolis'})});
    return Promise.resolve({ok: true, json: () => Promise.resolve({source: 'manual observation',
      checked_at: '2026-10-03T12:00:00Z', items: [{provider: 'ExampleAI', product: 'Assistant', profile: 'personal',
        bucket: 'default', window: 'five-hour', remaining_percent: 101.25,
        observed_at: '2026-10-03T12:00:00Z', reset_at: '2026-10-03T12:02:00Z'}]})});
  };
  const script = fs.readFileSync('src/workbench/static/status.js', 'utf8');
  vm.runInNewContext(script, {document, window, localStorage, navigator, fetch,
    AbortController, Date: ClockDate, Intl, Map, Object, Array, Number, Math, JSON, Error});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(fetches.map(item => item.url), ['/api/status/config', '/api/status/allowances']);
  assert.equal(window.intervals.length, 1);
  assert.match(cards.allText(), /101.25% \(manual\)/);
  assert.match(cards.allText(), /2 minutes/);
  assert.match(cards.allText(), /EDT/);
  clock += 60000; window.intervals[0]();
  assert.match(cards.allText(), /1 minute/);
  clock += 4 * 60000;
  document.hidden = true; document.fire('visibilitychange'); window.intervals[0]();
  assert.match(cards.allText(), /View hidden; status cleared until resume/);
  document.hidden = false; document.fire('visibilitychange');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(cards.allText(), /refresh due; awaiting new evidence/);
  assert.match(cards.allText(), /stale/);
  window.fire('pageshow', {persisted: false});
  assert.equal(window.intervals.length, 1);
  assert.equal(fetches.length, 4);
  navigator.onLine = false; window.fire('offline');
  assert.match(cards.allText(), /Offline; no fresh status available/);
  assert.equal(fetches.length, 4);
  navigator.onLine = true; window.fire('online');
  assert.equal(fetches.length, 4); // reconnect waits for an explicit refresh
  currentProfile = 'another-fixture-profile';
  refresh.fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(connection.textContent, /Profile changed/);
  assert.match(cards.allText(), /101.25%/);
  configStatus = 401;
  refresh.fire('click');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(cards.allText(), /Sign-in required/);
  assert.doesNotMatch(cards.allText(), /101.25%/);
  assert.match(connection.textContent, /Session expired/);
  const afterExpiry = fetches.length;
  boxes[3].checked = false; boxes[3].fire('change');
  assert.equal(cards.allText().includes('101.25'), false);
  assert.equal(fetches.length, afterExpiry); // unselected widget makes no request
}

run().catch(error => { console.error(error); process.exitCode = 1; });
