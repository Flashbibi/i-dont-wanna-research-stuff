const slider = document.querySelector('#tempo');
const target = document.querySelector('#variants');
const esc = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
let timer;
async function loadVariants() {
  const tempo = slider.value;
  document.querySelector('#tempo-value').textContent = `${Math.round(tempo * 100)}%`;
  const response = await fetch(`/api/jobs/${slider.dataset.job}/variants?tempo=${tempo}`);
  if (!response.ok) { target.innerHTML = `<p class="muted">${esc(await response.text())}</p>`; return; }
  const variants = await response.json();
  if (!variants.length) { target.innerHTML = '<section class="panel"><strong>Noch keine vollständige Variante.</strong><p class="muted">Für jede Zeile muss mindestens ein Kandidat bestätigt sein.</p></section>'; return; }
  target.innerHTML = variants.map((variant, index) => `
    <section class="panel variant" data-index="${index}">
      <h2><span>Variante ${index + 1}</span><span>CHF ${esc(variant.total_chf)}</span></h2>
      <p class="muted">Maximal ${esc(variant.max_liefertage)} Liefertage · ${variant.shop_ids.length} Shop(s)</p>
      <div class="variant-grid"><div><h3>Zuordnung</h3><ul>${variant.lines.map(line => `<li>${esc(line.menge)}× <a href="${esc(line.produkt_url)}" target="_blank" rel="noopener">${esc(line.produktname)}</a></li>`).join('')}</ul></div>
      <div><h3>Shops</h3><ul>${variant.shops.map(shop => `<li><a href="${esc(shop.url)}" target="_blank" rel="noopener">${esc(shop.name)}</a>: CHF ${esc(shop.subtotal_chf)} + ${esc(shop.versand_chf)} Versand</li>`).join('')}</ul></div></div>
      <div class="promises">${variant.shops.map(shop => `<label>${esc(shop.name)} Tage <input type="number" min="1" value="${esc(variant.max_liefertage)}" data-shop="${shop.id}"></label>`).join('')}</div>
      <button class="primary order-button">Bestellt</button>
    </section>`).join('');
  document.querySelectorAll('.order-button').forEach((button, index) => button.addEventListener('click', () => recordPurchase(button, variants[index])));
}
async function recordPurchase(button, variant) {
  if (!confirm('Diese Bestellung wirklich als bestellt erfassen?')) return;
  const panel = button.closest('.variant');
  const promised = {};
  panel.querySelectorAll('[data-shop]').forEach(input => promised[input.dataset.shop] = Number(input.value));
  const response = await fetch(`/api/jobs/${slider.dataset.job}/purchase`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({variante:variant, zugesagt_liefertage_pro_shop:promised})});
  if (!response.ok) { alert(await response.text()); return; }
  location.href = '/history';
}
slider.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(loadVariants, 120); });
loadVariants();
