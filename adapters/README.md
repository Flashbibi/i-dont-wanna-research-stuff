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
| `CHF` | `'` `’` space | `.` | `CHF 1'234.50`, `CHF 1 234.50`, `Fr. 7.50`, `12.90` |
| `EUR` | `.` space | `,` | `1.234,56 €`, `1 234,56 €`, `EUR 12,90` |
| `USD` | `,` space | `.` | `$1,299.00` |
| `GBP` | `,` space | `.` | `£9.99` |

A space groups in every currency — Swiss federal style writes `1 234.50`, and a
space can never be a decimal separator anyway. Ordinary, non-breaking and narrow
non-breaking spaces are all treated alike.

Three consequences worth knowing before you blame the parser:

- If the text names a **different** currency than the shop trades in, the
  offer is refused. A `€` on a CHF shop is not converted, it is a
  contradiction. This holds whether or not the code is glued to the number:
  `USD12.90` contradicts a CHF shop just as `USD 12.90` does.
- A number that does not fit its currency's rules is refused as unreadable
  rather than reinterpreted. `12.90` at a EUR shop is not 1290 — a grouping
  separator followed by two digits is not a grouping. Narrow the selector or
  add a `regex`; if the shop genuinely writes prices in a foreign convention,
  the manual path via `record_offer` stays open.
- A price torn apart by markup is refused too. `<span>19<sup>,99</sup></span>`
  comes out of extraction as `19 ,99`, because reading text inserts a separator
  between nested nodes — that is refused rather than silently truncated to 19.
  Point the selector at the element that holds the whole price, or pull it out
  with a `regex`.

- A price with more than two decimals is refused as well. The price columns are
  `NUMERIC(12,2)`, so `CHF 0.09562` would be stored as `0.10` — a different
  number, silently. Invisible characters (zero width space, soft hyphen and
  friends) are removed before parsing, because they are formatting, not content,
  and they used to cut a price in half.

If several numbers sit inside the matched element and something that cannot be
part of a number separates them, the first one wins. The selector is supposed to
hit the price; where it cannot, `regex` narrows it.

One check sits outside the parser, in the engine: if a shop's country and its
recorded currency disagree — a German shop kept in CHF, which is what
`record_shop` defaults to when no currency is given — the page has to name the
currency itself, or nothing is recorded. Booking a euro price as francs at rate
1 would be exactly the kind of unprovable number this project refuses to store.

## Politeness is structural

Every adapter fetch goes through one function, `app/fetch.py:hole_seite`, and
there is no second way past it:

- **robots.txt first.** It is fetched through the same client under the same
  user agent, and that request counts against the delay. A disallowed path is
  refused before the product page is requested at all. If robots.txt cannot be
  read — timeout, HTTP 5xx — the fetch stops; nothing is assumed. Only a clean
  4xx means "there are no rules here".
- **The shop's own `Crawl-delay` is honoured** where robots.txt names one. It
  raises the delay; it never replaces or lowers it. Above 60 seconds the fetch
  is refused rather than quietly waiting less - a silently shortened wait would
  be worse than an honest error.
- **A minimum delay per domain**, process-wide, at least 5 seconds. An adapter
  may raise it with `fetch.min_delay_s`. Neither an adapter nor a caller can
  lower it. The key is the hostname with a leading `www.` stripped, so a shop
  serving `de.`/`fr.`/`it.` locale subdomains gets one clock per subdomain -
  worth knowing when you pick `min_delay_s` for such a shop.
- **Every hop of a redirect chain runs the whole procedure again**: robots.txt
  for the new path, then the delay, then the request. Redirects are followed by
  hand for exactly that reason - an automatic client would walk the chain
  silently, and a shop could hand us a path its own robots.txt forbids simply by
  answering 301. A chain that leaves the domain ends the fetch *before* the
  foreign host is contacted at all. At most five hops.
- **An honest user agent**, naming the tool, its version and the URL where you
  can read what it does. A URL carrying credentials is refused outright — they
  would travel as a `Basic` header and end up stored in the offer row.
- **At most 5 MB per page**, counted on the wire. Answers are requested
  uncompressed for exactly that reason: a compressed stream is only measurable
  after unpacking, and a shop could otherwise push unlimited bytes while the
  limit never moves. A response that arrives compressed anyway is refused.
- **No retries, no backoff, no response caching.** A temporary failure comes
  back as one and a human or the model decides whether to try again. An
  automatic retry would be exactly the silent doubling of load the delay
  exists to prevent.
- **Fetching only happens on an explicit call.** Nothing is refreshed on a
  timer.

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

Finding the page is not the adapter's job either, and never will be. Which
product at which shop answers a line is a judgement call, and an AI client or a
person with a browser makes it far better than any selector could - so there is
no `search` block in the schema and none is planned. An adapter is handed a URL
and reads it; that is the whole contract.

For every one of those cases the manual path stays open: read the page
yourself and record it with `record_offer`, with the texts typed literally -
from the job page, that is the "von Hand nachtragen" form, and what it writes
is marked as unverified.
