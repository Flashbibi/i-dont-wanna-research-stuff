const slider = document.querySelector('#tempo');
const scenariosTarget = document.querySelector('#scenarios');
const tunedTarget = document.querySelector('#variants');
const esc = value => String(value ?? '').replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
let timer;
let currentData;

const days = value => Number(value) === 1 ? '1 Tag' : `${esc(value)} Tage`;
const deliveryText = line => line.lieferzeit_text
  ? `${days(line.lieferzeit_tage)} · ${esc(line.lieferzeit_text)}`
  : `${days(line.lieferzeit_tage)} · Schätzung (Shop-Standard)`;

function assignmentRow(line, choices) {
  const assumption = line.assumption ? `
    <span class="assumption-chip">Annahme: ${esc(line.produktname)}</span>
    <button type="button" class="change-product" data-line="${line.line_id}">ändern</button>
    <select class="product-choice" data-line="${line.line_id}" hidden>
      ${(choices[String(line.line_id)] || []).map(choice => `<option value="${choice.offer_id}" ${choice.product_key === normalize(line.produktname) ? 'selected' : ''}>${esc(choice.produktname)} · ${esc(choice.shop_name)} · CHF ${esc(choice.preis_chf)}</option>`).join('')}
    </select>` : '';
  return `<li class="assignment-row">
    <div><strong>${esc(line.suchtext)}</strong>${assumption}</div>
    <a href="${esc(line.produkt_url)}" target="_blank" rel="noopener">${esc(line.produktname)}</a>
    <span>CHF ${esc(line.einzelpreis_chf)} · ${deliveryText(line)}</span>
  </li>`;
}

const normalize = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

function renderVariant(variant, choices, scenario = false) {
  const estimate = variant.contains_estimates ? '<span class="badge estimate">enthält Schätzungen</span>' : '';
  const missing = variant.missing_lines?.length
    ? `<p class="error"><strong>Fehlende Zeilen:</strong> ${variant.missing_lines.map(line => esc(line.suchtext || `#${line.line_id}`)).join(', ')}</p>`
    : '';
  return `<section class="panel variant scenario-card" data-key="${esc(variant.key || '')}">
    <div class="scenario-head"><div><p class="eyebrow">${scenario ? esc(variant.label) : 'Feineinstellung'}</p><h2>CHF ${esc(variant.total_chf)}</h2></div>${estimate}</div>
    <p class="muted">Max. ${days(variant.max_liefertage)} · ${esc(variant.shop_ids.length)} Shop(s)</p>
    ${missing}
    <details ${scenario ? '' : 'open'}><summary>Zuordnungen anzeigen</summary><ul class="assignment-list">${variant.lines.map(line => assignmentRow(line, choices)).join('')}</ul></details>
    <div class="shop-summary">${variant.shops.map(shop => `<span>${esc(shop.name)}: CHF ${esc(shop.subtotal_chf)} + ${esc(shop.versand_chf)} Versand</span>`).join('')}</div>
  </section>`;
}

async function loadScenarios() {
  const tempo = Number(slider.value);
  document.querySelector('#tempo-value').textContent = `${Math.round(tempo * 100)}%`;
  const response = await fetch(`/api/jobs/${slider.dataset.job}/scenarios`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tempo}),
  });
  if (!response.ok) {
    scenariosTarget.innerHTML = `<p class="error">${esc(await response.text())}</p>`;
    return;
  }
  currentData = await response.json();
  scenariosTarget.innerHTML = currentData.scenarios.length
    ? currentData.scenarios.map(item => renderVariant(item, currentData.choices, true)).join('')
    : '<section class="panel"><strong>Noch kein vollständiges Szenario.</strong></section>';
  tunedTarget.innerHTML = currentData.fine_tuned.length
    ? currentData.fine_tuned.map(item => renderVariant(item, currentData.choices, false)).join('')
    : '<p class="muted">Keine vollständige Variante für diese Feineinstellung.</p>';
  bindProductChanges();
}

function bindProductChanges() {
  document.querySelectorAll('.change-product').forEach(button => button.addEventListener('click', () => {
    const select = button.parentElement.querySelector('.product-choice');
    select.hidden = false;
    select.focus();
  }));
  document.querySelectorAll('.product-choice').forEach(select => select.addEventListener('change', async () => {
    select.disabled = true;
    const response = await fetch(`/api/offers/${select.value}/decision`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: 'pin'}),
    });
    if (!response.ok) {
      select.insertAdjacentHTML('afterend', `<span class="error">${esc(await response.text())}</span>`);
      select.disabled = false;
      return;
    }
    await loadScenarios();
  }));
}

slider.addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(loadScenarios, 120);
});
loadScenarios();
