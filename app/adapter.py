# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Deklarative Shop-Adapter: Schema, Registry, Extraktion, Preis-Parser.

Ein Adapter ist eine YAML-Datei mit CSS-Selektoren. Er beschreibt, **wo** auf
einer Produktseite Name, Preis, Lieferzeit, Lagertext und Artikelnummer stehen -
und sonst nichts. Gelesen wird deterministisch: dieselbe Seite ergibt zweimal
dasselbe Ergebnis, ohne Modell dazwischen. Damit sind ``lieferzeit_text`` und
``lager_text`` wieder das, was sie sein sollen: wörtlicher Seitentext.

Die Rollenverteilung bleibt: die KI findet und ordnet zu (URL -> Zeile), die
Engine liest und schreibt.

Zwei Grenzen, die hier hart sind:

* **Streng beim Laden.** Ein unbekannter Schlüssel, ein falscher Typ oder ein
  Regex ohne Capture-Group ist ein Ladefehler mit Dateinamen - kein stilles
  Ignorieren. Eine defekte Datei nimmt aber nur sich selbst mit, nicht den Start.
* **Streng beim Lesen.** Fehlt ein Pflichtfeld, wird gar nichts geschrieben.
  Ein halb gelesenes Angebot wäre schlimmer als keines.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from bs4 import BeautifulSoup


#: Diagnose-Logging der Registry: laut bei defekten Dateien, aber nie tödlich.
log = logging.getLogger("beschaffung.adapter")

#: Einzige bisher gültige Schema-Fassung.
SCHEMA_VERSION = 1

#: Gebündelte Adapter reisen im Repository und im Image mit.
GEBUENDELT_DIR = Path(__file__).resolve().parent.parent / "adapters"

#: Eigene Adapter, ohne den Fork: ein Verzeichnis aus der Umgebung.
ENV_ADAPTER_DIR = "BESCHAFFUNG_ADAPTER_DIR"

QUELLE_GEBUENDELT = "gebuendelt"
QUELLE_NUTZER = "nutzer"

#: Genau diese fünf Felder kennt Schema 1 - in dieser Reihenfolge, damit jede
#: Ausgabe deterministisch ist.
FELDNAMEN = ("produktname", "preis", "lieferzeit_text", "lager_text", "artikelnummer")

#: Ohne diese beiden gibt es kein Angebot.
PFLICHTFELDER = ("produktname", "preis")

WURZEL_SCHLUESSEL = {"schema", "id", "domain", "notes", "fetch", "product"}
FETCH_SCHLUESSEL = {"min_delay_s"}
PRODUCT_SCHLUESSEL = {"url_pattern", "fields"}
FELD_SCHLUESSEL = {"selector", "attribute", "regex", "parse", "optional"}

ID_MUSTER = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
DOMAIN_MUSTER = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

#: Wie eine Währung im Preistext auftauchen darf. Ein Buchstabencode darf keine
#: Buchstaben neben sich haben - sonst täuschte ein «Neuromodul» einen Euro-Preis
#: vor -, wohl aber eine Ziffer: «USD12.90» ist genauso gemeint wie «USD 12.90».
WAEHRUNGS_MARKER: dict[str, str] = {
    "CHF": r"(?<![a-z])chf(?![a-z])|(?<![a-z])s?fr\.",
    "EUR": r"(?<![a-z])euro?(?![a-z])|€",
    "USD": r"(?<![a-z])usd(?![a-z])|\$",
    "GBP": r"(?<![a-z])gbp(?![a-z])|£",
}

#: Trennzeichen sind pro erwarteter Währung fest verdrahtet. Hier wird nichts
#: geraten: was nicht zur Regel der Shopwährung passt, gilt als unlesbar.
TRENNZEICHEN: dict[str, tuple[tuple[str, ...], str]] = {
    "CHF": (("'", "’"), "."),
    "EUR": ((".",), ","),
    "USD": ((",",), "."),
    "GBP": ((",",), "."),
}

#: Unsichtbare Zeichen: weiches Trennzeichen, Nullbreiten und Steuerzeichen der
#: Textrichtung. Shops streuen sie in Preise, und für ``str.split`` sind sie
#: kein Whitespace - aus «1<U+200B>234.50» würde sonst stillschweigend eine 1.
_UNSICHTBAR = re.compile(r"[\u00ad\u200b-\u200f\u2028\u2029\u2060-\u2064\ufeff]")

#: Erster zusammenhängender Zahlenlauf im Text, Trennzeichen eingeschlossen -
#: das Leerzeichen ausdrücklich mit. Schweizer Bundesschreibweise gruppiert mit
#: Leerzeichen, und ein in Spans zerlegter Preis kommt als «19 ,99» aus der
#: Extraktion. Beides muss vollständig in den Lauf, sonst gewänne die erste
#: Gruppe und aus 1 234.50 würde stillschweigend 1.
_ZAHLENLAUF = re.compile(r"\d[\d.,'’ ]*\d|\d")


class AdapterFehler(ValueError):
    """Klartext-Fehler; die Meldung ist für Oberfläche und MCP bestimmt."""


class AdapterLadefehler(AdapterFehler):
    """Die Adapterdatei ist unbrauchbar - mit Dateinamen und Grund."""


class AdapterFehlt(AdapterFehler):
    """Für diese URL gibt es keinen Adapter; der manuelle Weg bleibt offen."""


class ExtraktionFehlt(AdapterFehler):
    """Ein Pflichtfeld war auf der Seite nicht zu finden. Es wird nichts geschrieben."""


class WaehrungWiderspricht(AdapterFehler):
    """Der Preistext nennt eine andere Währung als der Shop führt."""


@dataclass(frozen=True)
class Feld:
    """Ein Selektor samt Nachbearbeitung."""

    name: str
    selector: str
    attribute: str | None = None
    regex: re.Pattern[str] | None = None
    parse: str = "text"
    optional: bool = False


@dataclass(frozen=True)
class Adapter:
    """Ein geladener, geprüfter Shop-Adapter."""

    id: str
    domain: str
    url_pattern: re.Pattern[str]
    felder: dict[str, Feld]
    notes: str = ""
    min_delay_s: float | None = None
    quelle: str = QUELLE_GEBUENDELT
    datei: Path | None = None


@dataclass(frozen=True)
class Registry:
    """Alle geladenen Adapter - und alles, was dabei liegen geblieben ist."""

    adapter: dict[str, Adapter]
    #: (Datei, Grund) je übersprungener Datei. Laut, aber nicht tödlich.
    fehler: tuple[tuple[str, str], ...] = ()


# -- Laden und Prüfen --------------------------------------------------------


def lade_adapter(pfad: Path, *, quelle: str = QUELLE_GEBUENDELT) -> Adapter:
    """Genau eine Adapterdatei lesen und streng prüfen."""
    try:
        roh = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        # Kein OSError - ohne eigenen Zweig entkäme dieser Fehler der Registry
        # und nähme jeden anderen Adapter mit.
        raise AdapterLadefehler(f"{pfad.name}: ist nicht UTF-8 ({error})") from error
    except OSError as error:
        raise AdapterLadefehler(f"{pfad.name}: nicht lesbar ({error})") from error
    except yaml.YAMLError as error:
        raise AdapterLadefehler(f"{pfad.name}: kein gültiges YAML ({error})") from error
    return _baue_adapter(roh, pfad, quelle)


def _baue_adapter(roh: Any, pfad: Path, quelle: str) -> Adapter:
    daten = _mapping(roh, "Wurzel", pfad)
    _nur_bekannte(daten, WURZEL_SCHLUESSEL, "Wurzel", pfad)

    schema = daten.get("schema")
    if schema != SCHEMA_VERSION:
        raise _ladefehler(pfad, f"schema muss {SCHEMA_VERSION} sein, gefunden: {schema!r}")

    kennung = _text(daten.get("id"), "id", pfad)
    if not ID_MUSTER.match(kennung):
        raise _ladefehler(
            pfad, f"id «{kennung}» passt nicht zu {ID_MUSTER.pattern}"
        )

    domain = _text(daten.get("domain"), "domain", pfad)
    if not DOMAIN_MUSTER.match(domain):
        raise _ladefehler(
            pfad,
            f"domain «{domain}» muss klein geschrieben sein, ohne Schema und ohne Pfad",
        )

    notes = daten.get("notes", "")
    if not isinstance(notes, str):
        raise _ladefehler(pfad, "notes muss Text sein")

    return Adapter(
        id=kennung,
        domain=domain,
        url_pattern=_url_muster(daten, pfad),
        felder=_felder(daten, pfad),
        notes=notes.strip(),
        min_delay_s=_min_delay(daten, pfad),
        quelle=quelle,
        datei=pfad,
    )


def _min_delay(daten: dict[str, Any], pfad: Path) -> float | None:
    profil = daten.get("fetch")
    if profil is None:
        return None
    profil = _mapping(profil, "fetch", pfad)
    _nur_bekannte(profil, FETCH_SCHLUESSEL, "fetch", pfad)
    if "min_delay_s" not in profil:
        return None
    wert = profil["min_delay_s"]
    if isinstance(wert, bool) or not isinstance(wert, (int, float)) or wert <= 0:
        raise _ladefehler(pfad, "fetch.min_delay_s muss eine Zahl grösser 0 sein")
    return float(wert)


def _url_muster(daten: dict[str, Any], pfad: Path) -> re.Pattern[str]:
    produkt = _mapping(daten.get("product"), "product", pfad)
    _nur_bekannte(produkt, PRODUCT_SCHLUESSEL, "product", pfad)
    muster = _text(produkt.get("url_pattern"), "product.url_pattern", pfad)
    try:
        return re.compile(muster)
    except re.error as error:
        raise _ladefehler(
            pfad, f"product.url_pattern ist kein gültiger regulärer Ausdruck ({error})"
        ) from error


def _felder(daten: dict[str, Any], pfad: Path) -> dict[str, Feld]:
    produkt = _mapping(daten.get("product"), "product", pfad)
    felder = _mapping(produkt.get("fields"), "product.fields", pfad)
    _nur_bekannte(felder, set(FELDNAMEN), "product.fields", pfad)
    for name in PFLICHTFELDER:
        if name not in felder:
            raise _ladefehler(pfad, f"product.fields.{name} fehlt und ist Pflicht")
    # Kanonische Reihenfolge statt YAML-Reihenfolge: jede Ausgabe ist damit
    # deterministisch, egal wie die Datei sortiert ist.
    return {
        name: _feld(name, felder[name], pfad) for name in FELDNAMEN if name in felder
    }


def _feld(name: str, roh: Any, pfad: Path) -> Feld:
    stelle = f"product.fields.{name}"
    daten = _mapping(roh, stelle, pfad)
    _nur_bekannte(daten, FELD_SCHLUESSEL, stelle, pfad)

    selector = _text(daten.get("selector"), f"{stelle}.selector", pfad)
    _pruefe_selektor(selector, stelle, pfad)

    attribute = daten.get("attribute")
    if attribute is not None:
        attribute = _text(attribute, f"{stelle}.attribute", pfad)

    muster = None
    if "regex" in daten:
        rohmuster = _text(daten["regex"], f"{stelle}.regex", pfad)
        try:
            muster = re.compile(rohmuster)
        except re.error as error:
            raise _ladefehler(
                pfad, f"{stelle}.regex ist kein gültiger regulärer Ausdruck ({error})"
            ) from error
        if muster.groups != 1:
            raise _ladefehler(
                pfad,
                f"{stelle}.regex braucht genau eine Capture-Group, "
                f"gefunden: {muster.groups}",
            )

    parse = daten.get("parse", "price" if name == "preis" else "text")
    if not isinstance(parse, str):
        raise _ladefehler(pfad, f"{stelle}.parse muss Text sein")
    if name == "preis" and parse != "price":
        raise _ladefehler(pfad, f"{stelle}.parse muss «price» sein")
    if name != "preis" and parse != "text":
        raise _ladefehler(
            pfad, f"{stelle}.parse kennt nur «text»; «price» gibt es nur bei preis"
        )

    optional = daten.get("optional", False)
    if not isinstance(optional, bool):
        raise _ladefehler(pfad, f"{stelle}.optional muss true oder false sein")
    if optional and name in PFLICHTFELDER:
        raise _ladefehler(pfad, f"{stelle} ist Pflicht und darf nicht optional sein")

    return Feld(
        name=name,
        selector=selector,
        attribute=attribute,
        regex=muster,
        parse=parse,
        optional=optional,
    )


def _pruefe_selektor(selector: str, stelle: str, pfad: Path) -> None:
    """Den Selektor beim Laden übersetzen, nicht erst auf der Produktseite."""
    try:
        BeautifulSoup("", "html.parser").select_one(selector)
    except Exception as error:  # noqa: BLE001 - soupsieve wirft eigene Typen
        raise _ladefehler(
            pfad, f"{stelle}.selector ist kein gültiger CSS-Selektor ({error})"
        ) from error


def _ladefehler(pfad: Path, grund: str) -> AdapterLadefehler:
    return AdapterLadefehler(f"{pfad.name}: {grund}")


def _mapping(wert: Any, stelle: str, pfad: Path) -> dict[str, Any]:
    if not isinstance(wert, dict):
        raise _ladefehler(pfad, f"{stelle} muss ein Block mit Schlüsseln sein")
    return wert


def _text(wert: Any, stelle: str, pfad: Path) -> str:
    if not isinstance(wert, str) or not wert.strip():
        raise _ladefehler(pfad, f"{stelle} muss ein nicht leerer Text sein")
    return wert.strip()


def _nur_bekannte(daten: dict[str, Any], erlaubt: set[str], stelle: str, pfad: Path) -> None:
    unbekannt = sorted(str(schluessel) for schluessel in daten if schluessel not in erlaubt)
    if unbekannt:
        raise _ladefehler(
            pfad,
            f"unbekannte Schlüssel unter {stelle}: " + ", ".join(f"«{k}»" for k in unbekannt),
        )


# -- Extraktion --------------------------------------------------------------


def extrahiere(adapter: Adapter, html: str) -> dict[str, str | None]:
    """Alle Felder des Adapters aus der Seite lesen - Rohtext, sonst nichts.

    Geparst wird hier noch nicht; der Preis kommt als Text zurück und geht
    danach durch :func:`parse_preis`. So sieht der Aufrufer, was die Seite
    wörtlich gesagt hat.
    """
    suppe = BeautifulSoup(html, "html.parser")
    return {name: _feldwert(suppe, feld) for name, feld in adapter.felder.items()}


def _feldwert(suppe: BeautifulSoup, feld: Feld) -> str | None:
    treffer = suppe.select_one(feld.selector)
    if treffer is None:
        return _fehlt(feld, "kein Treffer")
    if feld.attribute is not None:
        wert = treffer.get(feld.attribute)
        if isinstance(wert, list):  # class="a b" liefert bs4 als Liste
            wert = " ".join(wert)
        roh = _normalisiere(wert or "")
        if not roh:
            return _fehlt(feld, f"Attribut «{feld.attribute}» fehlt oder ist leer")
    else:
        roh = _normalisiere(treffer.get_text(" ", strip=True))
        if not roh:
            return _fehlt(feld, "Treffer ohne Text")
    if feld.regex is not None:
        gefunden = feld.regex.search(roh)
        if gefunden is None:
            return _fehlt(feld, f"regex trifft nicht auf «{roh}»")
        # Eine optionale Capture-Group kann mittreffen, ohne etwas zu fangen.
        if gefunden.group(1) is None:
            return _fehlt(feld, f"regex fängt nichts in «{roh}»")
        roh = _normalisiere(gefunden.group(1))
        if not roh:
            return _fehlt(feld, "regex liefert nur Leerraum")
    return roh


def _fehlt(feld: Feld, grund: str) -> None:
    if feld.optional:
        return None
    raise ExtraktionFehlt(
        f"Feld «{feld.name}» nicht gelesen: {grund} (Selektor «{feld.selector}»)"
    )


def _normalisiere(text: str) -> str:
    """Whitespace jeder Art auf einzelne Leerzeichen.

    ``str.split`` zählt auch geschützte Leerzeichen als Whitespace - genau die
    stehen in Preisen wie «CHF 12.90» oft statt eines normalen. Unsichtbare
    Zeichen fallen ganz weg, statt eine Zahl zu zerteilen.
    """
    return " ".join(_UNSICHTBAR.sub("", text).split())


# -- Preis -------------------------------------------------------------------


def parse_preis(text: str, erwartete_waehrung: str) -> Decimal:
    """Einen Preistext in einen Betrag der erwarteten Shopwährung überführen.

    Nennt der Text eine andere Währung, wird nichts umgedeutet und nichts
    geschrieben. Passt die Zahl nicht zu den Trennzeichen dieser Währung, gilt
    sie als unlesbar - lieber ein Klartextfehler als ein Faktor 100 daneben.

    Stehen mehrere Zahlen im Text, gewinnt die erste - aber nur, wenn zwischen
    ihnen etwas steht, das keine Zahl sein kann. «12.90 19.90» ist kein Preis,
    sondern zwei, und wird abgelehnt. Der Selektor soll den Preis treffen; wo das
    nicht reicht, engt ``regex`` im Adapter ein.
    """
    code = (erwartete_waehrung or "").strip().upper()
    regel = TRENNZEICHEN.get(code)
    if regel is None:
        raise ExtraktionFehlt(
            f"Für Währung «{code}» ist keine Trennzeichen-Regel hinterlegt; "
            "der Preis wird nicht geraten"
        )
    roh = _normalisiere(text or "")
    fremd = _erkannte_waehrungen(roh) - {code}
    if fremd:
        raise WaehrungWiderspricht(
            f"Preistext nennt {'/'.join(sorted(fremd))}, der Shop rechnet in {code}: «{roh}»"
        )
    # Das Leerzeichen gruppiert in jeder Währung: als Dezimaltrennzeichen kommt
    # es nirgends vor, also ist es entweder Gruppierung oder gar keine Zahl.
    gruppen, dezimal = regel
    gruppen = gruppen + (" ",)
    lauf = _ZAHLENLAUF.search(roh)
    if lauf is None or not _passt_zur_regel(lauf.group(0), gruppen, dezimal):
        raise ExtraktionFehlt(f"Preis nicht als {code}-Betrag lesbar: «{roh}»")
    zahl = lauf.group(0)
    for zeichen in gruppen:
        zahl = zahl.replace(zeichen, "")
    try:
        betrag = Decimal(zahl.replace(dezimal, "."))
    except InvalidOperation as error:  # pragma: no cover - vom Muster ausgeschlossen
        raise ExtraktionFehlt(f"Preis nicht als {code}-Betrag lesbar: «{roh}»") from error
    if betrag <= 0:
        raise ExtraktionFehlt(f"Preis muss grösser als 0 sein: «{roh}»")
    if betrag != betrag.quantize(Decimal("0.01")):
        # Die Preisspalten sind NUMERIC(12,2); Postgres rundete beim Schreiben
        # ohne ein Wort. Lieber ein Klartextfehler als ein anderer Betrag.
        raise ExtraktionFehlt(
            f"Preis hat mehr als zwei Nachkommastellen und würde beim Speichern "
            f"gerundet: «{roh}»"
        )
    return betrag


def nennt_waehrung(text: str, code: str) -> bool:
    """Ob ein Text diese Währung ausdrücklich nennt."""
    return (code or "").strip().upper() in _erkannte_waehrungen(_normalisiere(text or ""))


def _erkannte_waehrungen(text: str) -> set[str]:
    return {
        code
        for code, muster in WAEHRUNGS_MARKER.items()
        if re.search(muster, text, re.IGNORECASE)
    }


def _passt_zur_regel(zahl: str, gruppen: tuple[str, ...], dezimal: str) -> bool:
    """Gruppiert wird in Dreiergruppen, oder gar nicht.

    Damit fällt «12.90» bei einem EUR-Shop durch statt als 1290 in die Datenbank
    zu wandern: ein Gruppierungszeichen mit zwei Nachkommastellen ist keine
    Gruppierung.
    """
    klasse = "".join(re.escape(zeichen) for zeichen in gruppen)
    muster = rf"(?:\d{{1,3}}(?:[{klasse}]\d{{3}})+|\d+)(?:{re.escape(dezimal)}\d+)?"
    return re.fullmatch(muster, zahl) is not None


# -- Registry ----------------------------------------------------------------

#: Einmal geladen, danach gemerkt. Kein Hot-Reload in Fassung 1 - ein Neustart
#: liest neu, und ein halb getauschter Adapter unter laufendem Betrieb wäre
#: schlimmer als eine Minute Ausfall.
_registry: Registry | None = None


def registry() -> Registry:
    """Alle Adapter, beim ersten Zugriff geladen."""
    global _registry
    if _registry is None:
        _registry = _lade_registry()
    return _registry


def _lade_registry() -> Registry:
    adapter: dict[str, Adapter] = {}
    fehler: list[tuple[str, str]] = []
    for verzeichnis, quelle in _verzeichnisse(fehler):
        gefunden: dict[str, Adapter] = {}
        for pfad in sorted(verzeichnis.glob("*.yaml")):
            try:
                geladen = lade_adapter(pfad, quelle=quelle)
            except AdapterLadefehler as error:
                log.error("Adapter %s übersprungen: %s", pfad, error)
                fehler.append((str(pfad), str(error)))
                continue
            if geladen.id in gefunden:
                grund = (
                    f"{pfad.name}: id «{geladen.id}» ist im selben Verzeichnis "
                    f"schon von {gefunden[geladen.id].datei.name} belegt"
                )
                log.error("Adapter %s übersprungen: %s", pfad, grund)
                fehler.append((str(pfad), grund))
                continue
            gefunden[geladen.id] = geladen
        for kennung, geladen in gefunden.items():
            if kennung in adapter and quelle == QUELLE_NUTZER:
                log.info(
                    "Adapter %s aus Nutzerverzeichnis ersetzt gebündelten", kennung
                )
            adapter[kennung] = geladen
    return Registry(adapter=adapter, fehler=tuple(fehler))


def _verzeichnisse(fehler: list[tuple[str, str]]) -> list[tuple[Path, str]]:
    """Erst gebündelt, dann Nutzerverzeichnis - die spätere Datei gewinnt."""
    verzeichnisse: list[tuple[Path, str]] = []
    if GEBUENDELT_DIR.is_dir():
        verzeichnisse.append((GEBUENDELT_DIR, QUELLE_GEBUENDELT))
    else:
        grund = f"gebündeltes Adapterverzeichnis {GEBUENDELT_DIR} fehlt"
        log.error("%s", grund)
        fehler.append((str(GEBUENDELT_DIR), grund))
    eigen = (os.environ.get(ENV_ADAPTER_DIR) or "").strip()
    if eigen:
        pfad = Path(eigen)
        if pfad.is_dir():
            verzeichnisse.append((pfad, QUELLE_NUTZER))
        else:
            grund = f"{ENV_ADAPTER_DIR} zeigt auf kein Verzeichnis: {eigen}"
            log.error("%s", grund)
            fehler.append((eigen, grund))
    return verzeichnisse


def finde_adapter(url: str) -> Adapter:
    """Adapter für diese Produkt-URL suchen.

    Erst muss die Domain passen (exakt oder als Subdomain), dann das
    ``url_pattern``. Ohne Treffer ein Klartextfehler - der manuelle Weg über
    ``record_offer`` bleibt offen.
    """
    domain = _url_domain(url)
    kandidaten = sorted(
        (
            eintrag
            for eintrag in registry().adapter.values()
            if _deckt_domain(domain, eintrag.domain)
        ),
        key=lambda eintrag: (-len(eintrag.domain), eintrag.id),
    )
    for eintrag in kandidaten:
        if eintrag.url_pattern.search(url):
            return eintrag
    if kandidaten:
        namen = ", ".join(f"«{eintrag.id}»" for eintrag in kandidaten)
        raise AdapterFehlt(
            f"Adapter {namen} deckt {domain} ab, aber kein url_pattern passt auf "
            f"{url}; für diese Seite bleibt der manuelle Weg über record_offer"
        )
    raise AdapterFehlt(
        f"Für {domain} gibt es keinen Adapter; für diesen Shop bleibt der manuelle "
        "Weg über record_offer mit wörtlich abgetippten Texten"
    )


def uebersicht() -> dict[str, Any]:
    """Was die Registry führt - deterministisch sortiert, inklusive Schrott."""
    stand = registry()
    return {
        "adapter": [
            {
                "id": eintrag.id,
                "domain": eintrag.domain,
                "felder": list(eintrag.felder),
                "quelle": eintrag.quelle,
                "datei": eintrag.datei.name if eintrag.datei else None,
                "min_delay_s": eintrag.min_delay_s,
            }
            for eintrag in sorted(stand.adapter.values(), key=lambda e: e.id)
        ],
        "fehler": [{"datei": datei, "grund": grund} for datei, grund in stand.fehler],
    }


def _url_domain(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.lower().rstrip(".").removeprefix("www.")


def _deckt_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)
