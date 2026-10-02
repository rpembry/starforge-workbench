// Application-ephemeral live provider output. Entries exist only in this
// tab's memory: nothing here is written to localStorage, sessionStorage, or
// any server-side store, and each entry removes itself after a short time.
// A page reload starts empty again.
(function () {
  var container = document.getElementById('response-preview-feed');
  if (!container) return;
  var state = document.getElementById('response-preview-state');
  if (typeof EventSource === 'undefined') {
    if (state) state.textContent = 'Live preview unavailable in this browser. Missed output has no replay.';
    return;
  }

  var MAX_ENTRIES = 5;
  var LIFETIME_MS = 60000;
  var sessionId = container.getAttribute('data-registered-session-id');
  var source = null;
  var retry = null;
  var active = true;
  var connectedBefore = false;

  function status(message) { if (state) state.textContent = message; }

  function addEntry(event) {
    if (!event || typeof event.excerpt !== 'string' || !event.excerpt) return;
    // A session page must never display another session's response. The main
    // dashboard has no session scope and intentionally continues to show all
    // live previews available to the operator.
    if (sessionId && event.registered_session_id !== sessionId) return;
    var article = document.createElement('article');
    var badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = event.outcome === 'provider_response_error' ? 'error' : 'response';
    var text = document.createElement('p');
    text.textContent = event.excerpt;
    var meta = document.createElement('small');
    meta.className = 'muted';
    var shortId = typeof event.instruction_id === 'string' ? event.instruction_id.slice(0, 8) : 'unknown';
    meta.textContent = 'instruction ' + shortId + '… · live only, never stored';
    article.append(badge, document.createElement('br'), text, meta);
    container.prepend(article);
    while (container.children.length > MAX_ENTRIES) {
      container.removeChild(container.lastChild);
    }
    setTimeout(function () {
      if (article.parentNode) article.parentNode.removeChild(article);
    }, LIFETIME_MS);
  }

  function connect() {
    if (!active) return;
    status(connectedBefore ? 'Reconnecting live preview. Output missed during the gap cannot be replayed.' :
      'Connecting to live preview. Only future output will appear.');
    var stream = new EventSource('/api/instructions/preview-stream');
    source = stream;
    stream.onopen = function () {
      if (!active || source !== stream) return;
      connectedBefore = true;
      status('Live preview connected. Only future output appears; this is not task completion evidence.');
    };
    stream.onmessage = function (message) {
      if (!active || source !== stream) return;
      try {
        addEntry(JSON.parse(message.data));
      } catch (error) {
        // Malformed event; drop it rather than break the feed.
      }
    };
    stream.onerror = function () {
      stream.close();
      if (!active || source !== stream) return;
      source = null;
      status('Live preview disconnected. Reconnecting; missed output cannot be replayed.');
      if (active) retry = setTimeout(connect, 5000);
    };
  }

  connect();
  window.addEventListener('pagehide', function () {
    active = false;
    if (source) source.close();
    if (retry) clearTimeout(retry);
    source = null;
    retry = null;
  });
  window.addEventListener('pageshow', function (event) {
    if (!active && event.persisted) {
      active = true;
      connect();
    }
  });
})();
