(() => {
  'use strict';
  const names = {attention: 'Attention', jobs: 'Jobs', hosts: 'Collectors', allowances: 'Allowances'};
  const defaults = ['attention', 'jobs', 'hosts', 'allowances'];
  const boxes = [...document.querySelectorAll('[data-widget]')];
  const cards = document.getElementById('cards');
  const connection = document.getElementById('connection');
  const pending = new Map();
  let generation = 0;
  const zone = document.body.dataset.timezone;
  const time = value => {
    if (!value) return 'unknown';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'unknown' : new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium', timeStyle: 'short', timeZone: zone, timeZoneName: 'short'
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
  function addLine(card, label, value) { card.append(el('p', `${label}: ${value}`)); }
  function renderItem(widget, item, container) {
    const card = el('div', '', 'card');
    if (widget === 'allowances') {
      card.append(el('strong', `${item.provider} · ${item.product} · ${item.profile}`));
      addLine(card, 'Bucket / window', `${item.bucket} / ${item.window}`);
      addLine(card, 'Remaining', item.remaining_percent === null ? 'unknown' : `${item.remaining_percent}% (manual)`);
      addLine(card, 'Reset', item.reset_at ? time(item.reset_at) : 'unknown');
      if (item.reset_at) {
        const delta = new Date(item.reset_at).getTime() - Date.now();
        addLine(card, 'Countdown', delta <= 0 ? 'refresh due; awaiting new evidence' : `${Math.ceil(delta / 60000)} minutes`);
      }
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
    card.append(el('small', `Source: ${widget === 'allowances' ? 'manual observation' : 'Workbench'} · ${item.freshness || 'current status unknown'}`, 'muted'));
    container.append(card);
  }
  async function load(widget, token) {
    const controller = new AbortController();
    pending.set(widget, controller);
    const section = document.querySelector(`[data-card="${widget}"]`);
    section.replaceChildren(el('h2', names[widget]), el('p', 'Checking…', 'muted'));
    try {
      const response = await fetch(`/api/status/${widget}`, {signal: controller.signal, cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error(response.status === 401 ? 'Sign in again through Workbench' : `Source unavailable (${response.status})`);
      const data = await response.json();
      if (token !== generation || !selected().includes(widget)) return;
      section.replaceChildren(el('h2', names[widget]));
      section.append(el('small', `${data.source} · checked ${time(data.checked_at)}`, 'muted'));
      if (!data.items.length) section.append(el('p', widget === 'allowances' ? 'No observations. Allowance is unknown.' :
        widget === 'hosts' ? 'No collector reports. Host visibility is unknown.' : 'No current items reported.'));
      for (const item of data.items) renderItem(widget, item, section);
      if (data.truncated) section.append(el('p', 'More items exist; open full Workbench.'));
    } catch (error) {
      if (controller.signal.aborted || token !== generation) return;
      section.replaceChildren(el('h2', names[widget]), el('p', navigator.onLine ? error.message : 'Offline; no fresh status available.'));
    } finally {
      if (pending.get(widget) === controller) pending.delete(widget);
    }
  }
  function refresh() {
    generation++;
    for (const request of pending.values()) request.abort();
    pending.clear();
    const choice = selected();
    cards.replaceChildren();
    connection.textContent = navigator.onLine ? 'Foreground check requested.' : 'Offline; check again when connected.';
    if (!choice.length) { cards.append(el('p', 'No widgets selected. Choose a widget above.')); return; }
    for (const widget of choice) {
      const section = el('section', '');
      section.dataset.card = widget;
      cards.append(section);
      if (navigator.onLine) load(widget, generation);
      else section.replaceChildren(el('h2', names[widget]), el('p', 'Offline; no fresh status available.'));
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
  window.addEventListener('online', () => { connection.textContent = 'Online. Refresh to check current status.'; });
  window.addEventListener('offline', refresh);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) connection.textContent = 'View resumed. Refresh to check current status.'; });
  refresh();
})();
