(() => {
  'use strict';
  const names = {attention: 'Attention', jobs: 'Jobs', hosts: 'Collectors', allowances: 'Allowances'};
  const defaults = ['attention', 'jobs', 'hosts', 'allowances'];
  const boxes = [...document.querySelectorAll('[data-widget]')];
  const cards = document.getElementById('cards');
  const connection = document.getElementById('connection');
  const pending = new Map();
  const dynamic = [];
  let generation = 0;
  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  let zone = deviceZone;
  let profile = null;
  const time = value => {
    if (!value) return 'unknown';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'unknown' : new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      timeZone: zone, timeZoneName: 'short'
    }).format(date);
  };
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function readChoices() {
    try {
      const choice = JSON.parse(localStorage.getItem('workbench-status-widgets'));
      if (Array.isArray(choice) && choice.every(name => Object.hasOwn(names, name))) return choice;
    } catch (_) { /* private browsing may disable storage */ }
    return defaults;
  }
  function saveChoices(choice) {
    try { localStorage.setItem('workbench-status-widgets', JSON.stringify(choice)); } catch (_) {}
  }
  const selected = () => boxes.filter(box => box.checked).map(box => box.dataset.widget);
  function addLine(card, label, value) {
    const line = el('p', `${label}: ${value}`);
    card.append(line);
    return line;
  }
  function freshness(widget, item, now) {
    if (widget === 'attention') return 'current status unknown';
    const observed = widget === 'allowances' ? item.observed_at : item.heartbeat_at;
    const age = now - new Date(observed).getTime();
    if (!Number.isFinite(age)) return 'unknown';
    if (widget === 'allowances') {
      const reset = item.reset_at ? new Date(item.reset_at).getTime() : Infinity;
      return age > 3600000 || reset <= now ? 'stale' : 'manual';
    }
    return age > 90000 ? widget === 'hosts' ? 'disconnected' : 'stale' : 'fresh';
  }
  function updateDynamic() {
    const now = Date.now();
    for (const entry of dynamic) {
      entry.freshness.textContent = freshness(entry.widget, entry.item, now);
      if (entry.countdown) {
        const delta = new Date(entry.item.reset_at).getTime() - now;
        const minutes = Math.ceil(delta / 60000);
        entry.countdown.textContent = `Countdown: ${!Number.isFinite(delta) ? 'unknown' :
          delta <= 0 ? 'refresh due; awaiting new evidence' : `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`}`;
      }
    }
  }
  function renderItem(widget, item, container) {
    const card = el('div', '', 'card');
    let countdown = null;
    if (widget === 'allowances') {
      card.append(el('strong', `${item.provider} · ${item.product} · ${item.profile}`));
      addLine(card, 'Bucket / window', `${item.bucket} / ${item.window}`);
      addLine(card, 'Remaining', item.remaining_percent === null ? 'unknown' : `${item.remaining_percent}% (manual)`);
      addLine(card, 'Reset', item.reset_at ? time(item.reset_at) : 'unknown');
      countdown = item.reset_at ? addLine(card, 'Countdown', 'unknown') : null;
      addLine(card, 'Observed', time(item.observed_at));
    } else if (widget === 'attention') {
      card.append(el('strong', item.title || 'Untitled attention'));
      addLine(card, 'State', item.progress || 'unknown');
      addLine(card, 'Reason', item.reason || 'No details reported');
    } else if (widget === 'jobs') {
      card.append(el('strong', item.context || 'Unknown context'));
      addLine(card, 'Reported state', item.status || 'unknown');
      addLine(card, 'Provider', item.provider || 'unknown');
      addLine(card, 'Last heartbeat', time(item.heartbeat_at));
    } else {
      card.append(el('strong', item.source || 'Unknown collector'));
      addLine(card, 'Reported state', item.status || 'unknown');
      addLine(card, 'Last heartbeat', time(item.heartbeat_at));
      addLine(card, 'Last success', time(item.last_success_at));
    }
    const source = el('small', `Source: ${widget === 'allowances' ? 'manual observation' : 'Workbench'} · `, 'muted');
    const state = el('span', 'unknown');
    source.append(state);
    card.append(source);
    container.append(card);
    dynamic.push({widget, item, freshness: state, countdown});
  }
  async function load(widget, token) {
    const controller = new AbortController();
    pending.set(widget, controller);
    const section = document.querySelector(`[data-card="${widget}"]`);
    section.replaceChildren(el('h2', names[widget]), el('p', 'Checking…', 'muted'));
    try {
      const response = await fetch(`/api/status/${widget}`, {signal: controller.signal, cache: 'no-store', credentials: 'same-origin'});
      if (token !== generation || controller.signal.aborted) return;
      if (response.status === 401 || response.status === 403) { authenticationExpired(); return; }
      if (!response.ok) throw new Error(`Source unavailable (${response.status})`);
      const data = await response.json();
      if (token !== generation || !selected().includes(widget)) return;
      section.replaceChildren(el('h2', names[widget]));
      section.append(el('small', `${data.source} · checked ${time(data.checked_at)}`, 'muted'));
      if (!data.items.length) section.append(el('p', widget === 'allowances' ? 'No observations. Allowance is unknown.' :
        widget === 'hosts' ? 'No collector reports. Host visibility is unknown.' : 'No current items reported.'));
      for (const item of data.items) renderItem(widget, item, section);
      updateDynamic();
      if (data.truncated) section.append(el('p', 'More items exist; open full Workbench.'));
    } catch (error) {
      if (controller.signal.aborted || token !== generation) return;
      const message = !navigator.onLine ? 'Offline; no fresh status available.' :
        error instanceof TypeError ? 'Source unreachable; no fresh status available.' : error.message;
      section.replaceChildren(el('h2', names[widget]), el('p', message));
    } finally {
      if (pending.get(widget) === controller) pending.delete(widget);
    }
  }
  function cancelRequests() {
    generation++;
    for (const request of pending.values()) request.abort();
    pending.clear();
    dynamic.length = 0;
  }
  function authenticationExpired() {
    cancelRequests();
    profile = null;
    zone = deviceZone;
    cards.replaceChildren(el('p', 'Sign-in required. Open Workbench to sign in, then refresh.'));
    connection.textContent = 'Session expired; private status removed.';
  }
  async function refresh() {
    cancelRequests();
    const token = generation;
    const choice = selected();
    cards.replaceChildren();
    connection.textContent = navigator.onLine ? 'Foreground check requested.' : 'Offline; check again when connected.';
    if (!choice.length) { cards.append(el('p', 'No widgets selected. Choose a widget above.')); return; }
    if (!navigator.onLine) {
      for (const widget of choice) {
        const section = el('section', '');
        section.dataset.card = widget;
        section.replaceChildren(el('h2', names[widget]), el('p', 'Offline; no fresh status available.'));
        cards.append(section);
      }
      return;
    }
    const configController = new AbortController();
    pending.set('config', configController);
    try {
      const response = await fetch('/api/status/config', {signal: configController.signal, cache: 'no-store', credentials: 'same-origin'});
      if (token !== generation || configController.signal.aborted) return;
      if (response.status === 401 || response.status === 403) { authenticationExpired(); return; }
      if (!response.ok) throw new Error('Configuration unavailable');
      const config = await response.json();
      if (token !== generation) return;
      if (profile !== null && profile !== config.profile) connection.textContent = 'Profile changed; previous status cleared.';
      profile = config.profile;
      zone = config.timezone;
    } catch (error) {
      if (configController.signal.aborted || token !== generation) return;
      zone = deviceZone;
      connection.textContent = 'Time zone unavailable; showing device time. Status may be unavailable.';
    } finally {
      if (pending.get('config') === configController) pending.delete('config');
    }
    if (token !== generation) return;
    for (const widget of choice) {
      const section = el('section', '');
      section.dataset.card = widget;
      cards.append(section);
      load(widget, token);
    }
  }
  const initial = readChoices();
  for (const box of boxes) box.checked = initial.includes(box.dataset.widget);
  for (const box of boxes) box.addEventListener('change', () => { saveChoices(selected()); refresh(); });
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('defaults').addEventListener('click', () => {
    for (const box of boxes) box.checked = defaults.includes(box.dataset.widget);
    saveChoices(defaults); refresh();
  });
  window.addEventListener('online', () => { updateDynamic(); connection.textContent = 'Online. Refresh to check current status.'; });
  window.addEventListener('offline', refresh);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      cancelRequests();
      cards.replaceChildren(el('p', 'View hidden; status cleared until resume.'));
    } else refresh();
  });
  window.addEventListener('pageshow', event => { if (event.persisted) refresh(); });
  window.setInterval(() => { if (!document.hidden) updateDynamic(); }, 30000);
  const updateButton = document.getElementById('app-update');
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/status/sw.js', {scope: '/status/'}).then(registration => {
      updateButton.hidden = false;
      updateButton.textContent = registration.waiting ? 'Apply app update' : 'Check for app update';
      registration.addEventListener('updatefound', () => {
        registration.installing?.addEventListener('statechange', () => {
          if (registration.waiting) {
            updateButton.textContent = 'Apply app update';
            connection.textContent = 'App update ready. Apply it when convenient.';
          }
        });
      });
      let applying = false;
      navigator.serviceWorker.addEventListener('controllerchange', () => { if (applying) window.location.reload(); });
      updateButton.addEventListener('click', async () => {
        if (registration.waiting) {
          applying = true;
          registration.waiting.postMessage('ACTIVATE_UPDATE');
        } else {
          try {
            await registration.update();
            if (registration.installing) {
              await new Promise(resolve => {
                const worker = registration.installing;
                if (['installed', 'activated', 'redundant'].includes(worker.state)) return resolve();
                worker.addEventListener('statechange', () => {
                  if (['installed', 'activated', 'redundant'].includes(worker.state)) resolve();
                });
              });
            }
            updateButton.textContent = registration.waiting ? 'Apply app update' : 'Check for app update';
            connection.textContent = registration.waiting ? 'App update ready. Apply it when convenient.' : 'App is up to date.';
          } catch (_) { connection.textContent = 'App update check unavailable.'; }
        }
      });
    }).catch(() => { connection.textContent = 'Offline shell unavailable in this browser.'; });
  }
  refresh();
})();
