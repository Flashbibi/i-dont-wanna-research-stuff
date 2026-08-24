# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/Flashbibi/i-dont-wanna-research-stuff/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Flashbibi/i-dont-wanna-research-stuff/releases/tag/v0.1.0
