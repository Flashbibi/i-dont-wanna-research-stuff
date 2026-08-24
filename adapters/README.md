<!-- SPDX-License-Identifier: AGPL-3.0-only -->
<!-- Copyright (C) 2026 Flashbibi -->

# Shop adapters

An adapter is one YAML file per shop. It says **where** on a product page the
product name, the price, the delivery text, the stock text and the article
number sit — and nothing else. The engine fetches the page and applies those
selectors; the same page yields the same result twice, with no model in
between.

That is the point. Before adapters, a model read the page and typed the values
into `record_offer`, and a delivery time nobody could check was one plausible
sentence away. With an adapter, `lieferzeit_text` and `lager_text` are literal
page text again.

The division of labour is unchanged: **the model finds the page and maps it to
a line, the engine reads and writes.**

## Where adapters live

| Location | Contents |
| --- | --- |
| `adapters/*.yaml` | Bundled with the tool, shipped in the image. |
| `$BESCHAFFUNG_ADAPTER_DIR/*.yaml` | Your own, outside the repository. Optional. |

Files are loaded once, at first use, and cached until the process restarts.
There is no hot reload.

On an id collision **the user file wins**, with an INFO log line saying so — a
broken bundled adapter can be patched without forking the project. Two files
claiming the same id inside the *same* directory is an error; the second one is
skipped.

A broken file is loud but not fatal: it is logged as an ERROR, skipped, and
listed with its reason by the `list_adapters` MCP tool. The rest still loads
and the server still starts.

`beispiel.yaml.example` is the template. The `.example` suffix keeps it out of
the registry — copy it to `<shop>.yaml` before filling it in.

## Building one

```sh
python -m app.adapter_check <adapter.yaml> <produkt-url> [--waehrung CHF]
python -m app.adapter_check <adapter.yaml> --fixture <seite.html> [--waehrung CHF]
```

`adapter_check` validates exactly one file and prints, per field, the raw text
it found next to the parsed value. Compare that against the page yourself —
that comparison is the whole quality gate, and it does not belong to a machine.
The live mode goes through the same fetch layer as the engine; `--fixture`
reads a saved page and never touches the network. Neither mode goes near the
database.

## Schema, version 1

The complete example, also on disk as `beispiel.yaml.example`:

```yaml
schema: 1
id: beispielshop            # ^[a-z0-9][a-z0-9-]{1,31}$
domain: beispielshop.ch     # klein, ohne Schema, ohne Pfad
notes: >
  Freitext. Woher die Selektoren stammen, was beim Bauen auffiel.
fetch:
  min_delay_s: 8            # optional; wirkt nur erhöhend
product:
  url_pattern: "^https://(www\\.)?beispielshop\\.ch/produkt/"
  fields:
    produktname:
      selector: "h1.product-title"
    preis:
      selector: ".price .amount"
      parse: price
    lieferzeit_text:
      selector: ".delivery-info"
      optional: true
    lager_text:
      selector: ".stock-status"
      optional: true
    artikelnummer:
      selector: "meta[itemprop=sku]"
      attribute: "content"
      optional: true
```

| Key | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | Schema version. Must be `1`. |
| `id` | yes | `^[a-z0-9][a-z0-9-]{1,31}$`. Also what ends up in `offer.erfasst_via` as `adapter:<id>`. |
| `domain` | yes | Lowercase host, no scheme, no path. Matches the product URL exactly or as a subdomain; a leading `www.` is ignored. |
| `notes` | no | Free text. Where the selectors came from, what was odd while building them. |
| `fetch.min_delay_s` | no | Seconds between two requests to this domain. Raises the floor of 5 s and can never lower it. |
| `product.url_pattern` | yes | Regular expression, searched against the full product URL. Anchor it yourself with `^`. |
| `product.fields` | yes | Exactly the five field names below, nothing else. |

Fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `produktname` | yes | Product name as the page writes it. |
| `preis` | yes | Price text. Needs `parse: price`. |
| `lieferzeit_text` | no | Literal delivery text. The day count is derived from it by the same parser the manual path uses. |
| `lager_text` | no | Literal stock text. |
| `artikelnummer` | no | The shop's own article number; it anchors the cart check across languages. |

Per field:

| Key | Default | Meaning |
| --- | --- | --- |
| `selector` | — | CSS selector, required. The first match wins (`select_one`). |
| `attribute` | — | Read this attribute instead of the element's text. |
| `regex` | — | Applied after extraction. Must contain **exactly one** capture group; that group becomes the value. |
| `parse` | `text` | `price` is allowed only on `preis`, and is mandatory there. |
| `optional` | `false` | Only for the three optional fields. A missing optional field yields nothing; a missing mandatory one aborts the whole offer. |

Loading is strict. An unknown key at any level, a wrong type, an invalid CSS
selector, a regex without exactly one capture group, a malformed `id` or
`domain` — each is a load error naming the file and the reason. Nothing is
silently ignored, and nothing is guessed.

Reading is strict the other way round: if a mandatory field is not found, the
error names the field and its selector and **nothing at all is written**. A
half-read offer would be worse than no offer.

## Prices

Separators are fixed per shop currency. They are never inferred from the text,
because the same string means different amounts in different conventions.

| Currency | Grouping | Decimal | Reads |
| --- | --- | --- | --- |
| `CHF` | `'` `’` | `.` | `CHF 1'234.50`, `Fr. 7.50`, `12.90` |
| `EUR` | `.` | `,` | `1.234,56 €`, `EUR 12,90` |
| `USD` | `,` | `.` | `$1,299.00` |
| `GBP` | `,` | `.` | `£9.99` |

Two consequences worth knowing before you blame the parser:

- If the text names a **different** currency than the shop trades in, the
  offer is refused. A `€` on a CHF shop is not converted, it is a
  contradiction.
- A number that does not fit its currency's rules is refused as unreadable
  rather than reinterpreted. `12.90` at a EUR shop is not 1290 — a grouping
  separator followed by two digits is not a grouping. Narrow the selector or
  add a `regex`; if the shop genuinely writes prices in a foreign convention,
  the manual path via `record_offer` stays open.

If several numbers sit inside the matched element, the first one wins. The
selector is supposed to hit the price; where it cannot, `regex` narrows it.

## Politeness is structural

Every adapter fetch goes through one function, `app/fetch.py:hole_seite`, and
there is no second way past it:

- **robots.txt first.** It is fetched through the same client under the same
  user agent, and that request counts against the delay. A disallowed path is
  refused before the product page is requested at all. If robots.txt cannot be
  read — timeout, HTTP 5xx — the fetch stops; nothing is assumed. Only a clean
  4xx means "there are no rules here".
- **A minimum delay per domain**, process-wide, at least 5 seconds. An adapter
  may raise it with `fetch.min_delay_s`. It cannot lower it.
- **An honest user agent**, naming the tool, its version and the URL where you
  can read what it does.
- **No retries, no backoff, no response caching.** A temporary failure comes
  back as one and a human or the model decides whether to try again. An
  automatic retry would be exactly the silent doubling of load the delay
  exists to prevent.
- **Fetching only happens on an explicit call.** Nothing is refreshed on a
  timer, and a redirect that leaves the domain ends the fetch, because for that
  other domain no robots.txt was read.

None of this is switchable. There is no flag, no environment variable and no
adapter key that turns it off, and adding one is not a feature request this
project accepts.

## Limits

**Server-rendered HTML only.** The engine fetches the page and parses it — it
runs no JavaScript. A shop that fills its price in with a script hands us an
empty element, and an adapter cannot cover it. That is a limit, not a bug, and
the answer is a plain-text error rather than a guess.

The same applies to pages behind a login, behind a bot check, or behind a
consent wall that hides the content: an adapter has no business there. A shop
that answers HTTP 403 to an automated request has made a decision, and the
error message says so instead of dressing the request up as a browser.

For every one of those cases the manual path stays open: read the page
yourself and record it with `record_offer`, with the texts typed literally.
