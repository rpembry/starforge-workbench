'use strict';

const form = document.querySelector('[data-instruction-form]');
if (form) {
  const text = form.querySelector('textarea');
  const counter = form.querySelector('output');
  const button = form.querySelector('button[type="submit"]');
  let submitting = false;
  const update = () => { counter.textContent = String(Array.from(text.value).length); };
  text.addEventListener('input', update);
  window.addEventListener('pageshow', () => { submitting = false; button.textContent = 'Queue instruction'; update(); });
  form.addEventListener('submit', (event) => {
    if (submitting) {
      event.preventDefault();
      return;
    }
    if (form.checkValidity()) {
      submitting = true;
      button.textContent = 'Queuing instruction...';
    }
  });
  update();
}
