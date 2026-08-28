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
const planHeader = (page, label) => page.locator('#matrix-root .mc.chead').filter({has: page.locator('.labels', {hasText: label})});

try {
  const setup = await fetch(`${baseUrl}/api/e2e/jobs`, {method: 'POST', headers: markerHeaders});
  assert.equal(setup.status, 201, `Test-Job konnte nicht angelegt werden: HTTP ${setup.status}`);
  testJob = await setup.json();
  assert.equal(testJob.marker, '[E2E-TEST]');
  assert.equal(testJob.offer_ids.length, 6);
  assert.equal(testJob.line_ids.length, 3);

  context = await chromium.launchPersistentContext(profile, {headless: true, executablePath});
  const page = await context.newPage();
  const {job_id: jobId, offer_ids: offerIds, line_ids: lineIds} = testJob;
  const evidence = {
    baseUrl, testJobId: jobId, marker: testJob.marker,
    consoleErrors: [], failedRequests: [], badResponses: [],
    selectionResponses: [], decisionResponses: [], purchaseResponses: [],
  };
  page.on('console', message => { if (message.type() === 'error') evidence.consoleErrors.push(message.text()); });
  page.on('pageerror', error => evidence.consoleErrors.push(error.message));
  page.on('requestfailed', request => evidence.failedRequests.push({url: request.url(), error: request.failure()?.errorText}));
  page.on('response', response => {
    if (response.status() >= 400) evidence.badResponses.push({url: response.url(), status: response.status()});
    if (response.url().includes('/selection')) evidence.selectionResponses.push(response.status());
    if (response.url().includes('/decision')) evidence.decisionResponses.push(response.status());
    if (response.url().includes('/purchase')) evidence.purchaseResponses.push(response.status());
  });

  await page.goto(`${baseUrl}/jobs/${jobId}`, {waitUntil: 'networkidle'});
  await waitForMatrix(page);
  assert.equal(await page.getByText('[E2E-TEST]', {exact: true}).count(), 1);

  // Spalte wählen: sofort sichtbar und nach Reload persistent.
  const oneShop = planHeader(page, 'Ein Shop');
  await oneShop.getByRole('button', {name: 'Diesen Plan wählen'}).click();
  await oneShop.getByText('✓ Gewählt').waitFor();
  evidence.selectionVisible = true;
  await page.reload({waitUntil: 'networkidle'});
  await waitForMatrix(page);
  assert.equal(await planHeader(page, 'Ein Shop').getByText('✓ Gewählt').count(), 1);
  evidence.selectionPersisted = true;

  // Unvollständiger Plan bleibt auch nach Abhaken gesperrt.
  const incompleteChecks = page.locator('#railorder input[data-shop]');
  for (let index = 0; index < await incompleteChecks.count(); index += 1) await incompleteChecks.nth(index).check();
  assert.equal(await page.getByRole('button', {name: 'Bestellung erfassen'}).isDisabled(), true);
  await page.getByText('Unvollständige Pläne können nicht erfasst werden.').waitFor();
  evidence.incompletePurchaseBlocked = true;

  // Vollplan über den prototypischen Reparaturlink wählen.
  await planHeader(page, 'Ein Shop').getByText('Vollplan wählen', {exact: false}).click();
  await page.locator('#matrix-root .chosenmark').filter({hasText: '✓ Gewählt'}).waitFor();

  // Zeile aufklappen, alternativen Kandidaten pinnen, Matrix aktualisiert sich.
  await page.locator(`[data-row="${lineIds[0]}"]`).first().click();
  await page.locator(`#candidate-${offerIds[1]}`).waitFor();
  await page.locator(`#candidate-${offerIds[1]}`).getByRole('button', {name: 'Pinnen'}).click();
  await page.locator(`#candidate-${offerIds[1]} .chip.pin`, {hasText: 'gepinnt'}).waitFor();
  evidence.pinVisible = true;

  await page.reload({waitUntil: 'networkidle'});
  await waitForMatrix(page);
  await page.locator(`[data-row="${lineIds[0]}"]`).first().click();
  await page.locator(`#candidate-${offerIds[1]} .chip.pin`, {hasText: 'gepinnt'}).waitFor();
  evidence.pinPersisted = true;

  // Eine konkrete Angebots-ID ausschliessen und Persistenz nach Reload prüfen.
  await page.locator(`[data-row="${lineIds[1]}"]`).first().click();
  await page.locator(`#candidate-${offerIds[2]}`).getByRole('button', {name: 'Ausschliessen'}).click();
  await page.locator(`#candidate-${offerIds[2]}`).waitFor({state: 'detached'});
  evidence.excludeVisible = true;
  await page.reload({waitUntil: 'networkidle'});
  await waitForMatrix(page);
  await page.locator(`[data-row="${lineIds[1]}"]`).first().click();
  assert.equal(await page.locator(`#candidate-${offerIds[2]}`).count(), 0);
  evidence.excludePersisted = true;

  // Regler löst debouncte Serverrechnung und einen sichtbaren Verdict-Satz aus.
  const scenarioResponse = page.waitForResponse(response => response.url().includes(`/api/jobs/${jobId}/scenarios`) && response.request().method() === 'POST');
  await page.locator('#tempo').evaluate(element => {
    element.value = '85';
    element.dispatchEvent(new Event('input', {bubbles: true}));
  });
  assert.equal((await scenarioResponse).status(), 200);
  await page.locator('#verdict').filter({hasText: /Ändert bei diesem Angebots-Pool nichts:|Eigene Gewichtung/}).waitFor();
  evidence.tempoVerdictVisible = true;

  // Einen vollständigen Plan wählen, alle Shops abhaken und record_purchase auslösen.
  const completeHeaders = page.locator('#matrix-root .mc.chead').filter({has: page.locator('.labels')}).filter({hasNotText: 'Ein Shop'});
  const complete = completeHeaders.first();
  const chooseComplete = complete.getByRole('button', {name: 'Diesen Plan wählen'});
  if (await chooseComplete.count()) await chooseComplete.click();
  await complete.getByText('✓ Gewählt').waitFor();
  const checks = page.locator('#railorder input[data-shop]');
  for (let index = 0; index < await checks.count(); index += 1) await checks.nth(index).check();
  const record = page.getByRole('button', {name: 'Bestellung erfassen'});
  assert.equal(await record.isEnabled(), true);
  const purchaseResponse = page.waitForResponse(response => response.url().includes(`/api/jobs/${jobId}/purchase`));
  await record.click();
  assert.equal((await purchaseResponse).status(), 200);
  await page.getByText('Bestellung erfasst.', {exact: false}).waitFor();
  assert.equal((await page.locator('#jobstatus').innerText()).trim(), 'bestellt');
  evidence.purchaseRecorded = true;

  await page.reload({waitUntil: 'networkidle'});
  await page.getByText('Wenn die Pakete da sind: in der Historie «Angekommen» drücken.').waitFor();
  evidence.purchasePersisted = true;

  await page.goto(`${baseUrl}/`, {waitUntil: 'networkidle'});
  assert.equal(await page.getByText('[E2E-TEST]', {exact: false}).count(), 0, 'Test-Job darf nicht in der UI erscheinen');
  evidence.hiddenFromJobList = true;

  assert.deepEqual(evidence.consoleErrors, []);
  assert.deepEqual(evidence.failedRequests, []);
  assert.deepEqual(evidence.badResponses, []);
  assert.ok(evidence.selectionResponses.every(status => status === 200));
  assert.ok(evidence.decisionResponses.every(status => status === 200));
  assert.deepEqual(evidence.purchaseResponses, [200]);
  console.log(JSON.stringify(evidence));
} finally {
  if (context) await context.close();
  if (testJob) {
    const cleanup = await fetch(`${baseUrl}/api/e2e/jobs/${testJob.job_id}`, {method: 'DELETE', headers: markerHeaders});
    assert.equal(cleanup.status, 200, `Test-Job-Cleanup fehlgeschlagen: HTTP ${cleanup.status}`);
  }
}
