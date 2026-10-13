/* Read-only status hint. Never replaces forms, focus, or an in-progress request. */
(() => {
  const display = document.getElementById('coordinator-live-status');
  if (!display) return;
  const jobId = display.dataset.jobId;
  const shownVersion = Number(display.dataset.version);
  const initialUrl = location.href;
  let generation = 0;
  let active = null;

  async function poll() {
    const mine = ++generation;
    if (active) active.abort();
    active = new AbortController();
    try {
      const response = await fetch(`/ui/coordinator/jobs/${encodeURIComponent(jobId)}/status`, {
        credentials: 'same-origin', cache: 'no-store', signal: active.signal
      });
      if (mine !== generation || location.href !== initialUrl) return;
      if (response.status === 401 || response.status === 403) {
        display.textContent = 'Authentication expired. Reopen this page after signing in.';
        document.querySelectorAll('[data-coordinator-control] button').forEach(button => {
          button.disabled = true;
        });
        return;
      }
      if (!response.ok) throw new Error('status unavailable');
      const job = await response.json();
      if (mine !== generation || location.href !== initialUrl || job.id !== jobId) return;
      if (job.version !== shownVersion) {
        display.textContent = `New coordinator state is available (version ${job.version}). Refresh before a new action.`;
      } else {
        display.textContent = `Still showing version ${shownVersion}; visibility ${job.visibility}.`;
      }
    } catch (error) {
      if (error.name !== 'AbortError' && mine === generation && location.href === initialUrl) {
        display.textContent = 'Coordinator status is unavailable. Current worker state is unknown.';
      }
    }
  }
  let timer = setInterval(poll, 15000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  addEventListener('pageshow', event => {
    if (event.persisted) {
      if (timer === null) timer = setInterval(poll, 15000);
      poll();
    }
  });
  addEventListener('pagehide', () => {
    generation++;
    if (timer !== null) clearInterval(timer);
    timer = null;
    if (active) active.abort();
  });
  poll();
})();
