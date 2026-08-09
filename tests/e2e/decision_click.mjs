import {createRequire} from 'node:module';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_CORE || '/home/hermes/.local/share/beschaffung-browser/node_modules/playwright-core');

const baseUrl = process.env.BASE_URL || 'http://192.168.1.60:8000';
const offerId = process.env.OFFER_ID || '13';
const executablePath = process.env.CHROME_BIN || '/home/hermes/.local/share/beschaffung-browser/chrome-linux64/chrome';
const profile = process.env.BROWSER_PROFILE || '/home/hermes/.local/share/beschaffung-browser/e2e-profile';
const context = await chromium.launchPersistentContext(profile, {headless: true, executablePath});
const page = await context.newPage();
const evidence = {baseUrl, offerId, consoleErrors: [], failedRequests: [], decisionResponses: []};
page.on('console', message => {
  if (message.type() === 'error') evidence.consoleErrors.push(message.text());
});
page.on('pageerror', error => evidence.consoleErrors.push(error.message));
page.on('requestfailed', request => evidence.failedRequests.push({url: request.url(), error: request.failure()?.errorText}));
page.on('response', response => {
  if (response.url().includes(`/api/offers/${offerId}/decision`)) {
    evidence.decisionResponses.push({url: response.url(), status: response.status()});
  }
});

try {
  await page.goto(`${baseUrl}/jobs/1`, {waitUntil: 'networkidle'});
  const card = page.locator(`#offer-${offerId}`);
  await card.waitFor();
  const initialClass = await card.getAttribute('class');
  const initialStatus = initialClass.includes('decision-bestaetigt')
    ? 'bestaetigt'
    : initialClass.includes('decision-verworfen') ? 'verworfen' : null;
  assert.ok(initialStatus, 'Testangebot braucht einen wiederherstellbaren Ausgangsstatus');
  const targetStatus = initialStatus === 'bestaetigt' ? 'verworfen' : 'bestaetigt';
  const targetLabel = targetStatus === 'bestaetigt' ? 'Bestätigen' : 'Verwerfen';
  const targetFeedback = targetStatus === 'bestaetigt' ? 'Bestätigt' : 'Verworfen';

  await card.getByRole('button', {name: targetLabel, exact: true}).click();
  await card.locator('[role="status"]').filter({hasText: targetFeedback}).waitFor();
  assert.ok((await card.getAttribute('class')).includes(`decision-${targetStatus}`));

  await page.reload({waitUntil: 'networkidle'});
  const reloaded = page.locator(`#offer-${offerId}`);
  assert.ok((await reloaded.getAttribute('class')).includes(`decision-${targetStatus}`));
  assert.equal((await reloaded.locator('[role="status"]').innerText()).trim(), targetFeedback);

  const restoreLabel = initialStatus === 'bestaetigt' ? 'Bestätigen' : 'Verwerfen';
  await reloaded.getByRole('button', {name: restoreLabel, exact: true}).click();
  await reloaded.locator('[role="status"]').filter({hasText: initialStatus === 'bestaetigt' ? 'Bestätigt' : 'Verworfen'}).waitFor();
  await page.reload({waitUntil: 'networkidle'});
  assert.ok((await page.locator(`#offer-${offerId}`).getAttribute('class')).includes(`decision-${initialStatus}`));

  assert.deepEqual(evidence.consoleErrors, []);
  assert.deepEqual(evidence.failedRequests, []);
  assert.equal(evidence.decisionResponses.length, 2);
  assert.ok(evidence.decisionResponses.every(response => response.status === 200));
  evidence.initialStatus = initialStatus;
  evidence.clickedStatus = targetStatus;
  evidence.visibleAfterClick = true;
  evidence.persistedAfterReload = true;
  evidence.restoredInitialStatus = true;
  console.log(JSON.stringify(evidence));
} finally {
  await context.close();
}
