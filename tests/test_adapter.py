# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Schema, Extraktion, Preis-Parser und Registry der deklarativen Adapter.

Kein Netz, keine Datenbank: hier wird nur gelesen, was in Dateien steht.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

import pytest

from app import adapter
from app.adapter import (
    AdapterFehlt,
    AdapterLadefehler,
    ExtraktionFehlt,
    WaehrungWiderspricht,
    extrahiere,
    finde_adapter,
    lade_adapter,
    parse_preis,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters" / "demo"
DEMO_YAML = FIXTURES / "demo.yaml"
DEMO_HTML = FIXTURES / "produkt.html"

GUELTIG = """
schema: 1
id: demo
domain: demoshop.example
product:
  url_pattern: "^https://demoshop\\\\.example/produkt/"
  fields:
    produktname:
      selector: "h1"
    preis:
      selector: ".preis"
      parse: price
"""


def schreibe(tmp_path: Path, inhalt: str, name: str = "demo.yaml") -> Path:
    pfad = tmp_path / name
    pfad.write_text(inhalt, encoding="utf-8")
    return pfad


@pytest.fixture
def demo():
    return lade_adapter(DEMO_YAML)


@pytest.fixture
def seite():
    return DEMO_HTML.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def frische_registry(monkeypatch):
    """Die Registry ist Modulzustand; ohne Reset färbt ein Test den nächsten."""
    monkeypatch.setattr(adapter, "_registry", None)
    monkeypatch.delenv(adapter.ENV_ADAPTER_DIR, raising=False)


# -- Schema ------------------------------------------------------------------


def test_the_bundled_example_adapter_loads(tmp_path):
    # Die .example-Datei ist die Vorlage für echte Adapter - sie muss gültig sein.
    vorlage = Path("adapters/beispiel.yaml.example").read_text(encoding="utf-8")

    geladen = lade_adapter(schreibe(tmp_path, vorlage, "beispielshop.yaml"))

    assert geladen.id == "beispielshop"
    assert geladen.domain == "beispielshop.ch"
    assert geladen.min_delay_s == 8


def test_the_demo_adapter_describes_all_five_fields(demo):
    assert demo.id == "demo"
    assert demo.domain == "demoshop.example"
    assert demo.min_delay_s == 6
    assert list(demo.felder) == list(adapter.FELDNAMEN)


def test_an_unknown_key_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG + "\nfarbe: blau\n")

    with pytest.raises(AdapterLadefehler) as fehler:
        lade_adapter(pfad)

    assert "demo.yaml" in str(fehler.value)
    assert "«farbe»" in str(fehler.value)


def test_an_unknown_key_inside_a_field_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace('      selector: "h1"', '      selector: "h1"\n      trim: true'))

    with pytest.raises(AdapterLadefehler, match="product.fields.produktname"):
        lade_adapter(pfad)


def test_a_wrong_type_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace("id: demo", "id: 7"))

    with pytest.raises(AdapterLadefehler, match="id muss ein nicht leerer Text sein"):
        lade_adapter(pfad)


def test_a_regex_without_a_capture_group_is_a_load_error(tmp_path):
    pfad = schreibe(
        tmp_path,
        GUELTIG.replace('      selector: ".preis"', '      selector: ".preis"\n      regex: "CHF .*"'),
    )

    with pytest.raises(AdapterLadefehler) as fehler:
        lade_adapter(pfad)

    assert "genau eine Capture-Group" in str(fehler.value)


def test_two_capture_groups_are_too_many_as_well(tmp_path):
    pfad = schreibe(
        tmp_path,
        GUELTIG.replace('      selector: ".preis"', '      selector: ".preis"\n      regex: "(CHF) (.*)"'),
    )

    with pytest.raises(AdapterLadefehler, match="genau eine Capture-Group"):
        lade_adapter(pfad)


def test_an_invalid_id_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace("id: demo", "id: Demo_Shop"))

    with pytest.raises(AdapterLadefehler) as fehler:
        lade_adapter(pfad)

    assert "demo.yaml" in str(fehler.value)
    assert "id «Demo_Shop»" in str(fehler.value)


def test_an_invalid_domain_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace("domain: demoshop.example", "domain: https://Demoshop.example/"))

    with pytest.raises(AdapterLadefehler, match="domain"):
        lade_adapter(pfad)


def test_a_missing_mandatory_field_is_a_load_error(tmp_path):
    ohne_preis = GUELTIG.split("    preis:")[0]
    pfad = schreibe(tmp_path, ohne_preis)

    with pytest.raises(AdapterLadefehler, match="product.fields.preis fehlt"):
        lade_adapter(pfad)


def test_mandatory_fields_may_not_be_declared_optional(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace('      selector: "h1"', '      selector: "h1"\n      optional: true'))

    with pytest.raises(AdapterLadefehler, match="darf nicht optional sein"):
        lade_adapter(pfad)


def test_price_parsing_exists_only_for_the_price_field(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace('      selector: "h1"', '      selector: "h1"\n      parse: price'))

    with pytest.raises(AdapterLadefehler, match="nur «text»"):
        lade_adapter(pfad)


def test_the_price_field_demands_parse_price(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace("      parse: price", "      parse: text"))

    with pytest.raises(AdapterLadefehler, match="muss «price» sein"):
        lade_adapter(pfad)


def test_a_wrong_schema_version_is_a_load_error(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace("schema: 1", "schema: 2"))

    with pytest.raises(AdapterLadefehler, match="schema muss 1 sein"):
        lade_adapter(pfad)


def test_a_broken_selector_shows_up_at_load_time(tmp_path):
    pfad = schreibe(tmp_path, GUELTIG.replace('selector: "h1"', 'selector: "h1[["'))

    with pytest.raises(AdapterLadefehler, match="CSS-Selektor"):
        lade_adapter(pfad)


def test_broken_yaml_names_the_file(tmp_path):
    pfad = schreibe(tmp_path, "schema: 1\n  id: [unschliessbar\n")

    with pytest.raises(AdapterLadefehler) as fehler:
        lade_adapter(pfad)

    assert "demo.yaml" in str(fehler.value)


# -- Extraktion --------------------------------------------------------------


def test_the_demo_page_yields_every_field_literally(demo, seite):
    roh = extrahiere(demo, seite)

    assert roh == {
        "produktname": "MG996R Servo Metallgetriebe",
        "preis": "CHF 12.90",
        "lieferzeit_text": "Lieferzeit: 2–3 Werktage",
        "lager_text": "an Lager (5 Stück)",
        "artikelnummer": "DEMO-4711",
    }


def test_whitespace_is_normalized_to_single_spaces(demo, seite):
    # Die Fixture bricht den Titel über drei Zeilen und schreibt den Preis mit
    # geschütztem Leerzeichen.
    roh = extrahiere(demo, seite)

    assert "  " not in roh["produktname"]
    assert "\xa0" not in roh["preis"]


def test_a_missing_mandatory_field_writes_nothing(demo, seite):
    ohne_preis = seite.replace('class="betrag"', 'class="weg"')

    with pytest.raises(ExtraktionFehlt) as fehler:
        extrahiere(demo, ohne_preis)

    assert "«preis»" in str(fehler.value)
    assert ".preis .betrag" in str(fehler.value)


def test_an_empty_match_counts_as_missing(demo, seite):
    leer = seite.replace("CHF&nbsp;12.90", "   ")

    with pytest.raises(ExtraktionFehlt, match="Treffer ohne Text"):
        extrahiere(demo, leer)


def test_a_missing_optional_field_yields_none(demo, seite):
    ohne_lager = seite.replace('class="lager"', 'class="weg"')

    roh = extrahiere(demo, ohne_lager)

    assert roh["lager_text"] is None
    assert roh["preis"] == "CHF 12.90"


def test_the_attribute_mode_reads_the_attribute_instead_of_the_text(demo, seite):
    roh = extrahiere(demo, seite)

    # Das meta-Tag hat gar keinen Textinhalt - ohne attribute käme nichts.
    assert roh["artikelnummer"] == "DEMO-4711"


def test_a_missing_attribute_counts_as_missing(demo, seite):
    ohne_sku = seite.replace('content="DEMO-4711"', 'inhalt="DEMO-4711"')

    roh = extrahiere(demo, ohne_sku)

    assert roh["artikelnummer"] is None


def test_the_regex_narrows_the_raw_text(tmp_path, seite):
    mit_regex = DEMO_YAML.read_text(encoding="utf-8").replace(
        '      selector: "p.lager"',
        '      selector: "p.lager"\n      regex: "an Lager \\\\((\\\\d+) Stück\\\\)"',
    )
    geladen = lade_adapter(schreibe(tmp_path, mit_regex))

    assert extrahiere(geladen, seite)["lager_text"] == "5"


def test_a_capture_group_that_catches_nothing_is_a_plain_error(tmp_path, seite):
    """Eine optionale Gruppe kann mittreffen, ohne etwas zu fangen."""
    mit_regex = DEMO_YAML.read_text(encoding="utf-8").replace(
        '      selector: "p.lager"',
        '      selector: "p.lager"\n      regex: "an Lager( \\\\d+)?"',
    )
    geladen = lade_adapter(schreibe(tmp_path, mit_regex))

    # Kein Absturz, kein Traceback - das Feld ist optional und bleibt leer.
    assert extrahiere(geladen, seite.replace("an Lager (5 Stück)", "an Lager"))[
        "lager_text"
    ] is None


def test_a_regex_without_a_match_counts_as_missing(tmp_path, seite):
    mit_regex = DEMO_YAML.read_text(encoding="utf-8").replace(
        '      selector: "p.lager"',
        '      selector: "p.lager"\n      regex: "(ausverkauft)"',
    )
    geladen = lade_adapter(schreibe(tmp_path, mit_regex))

    assert extrahiere(geladen, seite)["lager_text"] is None


# -- Preis -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, waehrung, erwartet",
    [
        ("CHF 12.90", "CHF", "12.90"),
        ("1'234.50", "CHF", "1234.50"),
        ("1’234.50", "CHF", "1234.50"),
        ("12.90", "CHF", "12.90"),
        ("Fr. 7.50", "CHF", "7.50"),
        ("CHF 1'234.50 inkl. MwSt.", "CHF", "1234.50"),
        ("1.234,56 €", "EUR", "1234.56"),
        ("EUR 12,90", "EUR", "12.90"),
        ("$1,299.00", "USD", "1299.00"),
        ("£9.99", "GBP", "9.99"),
        # Schweizer Bundesschreibweise gruppiert mit Leerzeichen, geschützt oder
        # nicht - das darf nicht als «1» in der Datenbank landen.
        ("CHF 1 234.50", "CHF", "1234.50"),
        ("CHF 1\u00a0234.50", "CHF", "1234.50"),
        ("1\u202f234,56 €", "EUR", "1234.56"),
    ],
)
def test_the_price_parser_reads_the_common_notations(text, waehrung, erwartet):
    assert parse_preis(text, waehrung) == Decimal(erwartet)


@pytest.mark.parametrize(
    "text, waehrung",
    [
        # Ein in Spans zerlegter Preis kommt so aus der Extraktion. Lieber ein
        # Fehler als stillschweigend die Rappen weglassen.
        ("19 ,99 €", "EUR"),
        ("CHF 12. 90", "CHF"),
        ("1.234 ,50 €", "EUR"),
        # Zwei Zahlen nebeneinander sind kein Preis.
        ("12.90 19.90", "CHF"),
    ],
)
def test_a_price_torn_apart_is_refused_instead_of_truncated(text, waehrung):
    with pytest.raises(ExtraktionFehlt):
        parse_preis(text, waehrung)


@pytest.mark.parametrize(
    "text, erwartete, fremde",
    [
        ("USD12.90", "CHF", "USD"),
        ("12.90GBP", "USD", "GBP"),
        ("SFr. 12.90", "EUR", "CHF"),
    ],
)
def test_a_currency_code_glued_to_the_number_still_contradicts(text, erwartete, fremde):
    with pytest.raises(WaehrungWiderspricht, match=fremde):
        parse_preis(text, erwartete)


@pytest.mark.parametrize("unsichtbar", ["\u200b", "\u00ad", "\u2060", "\ufeff"])
def test_an_invisible_character_does_not_cut_a_price_in_half(unsichtbar):
    """Shops streuen sie ein; str.split sieht sie nicht als Whitespace."""
    assert parse_preis(f"CHF 1{unsichtbar}234.50", "CHF") == Decimal("1234.50")


def test_a_price_the_column_cannot_hold_is_refused_not_rounded():
    # preis_chf ist NUMERIC(12,2); Postgres rundete sonst wortlos auf 0.10.
    with pytest.raises(ExtraktionFehlt, match="Nachkommastellen"):
        parse_preis("CHF 0.09562", "CHF")
    # Nachlaufende Nullen sind keine zusätzliche Genauigkeit.
    assert parse_preis("CHF 12.900", "CHF") == Decimal("12.90")


def test_a_word_containing_a_currency_code_is_no_currency():
    # «Neuromodul» enthält «euro», ist aber keiner.
    assert parse_preis("Neuromodul 5.00", "CHF") == Decimal("5.00")


def test_a_foreign_currency_in_the_text_writes_nothing():
    with pytest.raises(WaehrungWiderspricht) as fehler:
        parse_preis("€ 12,90", "CHF")

    assert "EUR" in str(fehler.value)
    assert "CHF" in str(fehler.value)


def test_garbage_is_not_a_price():
    with pytest.raises(ExtraktionFehlt) as fehler:
        parse_preis("auf Anfrage", "CHF")

    assert "auf Anfrage" in str(fehler.value)


def test_a_number_that_breaks_the_currency_rule_is_refused():
    # «12.90» bei einem EUR-Shop wäre nach EUR-Regel 1290 - lieber ein Fehler.
    with pytest.raises(ExtraktionFehlt, match="EUR-Betrag"):
        parse_preis("12.90 EUR", "EUR")


def test_an_unknown_currency_is_not_guessed():
    with pytest.raises(ExtraktionFehlt, match="Trennzeichen-Regel"):
        parse_preis("12.90", "JPY")


def test_a_price_of_zero_is_no_price():
    with pytest.raises(ExtraktionFehlt, match="grösser als 0"):
        parse_preis("CHF 0.00", "CHF")


# -- Registry ----------------------------------------------------------------


def kopiere_demo(ziel: Path, *, kennung: str = "demo", name: str = "demo.yaml") -> Path:
    inhalt = DEMO_YAML.read_text(encoding="utf-8").replace("id: demo", f"id: {kennung}")
    pfad = ziel / name
    pfad.write_text(inhalt, encoding="utf-8")
    return pfad


def test_the_registry_reads_bundled_and_own_adapters(tmp_path, monkeypatch):
    gebuendelt = tmp_path / "gebuendelt"
    eigen = tmp_path / "eigen"
    gebuendelt.mkdir()
    eigen.mkdir()
    kopiere_demo(gebuendelt, kennung="demo")
    kopiere_demo(eigen, kennung="eigenshop", name="eigen.yaml")
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)
    monkeypatch.setenv(adapter.ENV_ADAPTER_DIR, str(eigen))

    uebersicht = adapter.uebersicht()

    assert [eintrag["id"] for eintrag in uebersicht["adapter"]] == ["demo", "eigenshop"]
    assert [eintrag["quelle"] for eintrag in uebersicht["adapter"]] == [
        "gebuendelt",
        "nutzer",
    ]
    assert uebersicht["adapter"][0]["felder"] == list(adapter.FELDNAMEN)
    assert uebersicht["fehler"] == []


def test_the_user_directory_replaces_a_bundled_adapter(
    tmp_path, monkeypatch, caplog
):
    gebuendelt = tmp_path / "gebuendelt"
    eigen = tmp_path / "eigen"
    gebuendelt.mkdir()
    eigen.mkdir()
    kopiere_demo(gebuendelt)
    geflickt = DEMO_YAML.read_text(encoding="utf-8").replace(
        'selector: "h1.produkt-titel"', 'selector: "h1.neuer-titel"'
    )
    (eigen / "demo.yaml").write_text(geflickt, encoding="utf-8")
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)
    monkeypatch.setenv(adapter.ENV_ADAPTER_DIR, str(eigen))

    with caplog.at_level(logging.INFO, logger="beschaffung.adapter"):
        stand = adapter.registry()

    assert stand.adapter["demo"].quelle == "nutzer"
    assert stand.adapter["demo"].felder["produktname"].selector == "h1.neuer-titel"
    assert "aus Nutzerverzeichnis ersetzt gebündelten" in caplog.text


def test_two_files_with_the_same_id_in_one_directory_are_an_error(
    tmp_path, monkeypatch
):
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    kopiere_demo(gebuendelt, name="a.yaml")
    kopiere_demo(gebuendelt, name="b.yaml")
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)

    stand = adapter.registry()

    assert set(stand.adapter) == {"demo"}
    assert stand.adapter["demo"].datei.name == "a.yaml"
    assert len(stand.fehler) == 1
    assert "schon von a.yaml belegt" in stand.fehler[0][1]


def test_a_file_that_is_not_utf8_only_takes_itself_down(tmp_path, monkeypatch):
    """Ohne eigenen Zweig entkäme der UnicodeDecodeError der ganzen Registry."""
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    kopiere_demo(gebuendelt, kennung="heil", name="heil.yaml")
    (gebuendelt / "latin1.yaml").write_bytes(
        DEMO_YAML.read_text(encoding="utf-8")
        .replace("id: demo", "id: latin1")
        .encode("iso-8859-1")
    )
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)

    uebersicht = adapter.uebersicht()

    assert [eintrag["id"] for eintrag in uebersicht["adapter"]] == ["heil"]
    assert "nicht UTF-8" in uebersicht["fehler"][0]["grund"]


def test_a_broken_file_does_not_take_the_rest_down(tmp_path, monkeypatch, caplog):
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    kopiere_demo(gebuendelt, kennung="heil", name="heil.yaml")
    (gebuendelt / "kaputt.yaml").write_text("schema: 1\nid: 7\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)

    with caplog.at_level(logging.ERROR, logger="beschaffung.adapter"):
        uebersicht = adapter.uebersicht()

    assert [eintrag["id"] for eintrag in uebersicht["adapter"]] == ["heil"]
    assert len(uebersicht["fehler"]) == 1
    assert "kaputt.yaml" in uebersicht["fehler"][0]["grund"]
    assert "kaputt.yaml" in caplog.text


def test_the_example_file_is_not_loaded(tmp_path, monkeypatch):
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    kopiere_demo(gebuendelt, name="beispiel.yaml.example")
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)

    stand = adapter.registry()

    assert stand.adapter == {}
    assert stand.fehler == ()


def test_a_misconfigured_user_directory_stays_visible(tmp_path, monkeypatch):
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)
    monkeypatch.setenv(adapter.ENV_ADAPTER_DIR, str(tmp_path / "gibtsnicht"))

    uebersicht = adapter.uebersicht()

    assert uebersicht["adapter"] == []
    assert "BESCHAFFUNG_ADAPTER_DIR" in uebersicht["fehler"][0]["grund"]


def test_a_missing_bundled_directory_stays_visible(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", tmp_path / "gibtsnicht")

    uebersicht = adapter.uebersicht()

    assert "fehlt" in uebersicht["fehler"][0]["grund"]


def test_the_lookup_checks_domain_and_url_pattern(tmp_path, monkeypatch):
    gebuendelt = tmp_path / "gebuendelt"
    gebuendelt.mkdir()
    kopiere_demo(gebuendelt)
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", gebuendelt)

    assert finde_adapter("https://demoshop.example/produkt/servo").id == "demo"
    assert finde_adapter("https://www.demoshop.example/produkt/servo").id == "demo"

    with pytest.raises(AdapterFehlt, match="kein url_pattern passt"):
        finde_adapter("https://demoshop.example/suche?q=servo")
    with pytest.raises(AdapterFehlt, match="record_offer"):
        finde_adapter("https://fremdshop.example/produkt/servo")
