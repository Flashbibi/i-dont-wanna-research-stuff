(() => {
  const host = document.getElementById("job-matrix");
  if (!host) return;

  const jobId = Number(host.dataset.job);
  const state = { data: null, tempo: 0.5, detail: null, checks: new Set(), ordered: host.dataset.status === "bestellt" };
  const esc = (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const chf = (value) => Number(value).toFixed(2);
  const sameAssignments = (a, b) => JSON.stringify(a || {}) === JSON.stringify(b || {});
  const json = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    return payload;
  };

  const columns = () => state.data ? [
    ...state.data.scenarios,
    ...(state.data.custom ? [state.data.custom] : []),
  ] : [];
  const active = () => columns().find((column) => sameAssignments(column.assignments, state.data.selected_assignments)) || columns()[0] || null;
  const lineOffer = (column, lineId) => column.lines.find((line) => Number(line.line_id) === Number(lineId)) || null;
  const daysText = (plan) => {
    if (plan.contains_unknown_delivery) return "Lieferzeit unbekannt";
    if (plan.max_liefertage == null) return "Lieferzeit unbekannt";
    return `max. ${plan.max_liefertage} ${plan.max_liefertage === 1 ? "Tag" : "Tage"}${plan.max_delivery_only_estimated ? " (geschätzt)" : ""}`;
  };
  const chip = (line) => {
    if (!line || line.lieferzeit_tage == null) return '<span class="chip unk">Lieferzeit unbekannt</span>';
    const kind = line.lieferzeit_geschaetzt || line.lieferzeit_bedingt ? "est" : "ok";
    return `<span class="chip ${kind}">${esc(line.lieferzeit_chip)}</span>`;
  };
  const classFor = (column, index, extra = "") => {
    const selected = active();
    return `${extra}${selected && sameAssignments(column.assignments, selected.assignments) ? " selcol" : ""}${index === columns().length - 1 ? " lastcol" : ""}`;
  };

  async function load() {
    state.data = await json(`/api/jobs/${jobId}/scenarios`, {
      method: "POST",
      body: JSON.stringify({ tempo: state.tempo }),
    });
    state.checks.clear();
    render();
  }

  function renderProgress() {
    const progress = document.getElementById("progress-view");
    const matrix = document.getElementById("matrix-view");
    if (state.data.ready) {
      progress.hidden = true;
      matrix.hidden = false;
      return false;
    }
    matrix.hidden = true;
    progress.hidden = false;
    const required = state.data.lines.filter((line) => line.required);
    progress.innerHTML = `<h2>Job in Erfassung</h2>
      <p>Bestellpläne erscheinen, sobald jede Position Kandidaten hat oder mit <code>mark_line</code> markiert ist.</p>
      <div class="progress-list">${required.map((line) => `<div class="progress-line"><b>Position ${line.position}: ${esc(line.suchtext)}</b><div class="note">${line.candidates.filter((candidate) => !candidate.excluded).length} Kandidaten · ${esc(line.status)}</div></div>`).join("") || '<div class="progress-line">Noch keine offenen Positionen erfasst.</div>'}</div>`;
    document.getElementById("railsum").innerHTML = '<h2>Gewählter Plan</h2><p class="note">Noch kein Plan verfügbar.</p>';
    document.getElementById("railorder").innerHTML = '<h2>Bestellen</h2><p class="note">Keine Pläne über halbfertige Jobs.</p>';
    return true;
  }

  function badges(plan) {
    return [
      plan.contains_estimates ? '<span class="bdg">enthält Schätzungen</span>' : "",
      plan.contains_unknown_delivery ? '<span class="bdg unk">Lieferzeit unbekannt</span>' : "",
      plan.lines.some((line) => line.lieferzeit_bedingt) ? '<span class="bdg">bedingte Lieferzeit</span>' : "",
    ].join("");
  }

  function renderMatrix() {
    const root = document.getElementById("matrix-root");
    const cols = columns();
    const selected = active();
    const full = cols.find((column) => !column.incomplete);
    root.style.gridTemplateColumns = `230px repeat(${cols.length},minmax(210px,1fr))`;
    let html = `<div class="mc chead"><h2>Bestellpläne im Vergleich</h2><div class="note" style="margin-top:6px">Alle Pläne werden aus denselben ${state.data.lines.reduce((sum, line) => sum + line.candidates.length, 0)} Angeboten gerechnet.</div></div>`;

    html += cols.map((column, index) => {
      const chosen = sameAssignments(column.assignments, selected && selected.assignments);
      let warning = "";
      if (column.incomplete) {
        const missing = state.data.lines.filter((line) => column.missing_line_ids.includes(line.line_id)).map((line) => line.suchtext).join(", ");
        const extra = full ? Number(full.total_chf) - Number(column.total_chf) : 0;
        warning = `<div class="warn">deckt ${esc(missing)} nicht ab${full ? ` — <a href="#" data-fix="${esc(full.key)}">Vollplan wählen (+CHF ${chf(extra)})</a>` : ""}</div>`;
      }
      return `<div class="mc chead${classFor(column, index)}">
        <div class="labels">${column.labels.map(esc).join(" · ")}</div>
        <div class="total"><small>CHF</small> ${chf(column.total_chf)}</div>
        <div class="days">${daysText(column)} · ${column.shop_count} Shop${column.shop_count === 1 ? "" : "s"}</div>
        ${warning}<div class="badges">${badges(column)}</div>
        <div class="act">${chosen ? '<span class="chosenmark">✓ Gewählt</span>' : `<button class="btn small" data-select="${esc(column.key)}">Diesen Plan wählen</button>`}</div>
      </div>`;
    }).join("");

    state.data.lines.filter((line) => line.required).forEach((line) => {
      const isOpen = state.detail === line.line_id;
      const candidates = line.candidates.filter((candidate) => !candidate.excluded);
      html += `<div class="mc rname" data-row="${line.line_id}"><div class="no">Position ${line.position}</div><div class="nm">${esc(line.suchtext)}</div><div class="hint">${isOpen ? "Kandidaten ausblenden ▴" : `${candidates.length} Kandidaten ▾`}</div></div>`;
      html += cols.map((column, index) => {
        const item = lineOffer(column, line.line_id);
        if (!item) return `<div class="mc pcell missing${classFor(column, index)}">nicht verfügbar</div>`;
        const selectedItem = selected ? lineOffer(selected, line.line_id) : null;
        const differs = selected && !sameAssignments(column.assignments, selected.assignments) && selectedItem && selectedItem.offer_id !== item.offer_id;
        return `<div class="mc pcell${differs ? " diff" : ""}${classFor(column, index)}" data-row="${line.line_id}">
          <div class="prod" title="${esc(item.produktname)}">${esc(item.produktname)}</div>
          <div class="l2"><span class="price">CHF ${chf(item.einzelpreis_chf)}</span>${chip(item)}${item.pinned ? '<span class="chip pin">gepinnt</span>' : ""}</div>
        </div>`;
      }).join("");
      if (isOpen) html += renderDetail(line, selected);
    });

    html += '<div class="mc foot"><b style="color:var(--ink);font-size:12.5px">Versand</b></div>';
    html += cols.map((column, index) => `<div class="mc foot${classFor(column, index)}">${column.shops.map((shop) => `${esc(shop.name.split(" ")[0])} ${Number(shop.shipping_chf) === 0 ? '<span class="free">gratis</span>' : `CHF ${chf(shop.shipping_chf)}`}`).join(" + ")}</div>`).join("");
    root.innerHTML = html;
  }

  function renderDetail(line, selected) {
    const candidates = line.candidates.filter((candidate) => !candidate.excluded);
    const chosen = selected ? lineOffer(selected, line.line_id) : null;
    const rows = candidates.map((candidate) => {
      const inPlan = chosen && chosen.offer_id === candidate.offer_id;
      const provenance = `Produktseite: «${esc(candidate.lieferzeit_text || "Lieferzeit nicht angegeben") }» · Lager: ${esc(candidate.lager_text || "Lagerstatus nicht angegeben")}`;
      const action = candidate.pinned
        ? `<button class="btn small" type="submit" name="status" value="neutral" data-decision="neutral" data-offer="${candidate.offer_id}">Pin lösen</button>`
        : `<button class="btn small" type="submit" name="status" value="pin" data-decision="pin" data-offer="${candidate.offer_id}">Pinnen</button>`;
      return `<div class="cand" id="candidate-${candidate.offer_id}">
        <div class="l1">${inPlan ? '<span class="chip ok">im Plan</span>' : ""}${candidate.pinned ? '<span class="chip pin">gepinnt</span>' : ""}<span class="prod">${esc(candidate.produktname)}</span><span class="shop">· ${esc(candidate.shop_name)}</span><span class="price">CHF ${chf(candidate.preis_chf)}</span>${chip(candidate)}</div>
        <div class="src">${provenance} · <a href="${esc(candidate.quelle_url)}" target="_blank" rel="noopener">Seite öffnen ↗</a></div>
        <form class="l3" method="post" action="/offers/${candidate.offer_id}/decision"><input type="hidden" name="job_id" value="${jobId}">${action}<button class="btn small" type="submit" name="status" value="exclude" data-decision="exclude" data-offer="${candidate.offer_id}" ${candidate.last_candidate ? 'disabled title="letzter Kandidat dieser Zeile"' : ""}>Ausschliessen</button><span class="deltatx" data-delta="${candidate.offer_id}">${inPlan ? "im gewählten Plan enthalten" : "wird berechnet …"}</span></form>
      </div>`;
    }).join("");
    return `<div class="detail"><h3>Kandidaten für «${esc(line.suchtext)}» — Pinnen legt das Produkt für alle Pläne fest</h3>${rows}<div class="closebar"><button class="linklike" data-row="${line.line_id}">Schliessen</button></div></div>`;
  }

  async function fillDeltas() {
    if (state.detail == null) return;
    const selected = active();
    if (!selected) return;
    const line = state.data.lines.find((item) => item.line_id === state.detail);
    if (!line) return;
    await Promise.all(line.candidates.filter((candidate) => !candidate.excluded && selected.assignments[String(line.line_id)] !== candidate.offer_id).map(async (candidate) => {
      const node = document.querySelector(`[data-delta="${candidate.offer_id}"]`);
      if (!node) return;
      try {
        const delta = await json(`/api/jobs/${jobId}/lines/${line.line_id}/delta`, {
          method: "POST",
          body: JSON.stringify({ offer_id: candidate.offer_id, base_assignments: selected.assignments, tempo: state.tempo }),
        });
        const amount = Number(delta.delta_chf);
        const max = delta.contains_unknown_delivery ? "Lieferzeit unbekannt" : `max. ${delta.max_liefertage} ${delta.max_liefertage === 1 ? "Tag" : "Tage"}`;
        node.innerHTML = `würde ergeben: <b>${amount >= 0 ? "+" : "−"}CHF ${chf(Math.abs(amount))}</b> · ${max}`;
      } catch (error) {
        node.textContent = "in diesem Plan nicht verfügbar";
      }
    }));
  }

  function renderTempo() {
    document.getElementById("pct").textContent = `${Math.round(state.tempo * 100)} %`;
    document.getElementById("verdict").innerHTML = state.data.custom_verdict
      ? esc(state.data.custom_verdict).replace(/^(Ändert bei diesem Angebots-Pool nichts: )([^ ]+(?: [^ ]+)?)/, "$1<b>$2</b>")
      : state.data.custom ? "Deine Gewichtung ergibt einen eigenen Plan — neue Spalte «Eigene Gewichtung»." : "";
  }

  function renderOpenLines() {
    const box = document.getElementById("open-lines");
    if (!state.data.open_lines.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = `<h2>Offen</h2>${state.data.open_lines.map((line) => `<div class="open-line"><b>Position ${line.position}: ${esc(line.suchtext)}</b><div class="note">${esc(line.status)}${line.kommentar ? ` · ${esc(line.kommentar)}` : ""}</div></div>`).join("")}`;
  }

  function renderRail() {
    const selected = active();
    const summary = document.getElementById("railsum");
    const order = document.getElementById("railorder");
    if (!selected) { summary.innerHTML = order.innerHTML = ""; return; }
    summary.innerHTML = `<h2>Gewählter Plan</h2><div class="big"><small>CHF</small> ${chf(selected.total_chf)}</div>
      <div class="rrow"><span class="k">Plan</span><span>${selected.labels.map(esc).join(" · ")}</span></div>
      <div class="rrow"><span class="k">Lieferzeit</span><span>${daysText(selected)}</span></div>
      <div class="rrow"><span class="k">Shops</span><span>${selected.shop_count}</span></div>
      ${selected.contains_estimates ? '<div class="note">Geschätzte Zeiten stammen aus Shop-Standards — echte Zeiten lernt das Tool, sobald du Lieferungen mit «Angekommen» bestätigst.</div>' : ""}`;

    if (state.ordered) {
      order.innerHTML = '<h2>Bestellung</h2><div class="okmsg" style="margin-top:8px">Bestellung erfasst. Shops, Positionen und zugesagte Lieferzeiten sind gespeichert (record_purchase).</div><div class="note">Wenn die Pakete da sind: in der Historie «Angekommen» drücken.</div>';
      return;
    }
    const shopRows = selected.shops.map((shop) => {
      const checked = state.checks.has(String(shop.id));
      const threshold = shop.free_shipping_from_chf == null ? "" : (() => {
        const target = Number(shop.free_shipping_from_chf);
        const subtotal = Number(shop.subtotal_chf);
        const reached = subtotal >= target;
        const pct = Math.min(100, subtotal / target * 100);
        const caption = reached ? `Gratisversand erreicht (ab CHF ${chf(target)})` : `noch CHF ${chf(target - subtotal)} bis Gratisversand (ab CHF ${chf(target)})`;
        return `<div class="meter"><div class="track"><div class="fill" style="width:${pct}%"></div></div><div class="cap">${caption}</div></div>`;
      })();
      const items = selected.lines.filter((line) => Number(line.shop_id) === Number(shop.id));
      const links = `<details><summary>Produktlinks öffnen ↗</summary><ul class="shop-links">${items.map((line) => `<li><a href="${esc(line.quelle_url)}" target="_blank" rel="noopener">${esc(line.produktname)} ↗</a></li>`).join("")}</ul></details>`;
      return `<div class="shoprow"><div class="top"><span class="nm">${esc(shop.name)}</span><span class="amt">CHF ${chf(Number(shop.subtotal_chf) + Number(shop.shipping_chf))}</span></div><div class="sub">${shop.item_count} Artikel · Versand ${Number(shop.shipping_chf) === 0 ? "gratis" : `CHF ${chf(shop.shipping_chf)}`}</div>${links}${threshold}<label><input type="checkbox" data-shop="${shop.id}" ${checked ? "checked" : ""}> bei ${esc(shop.name.split(" ")[0])} bestellt</label></div>`;
    }).join("");
    const allChecked = selected.shops.every((shop) => state.checks.has(String(shop.id)));
    order.innerHTML = `<h2>Bestellen</h2>${shopRows}<button class="cta" id="record-purchase" ${allChecked && !selected.incomplete ? "" : "disabled"}>Bestellung erfassen</button>${selected.incomplete ? '<div class="note">Unvollständige Pläne können nicht erfasst werden.</div>' : '<div class="note">Links öffnen, in jedem Shop bestellen, abhaken. Zahlung bleibt bei dir.</div>'}`;
  }

  function render() {
    document.getElementById("offer-count").textContent = state.data.lines.reduce((sum, line) => sum + line.candidates.length, 0);
    document.getElementById("shop-count").textContent = new Set(state.data.lines.flatMap((line) => line.candidates.map((candidate) => candidate.shop_id))).size;
    document.getElementById("ovcount").textContent = Object.keys(state.data.pins).length + state.data.excludes.length;
    if (renderProgress()) return;
    renderMatrix();
    renderTempo();
    renderOpenLines();
    renderRail();
    fillDeltas();
  }

  document.addEventListener("click", async (event) => {
    const select = event.target.closest("[data-select]");
    const fix = event.target.closest("[data-fix]");
    const row = event.target.closest("[data-row]");
    const decision = event.target.closest("[data-decision]");
    const purchase = event.target.closest("#record-purchase");
    try {
      if (select || fix) {
        event.preventDefault();
        const key = (select || fix).dataset.select || (select || fix).dataset.fix;
        const column = columns().find((item) => item.key === key);
        if (!column) return;
        await json(`/api/jobs/${jobId}/selection`, { method: "PUT", body: JSON.stringify({ assignments: column.assignments, tempo: state.tempo }) });
        state.data.selected_assignments = column.assignments;
        state.data.selected_key = column.key;
        state.checks.clear();
        render();
        return;
      }
      if (decision) {
        event.preventDefault();
        event.stopPropagation();
        decision.disabled = true;
        await json(`/api/offers/${decision.dataset.offer}/decision`, { method: "POST", body: JSON.stringify({ status: decision.dataset.decision }) });
        await load();
        return;
      }
      if (row) {
        state.detail = state.detail === Number(row.dataset.row) ? null : Number(row.dataset.row);
        render();
        return;
      }
      if (purchase && !purchase.disabled) {
        purchase.disabled = true;
        const selected = active();
        const promised = {};
        for (const shop of selected.shops) {
          const days = selected.lines.filter((line) => Number(line.shop_id) === Number(shop.id)).map((line) => line.lieferzeit_tage).filter((value) => value != null);
          if (!days.length) throw new Error(`Für ${shop.name} ist keine zugesagte Lieferzeit belegt.`);
          promised[String(shop.id)] = Math.max(...days);
        }
        await json(`/api/jobs/${jobId}/purchase`, { method: "POST", body: JSON.stringify({ variante: selected, zugesagt_liefertage_pro_shop: promised }) });
        state.ordered = true;
        const status = document.getElementById("jobstatus");
        status.textContent = "bestellt";
        status.classList.add("done");
        renderRail();
      }
    } catch (error) {
      alert(error.message);
      if (decision) decision.disabled = false;
      if (purchase) purchase.disabled = false;
    }
  });

  document.addEventListener("change", (event) => {
    if (!event.target.matches("[data-shop]")) return;
    event.target.checked ? state.checks.add(event.target.dataset.shop) : state.checks.delete(event.target.dataset.shop);
    renderRail();
  });

  let tempoTimer;
  document.getElementById("tempo").addEventListener("input", (event) => {
    state.tempo = Number(event.target.value) / 100;
    document.getElementById("pct").textContent = `${event.target.value} %`;
    clearTimeout(tempoTimer);
    tempoTimer = setTimeout(() => load().catch((error) => alert(error.message)), 250);
  });

  load().catch((error) => {
    document.getElementById("matrix-root").innerHTML = `<div class="mc chead"><h2>Bestellpläne im Vergleich</h2><div class="warn">${esc(error.message)}</div></div>`;
  });
})();
