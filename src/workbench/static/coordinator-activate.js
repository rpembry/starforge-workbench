/* The fragment is not sent in HTTP; exchange once, then erase it from history. */
(async () => {
  const status = document.getElementById('activation-status');
  const secret = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  if (!secret) {
    status.textContent = 'Open the one-use link from the local launcher.';
    return;
  }
  try {
    const response = await fetch('/activate', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({secret})
    });
    if (!response.ok) throw new Error('activation unavailable');
    location.replace('/coordinator');
  } catch {
    status.textContent = 'Activation expired or unavailable. Start a new explicit local session.';
  }
})();
