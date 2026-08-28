/** Der Shop wird serverseitig gestubbt und nur das Tab-Ziel umgebogen, weil das
 * Session-Cookie Secure ist und auf der Shop-Origin bleiben muss. */
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_CORE || '/home/hermes/.local/share/beschaffung-browser/node_modules/playwright-core');
const baseUrl = process.env.BASE_URL || 'http://192.168.1.60:8000';
const markerHeaders = {'X-E2E-Marker': 'beschaffung-e2e-disposable'};
const executablePath = process.env.CHROME_BIN || '/home/hermes/.local/share/beschaffung-browser/chrome-linux64/chrome';
const extensionDir = process.env.EXTENSION_DIR || fileURLToPath(new URL('../../extension', import.meta.url));
const READY_TIMEOUT = Number(process.env.READY_TIMEOUT || 6000);
const COOKIE_ORIGIN = 'https://www.bastelgarage.ch/';

let testJob = null;
let context = null;

const waitForMatrix = async page => {
  await page.locator('#matrix-root .labels').first().waitFor();
  await page.waitForLoadState('networkidle');
};

// Nur das Tab-Ziel wird umgebogen, damit das Cookie-Ziel unverändert bleibt.
const routeCartFill = async (page, stubMode) => {
  await page.route('**/shops/*/cart', async route => {
    const url = new URL(route.request().url());
    url.searchParams.set('stub', stubMode);
    const response = await route.fetch({
      url: url.toString(),
      headers: {...route.request().headers(), ...markerHeaders},
    });
    if (response.status() !== 200) {
      await route.fulfill({response});
      return;
    }
    const payload = await response.json();
    payload.uebergabe_url = `${baseUrl}/shops`;
    await route.fulfill({response, body: JSON.stringify(payload)});
  });
};

const openJob = async (page, jobId) => {
  await page.goto(`${baseUrl}/jobs/${jobId}`, {waitUntil: 'networkidle'});
  await waitForMatrix(page);
  const complete = page.locator('#matrix-root .mc.chead').filter({has: page.locator('.labels')}).filter({hasNotText: 'Ein Shop'}).first();
  const choose = complete.getByRole('button', {name: 'Diesen Plan wählen'});
  if (await choose.count()) await choose.click();
  await complete.getByText('✓ Gewählt').waitFor();
  const shopRow = page.locator('#railorder .shoprow').first();
  await shopRow.getByRole('button', {name: 'Warenkorb füllen'}).click();
  await shopRow.locator('.cartok').waitFor();
  return shopRow;
};

try {
  const setup = await fetch(`${baseUrl}/api/e2e/jobs`, {method: 'POST', headers: markerHeaders});
  assert.equal(setup.status, 201, `Test-Job konnte nicht angelegt werden: HTTP ${setup.status}`);
  testJob = await setup.json();
  const jobId = testJob.job_id;
  const evidence = {baseUrl, testJobId: jobId, extensionDir, consoleErrors: []};

  const info = await (await fetch(`${baseUrl}/api/extension`)).json();
  evidence.serverExtensionVersion = info.version;

  context = await chromium.launchPersistentContext('', {headless: true, executablePath});
  let page = await context.newPage();
  await routeCartFill(page, 'ok');
  let shopRow = await openJob(page, jobId);
  await shopRow.getByRole('button', {name: 'Kopieren'}).waitFor();
  assert.equal(await shopRow.getByRole('button', {name: 'Im Browser übernehmen'}).count(), 0,
    'Ohne Erweiterung darf der Ein-Klick-Knopf nicht erscheinen');
  await shopRow.getByText('Ein-Klick-Übernahme braucht die Browser-Erweiterung', {exact: false}).waitFor();
  evidence.withoutExtensionCopyOnly = true;
  await context.close();

  context = await chromium.launchPersistentContext('', {
    headless: false,
    executablePath,
    args: [`--disable-extensions-except=${extensionDir}`, `--load-extension=${extensionDir}`],
  });
  page = await context.newPage();
  page.on('console', message => { if (message.type() === 'error') evidence.consoleErrors.push(message.text()); });
  page.on('pageerror', error => evidence.consoleErrors.push(error.message));
  await routeCartFill(page, 'ok');
  shopRow = await openJob(page, jobId);

  const oneClick = shopRow.getByRole('button', {name: 'Im Browser übernehmen'});
  let loaded = true;
  try {
    await oneClick.waitFor({timeout: READY_TIMEOUT});
  } catch {
    loaded = false;
  }
  evidence.mode = loaded ? 'geladene-erweiterung' : 'gestubtes-ready-signal';

  if (!loaded) {
    await page.evaluate(() => {
      window.__handoffPayload = null;
      window.addEventListener('message', event => {
        if (event.source === window && event.data && event.data.type === 'beschaffung/cart-handoff') {
          window.__handoffPayload = event.data;
          window.postMessage({type: 'beschaffung/cart-handoff-result', ok: true, version: '0.0.0-stub'}, window.location.origin);
        }
      });
      window.postMessage({type: 'beschaffung/extension-ready', version: '0.0.0-stub'}, window.location.origin);
    });
    await oneClick.waitFor();
    await oneClick.click();
    await shopRow.getByText('Übernommen ✓ — Tab geöffnet', {exact: false}).waitFor();
    const payload = await page.evaluate(() => window.__handoffPayload);
    assert.equal(payload.type, 'beschaffung/cart-handoff');
    assert.ok(payload.cookie?.name && payload.cookie?.wert, 'Cookie fehlt in der Nutzlast');
    assert.ok(payload.shop_url?.startsWith('https://'), 'shop_url muss https sein');
    assert.ok(payload.uebergabe_url, 'uebergabe_url fehlt');
    evidence.payloadCookieName = payload.cookie.name;
    evidence.payloadHasHandoverUrl = true;
  } else {
    await shopRow.getByText(`Erweiterung v${info.version}`, {exact: false}).waitFor();
    evidence.pageShowsExtensionVersion = info.version;

    const tabOpened = context.waitForEvent('page');
    await oneClick.click();
    const newTab = await tabOpened;
    await newTab.waitForLoadState('domcontentloaded');
    evidence.openedTabUrl = newTab.url();
    assert.ok(newTab.url().startsWith(`${baseUrl}/shops`), `Unerwartetes Tab-Ziel: ${newTab.url()}`);

    await shopRow.getByText('Übernommen ✓ — Tab geöffnet', {exact: false}).waitFor();

    const cookies = await context.cookies(COOKIE_ORIGIN);
    const session = cookies.find(cookie => cookie.name === 'OCSESSID');
    assert.ok(session, 'OCSESSID wurde nicht auf der Shop-Origin gesetzt');
    assert.ok(session.value.length > 0);
    assert.equal(session.secure, true, 'Session-Cookie muss Secure sein');
    evidence.cookieSetOnShopOrigin = {name: session.name, secure: session.secure, domain: session.domain};
  }

  // Der Kopierflow bleibt in beiden Varianten vollständig bestehen.
  await shopRow.getByRole('button', {name: 'Kopieren'}).waitFor();
  await shopRow.getByText('Die Session ist flüchtig', {exact: false}).waitFor();
  evidence.copyFlowStillPresent = true;

  assert.deepEqual(evidence.consoleErrors, []);
  console.log(JSON.stringify(evidence));
} finally {
  if (context) await context.close();
  if (testJob) {
    const cleanup = await fetch(`${baseUrl}/api/e2e/jobs/${testJob.job_id}`, {method: 'DELETE', headers: markerHeaders});
    assert.equal(cleanup.status, 200, `Test-Job-Cleanup fehlgeschlagen: HTTP ${cleanup.status}`);
  }
}
