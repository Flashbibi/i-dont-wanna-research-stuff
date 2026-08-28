//
// Der Weg ohne AI-Client: URL eintragen, ehrlich scheitern, von Hand nachtragen.
//
// Deterministisch und ohne Netz zum Shop: die Instanz hat keinen Adapter für
// diese Domain, und AdapterFehlt greift vor jedem Abruf. Der Engine-Erfolgspfad
// bleibt hier bewusst aussen vor - er bräuchte eine echte Produktseite; ihn
// decken die Python-Tests mit MockTransport ab.
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_CORE || '/home/hermes/.local/share/beschaffung-browser/node_modules/playwright-core');
const baseUrl = process.env.BASE_URL || 'http://192.168.1.60:8000';
const markerHeaders = {'X-E2E-Marker': 'beschaffung-e2e-disposable'};
const executablePath = process.env.CHROME_BIN || '/home/hermes/.local/share/beschaffung-browser/chrome-linux64/chrome';
const profile = process.env.BROWSER_PROFILE || '/home/hermes/.local/share/beschaffung-browser/e2e-profile';
let testJob = null;
let context = null;
let evidence = null;

// Setup und Cleanup sind das Gerüst, nicht der Prüfgegenstand - und genau
// deshalb müssen sie laut scheitern. Ein blosses «HTTP 422» hat einmal einen
// halben Tag gekostet: die Antwort nannte den Grund, der Pfad druckte ihn nur
// nicht. Also erst Status und Körper zeigen, dann abbrechen.
async function erwarteAntwort(antwort, erwartet, was) {
  if (!antwort.ok) {
    const koerper = await antwort.text().catch(fehler => `<Körper nicht lesbar: ${fehler.message}>`);
    console.error(`${was}: HTTP ${antwort.status} ${antwort.statusText}`);
    console.error(`Antwortkörper: ${koerper}`);
    assert.fail(`${was}: HTTP ${antwort.status} - ${koerper}`);
  }
  assert.equal(antwort.status, erwartet, `${was}: unerwarteter HTTP ${antwort.status}`);
}

try {
  const setup = await fetch(`${baseUrl}/api/e2e/jobs`, {method: 'POST', headers: markerHeaders});
  await erwarteAntwort(setup, 201, 'Test-Job konnte nicht angelegt werden');
  testJob = await setup.json();
  assert.equal(testJob.marker, '[E2E-TEST]');
  const {job_id: jobId, line_ids: lineIds} = testJob;

  // Eine Produktseite auf der Domain eines bekannten Shops: der Shop wird
  // gefunden, ein Adapter für ihn gibt es nicht. Genau der Fall, den die
  // Handerfassung auffangen soll.
  const matrixResponse = await fetch(`${baseUrl}/api/jobs/${jobId}/scenarios`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tempo: 0.5}),
  });
  assert.equal(matrixResponse.status, 200);
  const matrix = await matrixResponse.json();
  const shopUrl = matrix.scenarios.flatMap(plan => plan.shops).map(shop => shop.url).find(Boolean);
  assert.ok(shopUrl, 'Kein Shop mit URL im Testjob gefunden');
  const produktUrl = new URL('/e2e-test-handerfassung', shopUrl).href;

  context = await chromium.launchPersistentContext(profile, {headless: true, executablePath});
  const page = await context.newPage();
  evidence = {
    baseUrl, testJobId: jobId, marker: testJob.marker, produktUrl,
    consoleErrors: [], failedRequests: [], badResponses: [],
    fetchResponses: [], recordResponses: [],
  };
  // Chromium spiegelt die erwartete 422 des Engine-Versuchs zusätzlich als
  // Konsolenfehler («Failed to load resource: ... 422 ...»). Genau dieses eine
  // Echo wird verworfen: die 422 ist weiter unten Prüfgegenstand, kein Befund -
  // sie hier zusätzlich als Konsolenfehler zu zählen, verlangte von derselben
  // Antwort gleichzeitig, dass sie kommt und dass sie nicht kommt. Jeder andere
  // Konsolenfehler bleibt scharf.
  const istErwartetesEcho = message =>
    (message.location()?.url || '').endsWith('/offers/fetch')
    && message.text().includes('422');
  page.on('console', message => {
    if (message.type() === 'error' && !istErwartetesEcho(message)) {
      evidence.consoleErrors.push(message.text());
    }
  });
  page.on('pageerror', error => evidence.consoleErrors.push(error.message));
  page.on('requestfailed', request => evidence.failedRequests.push({url: request.url(), error: request.failure()?.errorText}));
  page.on('response', response => {
    const url = response.url();
    // Die 422 des Engine-Versuchs ist hier das erwartete Ergebnis, kein Fehler.
    if (url.endsWith('/offers/fetch')) { evidence.fetchResponses.push(response.status()); return; }
    if (url.endsWith(`/lines/${lineIds[0]}/offers`)) { evidence.recordResponses.push(response.status()); return; }
    if (response.status() >= 400) evidence.badResponses.push({url, status: response.status()});
  });

  await page.goto(`${baseUrl}/jobs/${jobId}`, {waitUntil: 'networkidle'});
  await page.locator('#matrix-root .labels').first().waitFor();

  // Kandidatenansicht der ersten Position öffnen.
  const detail = page.locator('.detail');
  await page.locator(`.mc.rname[data-row="${lineIds[0]}"]`).click();
  await detail.waitFor();

  // Erfassungsblock aufklappen, URL eintragen, lesen lassen.
  await detail.getByRole('button', {name: 'Angebot per URL hinzufügen'}).click();
  await detail.locator(`textarea[data-add-urls="${lineIds[0]}"]`).fill(produktUrl);
  await detail.getByRole('button', {name: 'Seiten lesen'}).click();

  // Der ehrliche Fehler: kein Adapter, der manuelle Weg steht offen.
  const fehler = detail.locator('.addrow .warn');
  await fehler.waitFor();
  const fehlertext = (await fehler.first().innerText()).trim();
  assert.match(fehlertext, /Adapter/, `Unerwartete Meldung: ${fehlertext}`);
  assert.match(fehlertext, /record_offer/, `Meldung nennt den Handweg nicht: ${fehlertext}`);
  evidence.engineError = fehlertext;

  // Von Hand nachtragen - die URL ist bereits übernommen.
  await detail.getByRole('button', {name: 'von Hand nachtragen'}).click();
  const form = detail.locator('form.manualform');
  await form.waitFor();
  assert.equal(await form.locator('input[name="produkt_url"]').inputValue(), produktUrl);
  await form.getByText('wörtlich von der Seite').waitFor();
  evidence.manualFormPrefilled = true;

  await form.locator('input[name="produktname"]').fill('[E2E-TEST] Handerfassung');
  await form.locator('input[name="preis"]').fill('3.30');
  await form.locator('select[name="waehrung"]').selectOption('CHF');
  await form.locator('input[name="lieferzeit_text"]').fill('2 Testtage ab Testlager');
  await form.locator('input[name="lager_text"]').fill('E2E-Testbestand');
  const recorded = page.waitForResponse(response => response.url().endsWith(`/lines/${lineIds[0]}/offers`));
  await form.getByRole('button', {name: 'Angebot erfassen'}).click();
  assert.equal((await recorded).status(), 200);

  // Das Angebot steht in der Kandidatenliste - und trägt das Badge.
  const kandidat = page.locator('.cand').filter({hasText: '[E2E-TEST] Handerfassung'});
  await kandidat.first().waitFor();
  assert.equal(await kandidat.first().locator('.chip.unverified').count(), 1,
    'Handerfasstes Angebot muss als ungeprüft ausgewiesen sein');
  assert.equal((await kandidat.first().locator('.chip.unverified').innerText()).trim(), 'ungeprüft');
  evidence.unverifiedBadge = true;

  await page.reload({waitUntil: 'networkidle'});
  await page.locator('#matrix-root .labels').first().waitFor();
  await page.locator(`.mc.rname[data-row="${lineIds[0]}"]`).click();
  const persistiert = page.locator('.cand').filter({hasText: '[E2E-TEST] Handerfassung'});
  await persistiert.first().waitFor();
  assert.equal(await persistiert.first().locator('.chip.unverified').count(), 1);
  evidence.badgePersisted = true;

  // Der Knopf für den Auffrischlauf ist da; geklickt wird er hier nicht - er
  // ginge ans echte Netz.
  assert.equal(await page.locator('#check-prices').count(), 1);
  evidence.priceCheckPresent = true;

  assert.deepEqual(evidence.consoleErrors, []);
  assert.deepEqual(evidence.failedRequests, []);
  assert.deepEqual(evidence.badResponses, []);
  assert.deepEqual(evidence.fetchResponses, [422]);
  assert.deepEqual(evidence.recordResponses, [200]);
} finally {
  // Genau einmal, und im finally: bricht eine Assertion ab, ist die Evidence
  // das Einzige, woran sich der Lauf nachvollziehen lässt - im try gedruckt
  // wäre sie in genau dem Fall weg, in dem man sie braucht.
  if (evidence) console.log(JSON.stringify(evidence));
  if (context) await context.close();
  if (testJob) {
    const cleanup = await fetch(`${baseUrl}/api/e2e/jobs/${testJob.job_id}`, {method: 'DELETE', headers: markerHeaders});
    await erwarteAntwort(cleanup, 200, 'Test-Job-Cleanup fehlgeschlagen');
  }
}
