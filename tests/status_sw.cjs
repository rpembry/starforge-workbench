const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function run() {
  const handlers = {};
  const stores = new Map();
  const added = [];
  const removed = [];
  let claimed = 0;
  let skipped = 0;
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const entries = stores.get(name);
      return {
        async addAll(paths) { added.push(...paths); for (const path of paths) entries.set(path, {ok: true, path}); },
        async match(request) { return entries.get(new URL(request.url).pathname); },
        async put(request, response) { entries.set(new URL(request.url).pathname, response); }
      };
    },
    async keys() { return [...stores.keys()]; },
    async delete(name) { removed.push(name); return stores.delete(name); }
  };
  stores.set('workbench-status-shell-v1', new Map());
  stores.set('workbench-status-shell-v2', new Map());
  stores.set('workbench-status-shell-v3', new Map());
  stores.set('other-application-cache', new Map());
  const self = {location: {origin: 'https://example.invalid'}, clients: {claim: async () => { claimed++; }},
    skipWaiting: () => { skipped++; }, addEventListener: (name, handler) => { handlers[name] = handler; }};
  let network = 0;
  const fetch = async request => { network++; return {ok: true, type: 'basic', clone() { return this; }}; };
  vm.runInNewContext(fs.readFileSync('src/workbench/static/status-sw.js', 'utf8'), {self, caches, URL, Set, fetch});
  async function lifecycle(name, extra = {}) {
    let work;
    handlers[name]({...extra, waitUntil(promise) { work = promise; }});
    await work;
  }
  await lifecycle('install');
  assert.deepEqual(added.sort(), ['/assets/status.js', '/status/', '/status/manifest.webmanifest',
    '/status/icon-192.png', '/status/icon-512.png'].sort());
  assert.equal(added.some(path => path.startsWith('/api/') || path.includes('logo')), false);
  async function request(path, method = 'GET') {
    let response;
    handlers.fetch({request: {url: 'https://example.invalid' + path, method},
      respondWith(promise) { response = promise; }});
    return response;
  }
  assert.equal(await request('/api/status/allowances'), undefined);
  assert.equal(await request('/api/status/config'), undefined);
  assert.equal(await request('/api/actions', 'POST'), undefined);
  assert.equal(await request('/status/?secret=fixture'), undefined);
  assert.equal((await request('/status/')).path, '/status/');
  assert.equal(network, 0); // shell served from static allowlist cache
  await lifecycle('activate');
  assert.deepEqual(removed, ['workbench-status-shell-v1', 'workbench-status-shell-v2', 'workbench-status-shell-v3']);
  assert.equal(stores.has('other-application-cache'), true);
  assert.equal(claimed, 1);
  handlers.message({data: 'ACTIVATE_UPDATE', source: {url: 'https://evil.example/status/'}});
  assert.equal(skipped, 0);
  handlers.message({data: 'ACTIVATE_UPDATE', source: {url: 'https://example.invalid/status/'}});
  assert.equal(skipped, 1);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
