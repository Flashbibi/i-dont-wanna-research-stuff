# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-28

### Added

- Offers can be added from the job page by pasting product URLs. The engine
  reads each page itself, one after another, and shows the result per URL as it
  arrives. Where it cannot read a page it says why, and a manual form takes
  over - that offer is then visibly marked as unverified, because nobody
  machine-read the page it came from.
- A "check prices" button that re-reads every adapter-covered offer of a job
  before ordering, showing each old and new price side by side, counting the
  ones without an adapter instead of hiding them, and cancellable between two
  requests.
- MCP tool `refresh_offer`, which re-reads an offer identified by its id, so a
  refresh cannot pair a line with the wrong page.
- Declarative YAML shop adapters with a deterministic extraction engine. An
  adapter names the CSS selectors for product name, price, delivery text, stock
  text and article number; the engine fetches the page and applies them, so
  those texts are literal page text instead of something a model typed.
- A polite fetch layer that every adapter request goes through: robots.txt is
  read first and obeyed (including `Crawl-delay`), a minimum delay per domain is
  enforced process-wide, and the user agent names the tool and where to read
  what it does. Redirects are followed by hand so that every hop passes the same
  checks, and a chain leaving the domain ends the fetch before the other host is
  contacted. None of it is switchable.
- MCP tools `fetch_offer` and `list_adapters`.
- An `adapter_check` CLI for building adapters: it prints the raw text found
  per field next to the parsed value, live through the same fetch layer or
  against a saved page, and touches no database.

### Changed

- Offers record how they were captured (`erfasst_via`). Empty keeps meaning
  captured by hand or via the model; only the engine writes `adapter:<id>`.
- Re-recording an offer on the same day keeps the marketplace seller it already
  had when the new capture does not name one, the way the article number
  already behaved. Provenance is evidence that cannot be re-derived later.
- A URL carrying credentials is refused wherever a URL is validated, including
  the password-only form that used to slip through.
- The fetch layer reads up to 5 MB of a page instead of 2 MB. The ceiling is
  self-protection against a shop that never stops sending, and it was set too
  tight: BerryBase serves product pages above 2 MB and was locked out by our
  own guard, not by the shop.
- A delivery time stated in weeks is read as days - "1-2 Wochen" is 14 days,
  not "unknown", so the planner stops charging the unknown-delivery penalty for
  a page that says it plainly. Where a page names both, the day figure still
  wins. English patterns are deliberately still not read.
- The BerryBase adapters read the delivery time as well, not only the stock
  text. Both stand in one sentence in the buy box ("Sofort verfügbar · 100+
  Stück · 1-3 Tage"), so the same element feeds both fields and the planner
  gets a delivery figure instead of an unknown.

### Fixed

- The E2E setup endpoint supplies its own throwaway shops when an instance
  knows fewer than three, so the click paths no longer fail on a fresh database
  with an HTTP 422 about missing shops. The cleanup takes those shops back out
  once no offer points at them.
- The manual-capture click path no longer trips over the console echo of its
  own expected 422. Chromium mirrors the engine's refusal as a console error,
  and the path demanded of one and the same answer that it arrive and that it
  not arrive; only that one echo is dropped now, every other console error
  still turns the run red. Its evidence JSON is printed even when an assertion
  aborts - which is precisely when it is needed.

## [0.1.0] - 2026-08-21

### Added

- Multi-shop optimization for a pasted bill of materials. The planner decides
  which shop supplies which position and prices whole orders, including
  shipping, free-shipping thresholds and minimum order values. It answers with
  ranked scenarios instead of one plan; pinning or excluding a single offer
  recalculates the affected plans server-side.
- Mandatory provenance on every recorded fact. A shop profile needs its source
  URL and the shipping text it was read from, an offer keeps the source it came
  from, a marketplace listing keeps its seller, and an exchange rate keeps rate,
  timestamp and source. Nothing enters the database as a bare number.
- Cart handover into the shop. The tool fills a guest cart on supported
  platforms and reads it back position by position on gross prices before
  handing it over; a mismatch blocks the handover and names the difference. The
  browser extension carries the guest session across origins for the one-click
  path, and the copy flow remains as the fallback for every browser.
- An MCP server over streamable HTTP with 17 tools covering jobs, offers,
  decisions, plans, shops and stock, validated through the same service layer as
  the web interface.
- A stock ledger. Every movement is booked with a reason, a correction is only
  accepted with a comment, and deleting a job books its stock back instead of
  dropping it.
- A stock page listing what is on hand, where each part came from and the most
  recent movements, with an inline correction form.
- Currency and delivery-target support. Shops may sit outside Switzerland and
  price in a foreign currency; plans are compared on the honest end price for a
  delivery target, including its pickup surcharge and waiting time.
- Additive migrations that run at startup, and a `/health` endpoint that reports
  the applied schema version and the running application version.
- AGPL-3.0-only licensing, with the source link in the interface footer.

[Unreleased]: https://github.com/Flashbibi/i-dont-wanna-research-stuff/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Flashbibi/i-dont-wanna-research-stuff/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Flashbibi/i-dont-wanna-research-stuff/releases/tag/v0.1.0
