"""Prüft einen Adapter gegen eine Seite und zeigt zu jedem Feld den Rohtext neben dem
geparsten Wert, weil der Vergleich beim Menschen bleibt."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .adapter import (
    Adapter,
    AdapterFehler,
    AdapterFehlt,
    extrahiere,
    lade_adapter,
    parse_preis,
)
from .fetch import FetchFehler, hole_seite


def baue_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.adapter_check",
        description="Einen Shop-Adapter gegen eine Seite prüfen - ohne Datenbank.",
    )
    parser.add_argument("adapter", type=Path, help="Pfad zur Adapter-YAML")
    parser.add_argument(
        "url",
        nargs="?",
        help="Produkt-URL; wird live geholt (robots.txt und Mindestabstand gelten)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        help="HTML-Datei statt eines Abrufs - kein Netzzugriff",
    )
    parser.add_argument(
        "--waehrung",
        default="CHF",
        help="Währung des Shops, gegen die der Preis geprüft wird (Vorgabe: CHF)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = baue_parser().parse_args(argv)
    if bool(args.url) == bool(args.fixture):
        print(
            "Entweder eine Produkt-URL oder --fixture angeben, nicht beides.",
            file=sys.stderr,
        )
        return 1
    try:
        adapter = lade_adapter(args.adapter)
        quelle, html = _seite(adapter, args)
        _zeige(adapter, quelle, html, args.waehrung)
    except (AdapterFehler, FetchFehler) as fehler:
        print(f"Fehler: {fehler}", file=sys.stderr)
        return 1
    except OSError as fehler:
        print(f"Fehler: {fehler}", file=sys.stderr)
        return 1
    return 0


def _seite(adapter: Adapter, args: argparse.Namespace) -> tuple[str, str]:
    """Die zu prüfende Seite besorgen - aus dem Netz oder von der Platte."""
    if args.fixture is not None:
        try:
            return str(args.fixture), args.fixture.read_text(encoding="utf-8")
        except UnicodeDecodeError as fehler:
            raise AdapterFehler(
                f"{args.fixture}: ist nicht UTF-8 ({fehler})"
            ) from fehler
    _pruefe_zustaendigkeit(adapter, args.url)
    ergebnis = hole_seite(args.url, min_delay_s=adapter.min_delay_s)
    return ergebnis.final_url, ergebnis.text


def _pruefe_zustaendigkeit(adapter: Adapter, url: str) -> None:
    """Dieselben zwei Hürden, an denen auch ``fetch_offer`` scheitern würde."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")
    if host != adapter.domain and not host.endswith("." + adapter.domain):
        raise AdapterFehlt(
            f"{host} gehört nicht zu domain «{adapter.domain}» dieses Adapters"
        )
    if not adapter.url_pattern.search(url):
        raise AdapterFehlt(
            f"url_pattern «{adapter.url_pattern.pattern}» trifft nicht auf {url}"
        )


def _zeige(adapter: Adapter, quelle: str, html: str, waehrung: str) -> None:
    felder = extrahiere(adapter, html)
    print(f"Adapter : {adapter.id} ({adapter.domain}, {adapter.datei})")
    print(f"Quelle  : {quelle}")
    print("")
    for name, roh in felder.items():
        if roh is None:
            print(f"  {name:<16} -  (nicht gefunden, optional)")
            continue
        print(f"  {name:<16} -> «{roh}»")
        if name == "preis":
            print(f"  {'':<16}    = {parse_preis(roh, waehrung)} {waehrung.upper()}")


if __name__ == "__main__":
    raise SystemExit(main())
