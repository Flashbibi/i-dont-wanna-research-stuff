// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (C) 2026 Flashbibi
/**
 * Setzt das Gast-Session-Cookie im Shop und öffnet den gefüllten Warenkorb.
 *
 * Läuft in Chromium als service_worker, in Firefox als Hintergrundskript - das
 * Manifest trägt beide Schlüssel, dieser Code kommt ohne Unterschied aus.
 *
 * Cookie-Werte werden nie geloggt.
 */
(() => {
  "use strict";

  const api = globalThis.browser ?? globalThis.chrome;
  const HANDOFF = "beschaffung/cart-handoff";

  async function handoff({cookie, shop_url: shopUrl, uebergabe_url: uebergabeUrl}) {
    let origin;
    try {
      origin = new URL(shopUrl);
    } catch {
      return {ok: false, error: `Shop-URL nicht lesbar: ${shopUrl}`};
    }
    if (origin.protocol !== "https:") {
      // Das Session-Cookie ist Secure; über http würde es stillschweigend
      // verworfen und der Korb wäre im Tab nicht da.
      return {ok: false, error: "Shop-URL ist nicht https - ein Secure-Cookie lässt sich so nicht setzen."};
    }

    try {
      await api.cookies.set({
        url: origin.origin + "/",
        name: cookie.name,
        value: cookie.wert,
        path: "/",
        secure: true,
      });
    } catch (error) {
      return {ok: false, error: `Cookie liess sich nicht setzen: ${error?.message ?? error}`};
    }

    try {
      await api.tabs.create({url: uebergabeUrl});
    } catch (error) {
      return {ok: false, error: `Cookie ist gesetzt, aber der Tab liess sich nicht öffnen: ${error?.message ?? error}`};
    }
    return {ok: true};
  }

  api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== HANDOFF) return undefined;
    // sendResponse + "return true" statt eines zurückgegebenen Promise:
    // Firefox versteht beides, Chrome nur diesen Weg.
    handoff(message).then(sendResponse, (error) =>
      sendResponse({ok: false, error: `Unerwarteter Fehler: ${error?.message ?? error}`}),
    );
    return true;
  });
})();
