const decide = async (card, status) => {
  const response = await fetch(`/api/offers/${card.dataset.offer}/decision`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status})
  });
  if (!response.ok) { alert(await response.text()); return; }
  card.classList.remove('decision-offen','decision-bestaetigt','decision-verworfen');
  card.classList.add(`decision-${status}`);
};
document.querySelectorAll('.swipe-card').forEach(card => {
  card.querySelectorAll('[data-decision]').forEach(button => button.addEventListener('click', () => decide(card, button.dataset.decision)));
  let start = null;
  card.addEventListener('pointerdown', event => { start = event.clientX; card.setPointerCapture(event.pointerId); });
  card.addEventListener('pointerup', event => {
    if (start === null) return;
    const delta = event.clientX - start; start = null;
    if (Math.abs(delta) > 70) decide(card, delta > 0 ? 'bestaetigt' : 'verworfen');
  });
});
if (document.querySelector('.waiting')) setTimeout(() => location.reload(), 8000);
