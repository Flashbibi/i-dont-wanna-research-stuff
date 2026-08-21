// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Flashbibi
/**
 * Klickpfad der Ein-Klick-Übergabe gegen die LAN-Instanz.
 *
 * Bevorzugte Variante: Chromium als persistenter Kontext mit --load-extension
 * auf extension/. Das braucht headful, in CT 103 also unter Xvfb:
 *
 *   xvfb-run -a env BASE_URL=http://192.168.1.60:8000 node tests/e2e/extension_handoff_click.mjs
 *
 * Lädt der Runner die Erweiterung nicht (kein Bereitschafts-Signal binnen
 * READY_TIMEOUT), fällt das Skript dokumentiert auf ein gestubtes ready-Signal
 * zurück und prüft dann nur die Nutzlast der Seite. Welche Variante lief, steht
 * als `mode` in der Ausgabe - die echte Erweiterung prüft Linus in dem Fall
 * manuell.
 *
 * Der Shop wird serverseitig gestubbt (?stub=), es wird kein echter Shop
 * kontaktiert. Das Cookie-Ziel bleibt die Shop-Origin (das Session-Cookie ist
 * Secure), nur das Tab-Ziel zeigt auf eine lokale Seite.
 */
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

// Fill-Antwort abfangen: Tab-Ziel auf eine lokale Seite, Cookie-Ziel unverändert.
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

  // --- 1) Ohne Erweiterung: nur der Kopierflow ---------------------------
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

  // --- 2) Mit geladener Erweiterung -------------------------------------
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
    // Dokumentierter Rückfall: ready-Signal simulieren, Nutzlast der Seite prüfen.
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
