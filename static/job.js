const decisionLabel = {
  bestaetigt: 'Bestätigt',
  verworfen: 'Verworfen',
};

const decide = async (card, status) => {
  const feedback = card.querySelector('.decision-feedback');
  const buttons = card.querySelectorAll('[data-decision]');
  buttons.forEach(button => { button.disabled = true; });
  feedback.textContent = 'Speichert …';
  feedback.classList.remove('error');

  try {
    const response = await fetch(`/api/offers/${encodeURIComponent(card.dataset.offer)}/decision`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status}),
    });
    if (!response.ok) {
      throw new Error((await response.text()) || `HTTP ${response.status}`);
    }
    card.classList.remove('decision-offen', 'decision-bestaetigt', 'decision-verworfen');
    card.classList.add(`decision-${status}`);
    feedback.textContent = decisionLabel[status];
  } catch (error) {
    feedback.textContent = `Nicht gespeichert: ${error.message}`;
    feedback.classList.add('error');
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
};

document.addEventListener('submit', event => {
  const form = event.target.closest('.decision-form');
  if (!form) return;
  event.preventDefault();
  const status = event.submitter?.value;
  if (status) decide(form.closest('.swipe-card'), status);
});

document.querySelectorAll('.swipe-card').forEach(card => {
  let start = null;
  card.addEventListener('pointerdown', event => {
    if (event.target.closest('button, a, input')) return;
    start = event.clientX;
    if (card.setPointerCapture) card.setPointerCapture(event.pointerId);
  });
  card.addEventListener('pointerup', event => {
    if (start === null) return;
    const delta = event.clientX - start;
    start = null;
    if (Math.abs(delta) > 70) decide(card, delta > 0 ? 'bestaetigt' : 'verworfen');
  });
});

if (document.querySelector('.waiting')) setTimeout(() => location.reload(), 8000);
