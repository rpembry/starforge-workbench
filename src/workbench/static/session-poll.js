'use strict';

(function () {
  const list = document.getElementById('sessions-list');
  const detail = document.querySelector('main[data-session-id]');
  const indicator = document.getElementById('session-poll-state');
  if (!indicator || (!list && !detail)) return;

  let sequence = 0;
  let lastApplied = 0;
  let timer = null;
  let active = true;
  const requests = new Set();

  function announce(message) { indicator.textContent = message; }
  function publishStatus(value) {
    document.dispatchEvent(new CustomEvent('workbench:session-status', {detail: value}));
  }
  function text(selector, value) {
    const node = detail.querySelector(selector);
    if (node) node.textContent = value;
  }
  function listArticle(item) {
    const article = document.createElement('article');
    const heading = document.createElement('h2');
    const link = document.createElement('a');
    link.href = '/sessions/' + encodeURIComponent(item.id);
    link.textContent = item.display_name;
    heading.append(link);
    const meta = document.createElement('p');
    meta.className = 'meta';
    meta.textContent = item.host + ' · ' + item.provider;
    const status = document.createElement('p');
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = item.status_label;
    status.append(badge);
    const reason = document.createElement('p');
    reason.textContent = item.reason_label;
    const dates = document.createElement('small');
    dates.textContent = 'Last activity: ' + item.activity_label + ' · Last contact: ' + item.heartbeat_label;
    article.append(heading, meta, status, reason, dates);
    return article;
  }
  function applyList(payload) {
    if (!Array.isArray(payload.items)) throw new Error('invalid session list');
    const focused = document.activeElement && document.activeElement.closest('#sessions-list a');
    const focusedHref = focused && focused.getAttribute('href');
    const nodes = payload.items.map(listArticle);
    if (!nodes.length) {
      const article = document.createElement('article');
      const message = document.createElement('p');
      message.textContent = 'No sessions are registered yet. This does not mean no agents are running.';
      article.append(message);
      nodes.push(article);
    }
    list.replaceChildren(...nodes);
    if (focusedHref) {
      const replacement = [...list.querySelectorAll('a')].find(link => link.getAttribute('href') === focusedHref);
      if (replacement) replacement.focus({preventScroll: true});
    }
    const pager = document.querySelector('nav.pager');
    if (pager) {
      const focusedPage = pager.contains(document.activeElement) && document.activeElement.getAttribute('href');
      const limit = Number(list.dataset.limit);
      const offset = Number(list.dataset.offset);
      const links = [];
      for (const [label, nextOffset, shown] of [
        ['Previous', Math.max(0, offset - limit), offset > 0],
        ['Next', offset + limit, payload.has_next === true]]) {
        if (!shown) continue;
        const link = document.createElement('a');
        link.href = '/sessions?limit=' + limit + '&offset=' + nextOffset;
        link.textContent = label;
        links.push(link);
      }
      pager.replaceChildren(...links);
      if (focusedPage) {
        const replacement = links.find(link => link.getAttribute('href') === focusedPage);
        if (replacement) replacement.focus({preventScroll: true});
      }
    }
  }
  function applyDetail(item) {
    if (!item || item.id !== detail.dataset.sessionId) throw new Error('wrong session response');
    text('[data-session-host]', item.host);
    text('[data-session-provider]', item.provider);
    text('[data-status-label]', item.status_label);
    text('[data-reason-label]', item.reason_label);
    text('[data-activity-label]', item.activity_label);
    text('[data-heartbeat-label]', item.heartbeat_label);
    text('[data-send-explanation]', item.send_explanation);
    publishStatus({available: true, session: item});
    return item.visibility;
  }
  async function poll() {
    if (!active || document.hidden) return;
    const requestNumber = ++sequence;
    const controller = new AbortController();
    requests.add(controller);
    const deadline = setTimeout(() => controller.abort(), 30000);
    const current = () => active && requestNumber > lastApplied;
    const path = detail
      ? '/ui/sessions/' + encodeURIComponent(detail.dataset.sessionId) + '/status'
      : '/ui/sessions/status?limit=' + encodeURIComponent(list.dataset.limit) + '&offset=' + encodeURIComponent(list.dataset.offset);
    try {
      const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin',
        headers: {Accept: 'application/json'}, signal: controller.signal});
      if (!current()) return;
      if (response.status === 401 || response.status === 403) {
        lastApplied = requestNumber;
        announce('Authentication expired or access denied. Showing last known status; sign in again.');
        if (detail) publishStatus({available: false, authExpired: true});
        return;
      }
      if (!response.ok) throw new Error('status request failed');
      const payload = await response.json();
      if (!current()) return;
      const visibility = detail ? applyDetail(payload) : (applyList(payload), null);
      lastApplied = requestNumber;
      announce('Server status checked now. ' + (visibility ? 'Collector visibility: ' + visibility + '.' :
        'Session observations may still be stale or offline.'));
    } catch (_error) {
      if (!current()) return;
      lastApplied = requestNumber;
      announce('Status refresh unavailable. Showing last known status; send is paused until a fresh check succeeds.');
      if (detail) publishStatus({available: false, authExpired: false});
    } finally {
      clearTimeout(deadline);
      requests.delete(controller);
    }
  }
  function start() {
    active = true;
    if (timer) clearInterval(timer);
    poll();
    timer = setInterval(poll, 10000);
  }
  window.addEventListener('pagehide', () => {
    active = false;
    lastApplied = ++sequence;
    for (const controller of requests) controller.abort();
    if (timer) clearInterval(timer);
    timer = null;
  });
  window.addEventListener('pageshow', start);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
