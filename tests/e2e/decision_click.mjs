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

try {
  const setup = await fetch(`${baseUrl}/api/e2e/jobs`, {method: 'POST', headers: markerHeaders});
  assert.equal(setup.status, 201, `Test-Job konnte nicht angelegt werden: HTTP ${setup.status}`);
  testJob = await setup.json();
  assert.equal(testJob.marker, '[E2E-TEST]');

  context = await chromium.launchPersistentContext(profile, {headless: true, executablePath});
  const page = await context.newPage();
  const {job_id: jobId, offer_id: offerId} = testJob;
  const evidence = {
    baseUrl,
    testJobId: jobId,
    testOfferId: offerId,
    marker: testJob.marker,
    consoleErrors: [],
    failedRequests: [],
    badResponses: [],
    decisionResponses: [],
  };
  page.on('console', message => {
    if (message.type() === 'error') evidence.consoleErrors.push(message.text());
  });
  page.on('pageerror', error => evidence.consoleErrors.push(error.message));
  page.on('requestfailed', request => evidence.failedRequests.push({url: request.url(), error: request.failure()?.errorText}));
  page.on('response', response => {
    if (response.status() >= 400) evidence.badResponses.push({url: response.url(), status: response.status()});
    if (response.url().includes(`/api/offers/${offerId}/decision`)) {
      evidence.decisionResponses.push({url: response.url(), status: response.status()});
    }
  });

  await page.goto(`${baseUrl}/jobs/${jobId}`, {waitUntil: 'networkidle'});
  const card = page.locator(`#offer-${offerId}`);
  await card.waitFor();
  assert.ok((await card.getAttribute('class')).includes('decision-offen'));

  await card.getByRole('button', {name: 'Pin', exact: true}).click();
  await card.locator('[role="status"]').filter({hasText: 'Gepinnt'}).waitFor();
  assert.ok((await card.getAttribute('class')).includes('override-pin'));

  await page.reload({waitUntil: 'networkidle'});
  const reloaded = page.locator(`#offer-${offerId}`);
  assert.ok((await reloaded.getAttribute('class')).includes('override-pin'));
  assert.equal((await reloaded.locator('[role="status"]').innerText()).trim(), 'Gepinnt');

  await page.goto(`${baseUrl}/`, {waitUntil: 'networkidle'});
  assert.equal(await page.getByText(`[E2E-TEST]`, {exact: false}).count(), 0, 'Test-Job darf nicht in der UI erscheinen');

  assert.deepEqual(evidence.consoleErrors, []);
  assert.deepEqual(evidence.failedRequests, []);
  assert.deepEqual(evidence.badResponses, []);
  assert.equal(evidence.decisionResponses.length, 1);
  assert.equal(evidence.decisionResponses[0].status, 200);
  evidence.visibleAfterClick = true;
  evidence.persistedAfterReload = true;
  evidence.hiddenFromJobList = true;
  console.log(JSON.stringify(evidence));
} finally {
  if (context) await context.close();
  if (testJob) {
    const cleanup = await fetch(`${baseUrl}/api/e2e/jobs/${testJob.job_id}`, {
      method: 'DELETE',
      headers: markerHeaders,
    });
    assert.equal(cleanup.status, 200, `Test-Job-Cleanup fehlgeschlagen: HTTP ${cleanup.status}`);
  }
}
