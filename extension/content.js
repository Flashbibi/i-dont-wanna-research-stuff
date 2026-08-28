/** window.postMessage statt externally_connectable, weil es das nur in Chrome gibt. */
(() => {
  "use strict";

  const api = globalThis.browser ?? globalThis.chrome;
  const VERSION = api.runtime.getManifest().version;

  const HANDOFF = "beschaffung/cart-handoff";
  const RESULT = "beschaffung/cart-handoff-result";
  const READY = "beschaffung/extension-ready";

  const post = (payload) => window.postMessage(payload, window.location.origin);

  const isFromThisPage = (event) =>
    event.source === window && event.origin === window.location.origin;

  window.addEventListener("message", (event) => {
    if (!isFromThisPage(event)) return;
    const message = event.data;
    if (!message || message.type !== HANDOFF) return;

    const {cookie, shop_url: shopUrl, uebergabe_url: uebergabeUrl} = message;
    if (!cookie || !cookie.name || !cookie.wert || !shopUrl || !uebergabeUrl) {
      post({type: RESULT, ok: false, error: "Übergabe unvollständig - Cookie oder Ziel-URL fehlt."});
      return;
    }

    api.runtime.sendMessage(
      {type: HANDOFF, cookie, shop_url: shopUrl, uebergabe_url: uebergabeUrl},
      (result) => {
        const failure = api.runtime.lastError;
        if (failure) {
          post({type: RESULT, ok: false, error: `Erweiterung antwortet nicht: ${failure.message}`});
          return;
        }
        post({type: RESULT, version: VERSION, ...(result || {ok: false, error: "Keine Antwort der Erweiterung."})});
      },
    );
  });

  // Ohne dieses Signal zeigt die Seite den Ein-Klick-Knopf nicht und bleibt exakt
  // beim Kopierflow.
  post({type: READY, version: VERSION});
  document.addEventListener("DOMContentLoaded", () => post({type: READY, version: VERSION}));
})();
