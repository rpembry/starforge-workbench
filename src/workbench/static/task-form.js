'use strict';
const drafts = new WeakMap();
document.addEventListener('htmx:beforeRequest', (event) => {
  const form = event.detail.elt;
  if (form.id !== 'human-action-form') return;
  drafts.set(form, [form.elements.title.value, form.elements.details.value]);
});
document.addEventListener('htmx:afterRequest', (event) => {
  const form = event.detail.elt;
  if (form.id !== 'human-action-form') return;
  const draft = drafts.get(form);
  if (event.detail.successful) {
    // Do not discard a newer draft typed while the save was in flight.
    if (draft && draft[0] === form.elements.title.value && draft[1] === form.elements.details.value) {
      form.reset();
      form.elements.title.focus();
    }
  } else {
    document.getElementById('action-result').textContent = 'Task could not be saved. Your draft is preserved.';
  }
  drafts.delete(form);
});
