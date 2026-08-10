/**
 * Klickpfad der Warenkorb-Übergabe gegen die LAN-Instanz.
 *
 * Der Shop wird serverseitig durch eine Attrappe ersetzt (`?stub=`), damit der
 * Lauf keinen echten Shop anfasst. Umgeschaltet wird erst hier im Test, nicht
 * in der Oberfläche - so durchläuft der Klick den echten Code inklusive
 * Zustandswechsel, Rendering und Rückverifikation.
 *
 * Der Stub-Weg schreibt nichts: weder Plattform noch Produkt-IDs.
 */
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

const waitForMatrix = async page => {
  await page.locator('#matrix-root .labels').first().waitFor();
  await page.waitForLoadState('networkidle');
};

try {
  const setup = await fetch(`${baseUrl}/api/e2e/jobs`, {method: 'POST', headers: markerHeaders});
  assert.equal(setup.status, 201, `Test-Job konnte nicht angelegt werden: HTTP ${setup.status}`);
  testJob = await setup.json();
  assert.equal(testJob.marker, '[E2E-TEST]');

  context = await chromium.launchPersistentContext(profile, {headless: true, executablePath});
  const page = await context.newPage();
  const {job_id: jobId} = testJob;
  const evidence = {
    baseUrl, testJobId: jobId, marker: testJob.marker,
    consoleErrors: [], failedRequests: [], cartResponses: [],
  };
  page.on('console', message => { if (message.type() === 'error') evidence.consoleErrors.push(message.text()); });
  page.on('pageerror', error => evidence.consoleErrors.push(error.message));
  page.on('requestfailed', request => evidence.failedRequests.push({url: request.url(), error: request.failure()?.errorText}));
  page.on('response', response => {
    if (response.url().includes('/cart') && !response.url().includes('cart-shops')) {
      evidence.cartResponses.push(response.status());
    }
  });

  // Die Oberfläche kennt den Stub nicht; der Test hängt ihn an.
  let stubMode = 'ok';
  await page.route('**/shops/*/cart', async route => {
    const url = new URL(route.request().url());
    url.searchParams.set('stub', stubMode);
    await route.continue({
      url: url.toString(),
      headers: {...route.request().headers(), ...markerHeaders},
    });
  });

  await page.goto(`${baseUrl}/jobs/${jobId}`, {waitUntil: 'networkidle'});
  await waitForMatrix(page);

  // Einen vollständigen Plan wählen, damit die Bestellspalte Shops zeigt.
  const complete = page.locator('#matrix-root .mc.chead').filter({has: page.locator('.labels')}).filter({hasNotText: 'Ein Shop'}).first();
  const choose = complete.getByRole('button', {name: 'Diesen Plan wählen'});
  if (await choose.count()) await choose.click();
  await complete.getByText('✓ Gewählt').waitFor();

  const shopRow = page.locator('#railorder .shoprow').first();
  const fillButton = shopRow.getByRole('button', {name: 'Warenkorb füllen'});
  await fillButton.waitFor();
  evidence.fillButtonVisible = true;

  // Erfolgsfall: Zustandswechsel, geprüfter Korb, Cookie-Übergabe.
  const okResponse = page.waitForResponse(response => response.url().includes('/cart?stub=ok'));
  await fillButton.click();
  assert.equal((await okResponse).status(), 200);
  const confirmation = shopRow.locator('.cartok');
  await confirmation.waitFor();
  const confirmationText = (await confirmation.innerText()).trim();
  assert.match(confirmationText, /^Korb geprüft: \d+ Artikel, CHF \d+\.\d{2} ✓$/, `Unerwartete Bestätigung: ${confirmationText}`);
  evidence.verifiedCartVisible = confirmationText;

  await shopRow.getByText('Die Session ist flüchtig', {exact: false}).waitFor();
  await shopRow.getByRole('button', {name: 'Kopieren'}).waitFor();
  const cookieValue = (await shopRow.locator('[data-cookie]').innerText()).trim();
  assert.ok(cookieValue.length > 0, 'Cookie-Wert fehlt in der Übergabe');
  assert.equal(await shopRow.getByRole('button', {name: 'Warenkorb füllen'}).count(), 0, 'Füllknopf bleibt nach Erfolg stehen');
  evidence.cookieHandoverVisible = true;

  // Nach Reload ist die flüchtige Session wieder weg und der Knopf zurück -
  // just-in-time gefüllt, nie auf Vorrat.
  await page.reload({waitUntil: 'networkidle'});
  await waitForMatrix(page);
  const rowAfterReload = page.locator('#railorder .shoprow').first();
  await rowAfterReload.getByRole('button', {name: 'Warenkorb füllen'}).waitFor();
  assert.equal(await rowAfterReload.locator('.cartok').count(), 0, 'Bestätigung darf einen Reload nicht überleben');
  evidence.consistentAfterReload = true;

  // Fehlerfall: geänderter Shop-Preis blockiert die Übergabe mit belegtem Diff.
  stubMode = 'mismatch';
  const badResponse = page.waitForResponse(response => response.url().includes('/cart?stub=mismatch'));
  await rowAfterReload.getByRole('button', {name: 'Warenkorb füllen'}).click();
  assert.equal((await badResponse).status(), 409);
  const diff = rowAfterReload.locator('.cartdiff');
  await diff.waitFor();
  const diffText = (await diff.innerText()).trim();
  assert.match(diffText, /Zwischensumme weicht ab: erfasst CHF \d+\.\d{2}, Korb CHF \d+\.\d{2}\./, `Unerwarteter Diff: ${diffText}`);
  assert.equal(await rowAfterReload.locator('[data-cookie]').count(), 0, 'Bei Abweichung darf kein Cookie erscheinen');
  await rowAfterReload.getByRole('button', {name: 'Nochmal versuchen'}).waitFor();
  evidence.mismatchDiffVisible = diffText;

  // Wiederholbar: derselbe Knopf führt nach Korrektur wieder zur Übergabe.
  stubMode = 'ok';
  await rowAfterReload.getByRole('button', {name: 'Nochmal versuchen'}).click();
  await rowAfterReload.locator('.cartok').waitFor();
  evidence.retrySucceeds = true;

  assert.deepEqual(evidence.consoleErrors, []);
  assert.deepEqual(evidence.failedRequests, []);
  assert.deepEqual(evidence.cartResponses, [200, 409, 200]);
  console.log(JSON.stringify(evidence));
} finally {
  if (context) await context.close();
  if (testJob) {
    const cleanup = await fetch(`${baseUrl}/api/e2e/jobs/${testJob.job_id}`, {method: 'DELETE', headers: markerHeaders});
    assert.equal(cleanup.status, 200, `Test-Job-Cleanup fehlgeschlagen: HTTP ${cleanup.status}`);
  }
}
