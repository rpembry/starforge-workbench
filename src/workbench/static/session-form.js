'use strict';

(function () {
  const form = document.querySelector('[data-instruction-form]');
  if (!form) return;
  const target = form.dataset.registeredSessionId;
  const text = form.querySelector('textarea');
  const expiry = form.querySelector('select');
  const confirmation = form.querySelector('input[type="checkbox"]');
  const keyField = form.querySelector('input[name="idempotency_key"]');
  const counter = form.querySelector('output');
  const sendButton = form.querySelector('button[type="submit"]');
  const checkButton = form.querySelector('[data-check-attempt]');
  const retryButton = form.querySelector('[data-retry-attempt]');
  const newButton = form.querySelector('[data-new-attempt]');
  const message = document.getElementById('instruction-attempt-state');
  const confirmationLabel = form.querySelector('.confirmation label');
  const storageKey = 'workbench:instruction-draft:' + target;
  let attempt = null;
  let eligible = false;
  let sending = false;
  let reconciling = false;
  let authExpired = false;
  let storageAvailable = true;

  function announce(value) { message.textContent = value; }
  function count() { counter.textContent = String(Array.from(text.value).length); }
  function sameEnvelope() {
    return attempt && attempt.target === target && attempt.text === text.value &&
      attempt.expiry_minutes === Number(expiry.value);
  }
  function matchingRecord(record) {
    return record.idempotency_key === attempt.key && record.registered_session_id === attempt.target &&
      Number(record.expiry_minutes) === attempt.expiry_minutes &&
      (record.text === attempt.text || record.text === attempt.text.trim());
  }
  function save() {
    try {
      const snapshot = JSON.stringify({text: text.value, expiry: expiry.value,
        confirmed: confirmation.checked, attempt, caretStart: text.selectionStart,
        caretEnd: text.selectionEnd, focused: document.activeElement === text});
      sessionStorage.setItem(storageKey, snapshot);
      if (sessionStorage.getItem(storageKey) !== snapshot) throw new Error('tab storage did not retain attempt');
      storageAvailable = true;
      return true;
    } catch (_error) {
      storageAvailable = false;
      return false;
    }
  }
  function render() {
    const unknown = attempt && attempt.phase === 'unknown';
    sendButton.hidden = Boolean(attempt);
    sendButton.disabled = !eligible || sending || Boolean(attempt) || !storageAvailable;
    checkButton.hidden = !unknown;
    checkButton.disabled = reconciling || sending || authExpired;
    retryButton.hidden = !unknown || !attempt.reconciledMissing || attempt.conflictSeen;
    retryButton.disabled = !eligible || sending || reconciling || !sameEnvelope() || !confirmation.checked || !storageAvailable;
    newButton.hidden = !attempt || unknown;
    newButton.disabled = sending || reconciling;
    count();
    save();
    sendButton.disabled = sendButton.disabled || !storageAvailable;
    retryButton.disabled = retryButton.disabled || !storageAvailable;
  }
  function restore() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!saved || typeof saved.text !== 'string' || saved.text.length > 2000) return;
      text.value = saved.text;
      if ([...expiry.options].some(option => option.value === saved.expiry)) expiry.value = saved.expiry;
      confirmation.checked = saved.confirmed === true;
      if (saved.attempt && saved.attempt.target === target && typeof saved.attempt.key === 'string' &&
          typeof saved.attempt.text === 'string' && Number.isInteger(saved.attempt.expiry_minutes)) {
        attempt = {...saved.attempt, phase: 'unknown', reconciledMissing: false};
        keyField.value = attempt.key;
        announce('Previous send attempt needs status reconciliation. Nothing will be sent automatically.');
      }
      if (saved.focused) {
        text.focus({preventScroll: true});
        text.setSelectionRange(saved.caretStart || 0, saved.caretEnd || 0);
      }
    } catch (_error) { /* A corrupt or unavailable tab store cannot authorize a send. */ }
  }
  async function reconcile() {
    if (!attempt || reconciling) return;
    reconciling = true;
    attempt.reconciledMissing = false;
    render();
    try {
      const response = await fetch('/api/instructions/by-key/' + encodeURIComponent(attempt.key),
        {cache: 'no-store', credentials: 'same-origin', headers: {Accept: 'application/json'}});
      if (response.status === 401 || response.status === 403) {
        authExpired = true;
        announce('Authentication expired. Send outcome is unknown; sign in again to check it.');
      } else if (response.status === 404) {
        attempt.phase = 'unknown';
        attempt.reconciledMissing = !attempt.conflictSeen;
        announce(attempt.conflictSeen
          ? 'This key is already in use but its record is unavailable to this sign-in. Outcome remains unknown; do not start another attempt.'
          : 'No record was found for this key yet. The send outcome is unknown. You may explicitly retry the same unchanged attempt.');
      } else if (!response.ok) {
        announce('Attempt status is unavailable. Outcome remains unknown; no retry is offered until it can be checked.');
      } else {
        const record = await response.json();
        if (!matchingRecord(record)) {
          attempt.phase = 'unknown';
          attempt.conflictSeen = true;
          announce('The key belongs to different instruction metadata. Outcome remains unknown; do not start another attempt.');
        } else {
          attempt.phase = 'accepted';
          announce('Instruction recorded as ' + record.state + '. Provider receipt and requested-work completion are separate.');
        }
      }
    } catch (_error) {
      announce('Attempt status is unavailable. Outcome remains unknown; no retry is offered until it can be checked.');
    } finally {
      reconciling = false;
      render();
    }
  }
  async function send() {
    if (sending || !eligible || !confirmation.checked || !form.checkValidity()) return;
    if (attempt && (!attempt.reconciledMissing || !sameEnvelope() || attempt.phase !== 'unknown')) return;
    const newAttempt = !attempt;
    if (newAttempt) {
      attempt = {key: keyField.value, target, text: text.value, expiry_minutes: Number(expiry.value),
        phase: 'unknown', reconciledMissing: false};
    }
    if (!save()) {
      if (newAttempt) attempt = null;
      announce(newAttempt
        ? 'Tab storage is unavailable. No instruction was sent. Keep this tab open and retry only after storage works.'
        : 'Tab storage is unavailable. This retry was not sent; the prior outcome remains unknown. Keep this tab open.');
      render();
      return;
    }
    sending = true;
    announce('Submitting this exact instruction attempt…');
    render();
    try {
      const response = await fetch('/api/instructions', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', Accept: 'application/json'},
        body: JSON.stringify({idempotency_key: attempt.key, registered_session_id: attempt.target,
          text: attempt.text, expiry_minutes: attempt.expiry_minutes})});
      if (response.ok) {
        const record = await response.json();
        if (!matchingRecord(record)) {
          announce('The response did not match this attempt. Outcome is unknown; checking the original key.');
          await reconcile();
        } else {
          attempt.phase = 'accepted';
          announce('Instruction queued for this exact session. Provider receipt and requested-work completion are not established.');
        }
      } else if (response.status === 409) {
        const problem = await response.json().catch(() => ({}));
        if (problem.error && problem.error.code === 'idempotency_conflict') {
          attempt.conflictSeen = true;
          announce('This key conflicts with an existing instruction. Checking the original attempt before any next action.');
          await reconcile();
        } else {
          attempt.phase = 'failed';
          announce('The server rejected this attempt because the target is unavailable or ineligible. Draft preserved.');
        }
      } else if (response.status >= 400 && response.status < 500 && response.status !== 401 && response.status !== 403) {
        attempt.phase = 'failed';
        announce('The server rejected this attempt. Draft preserved; no instruction was queued by this request.');
      } else {
        announce('Submission response was lost or ambiguous. Checking the original key; nothing will be sent automatically.');
        await reconcile();
      }
    } catch (_error) {
      announce('Submission response was lost or ambiguous. Checking the original key; nothing will be sent automatically.');
      await reconcile();
    } finally {
      sending = false;
      render();
    }
  }

  restore();
  render();
  for (const control of [text, expiry, confirmation]) {
    control.addEventListener('input', render);
    control.addEventListener('change', render);
  }
  form.addEventListener('submit', event => { event.preventDefault(); send(); });
  checkButton.addEventListener('click', reconcile);
  retryButton.addEventListener('click', send);
  newButton.addEventListener('click', () => {
    if (!attempt || attempt.phase === 'unknown') return;
    attempt = null;
    keyField.value = crypto.randomUUID();
    confirmation.checked = false;
    announce('New attempt ready. Confirm this exact session before sending.');
    render();
  });
  document.addEventListener('workbench:session-status', event => {
    if (!event.detail.available) {
      eligible = false;
      authExpired = Boolean(event.detail.authExpired);
    } else {
      const session = event.detail.session;
      authExpired = false;
      const changed = session.id !== target || session.display_name !== form.dataset.targetName ||
        session.host !== form.dataset.targetHost;
      if (changed || !session.send_allowed) confirmation.checked = false;
      form.dataset.targetName = session.display_name;
      form.dataset.targetHost = session.host;
      confirmationLabel.textContent = 'Confirm this exact session: ' + session.display_name + ' on ' + session.host + '.';
      eligible = session.id === target && session.send_allowed;
      if (attempt && attempt.phase === 'unknown' && !reconciling) reconcile();
    }
    render();
  });
  window.addEventListener('pagehide', save);
  window.addEventListener('pageshow', () => { if (attempt && attempt.phase === 'unknown') reconcile(); });
})();
