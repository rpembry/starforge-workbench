// Application-ephemeral live provider output. Entries exist only in this
// tab's memory: nothing here is written to localStorage, sessionStorage, or
// any server-side store, and each entry removes itself after a short time.
// A page reload starts empty again.
(function () {
  var container = document.getElementById('response-preview-feed');
  if (!container || typeof EventSource === 'undefined') return;

  var MAX_ENTRIES = 5;
  var LIFETIME_MS = 60000;
  var sessionId = container.getAttribute('data-registered-session-id');

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
    var source = new EventSource('/api/instructions/preview-stream');
    source.onmessage = function (message) {
      try {
        addEntry(JSON.parse(message.data));
      } catch (error) {
        // Malformed event; drop it rather than break the feed.
      }
    };
    source.onerror = function () {
      source.close();
      setTimeout(connect, 5000);
    };
  }

  connect();
})();
