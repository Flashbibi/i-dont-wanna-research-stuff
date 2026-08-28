"""Der Fixture-Modus darf das Netz nicht anfassen, der Live-Modus muss durch dieselbe
Tür wie die Engine gehen - und beide dürfen keine Datenbank sehen."""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from app import fetch
from app.adapter_check import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters" / "demo"
DEMO_YAML = str(FIXTURES / "demo.yaml")
DEMO_HTML = str(FIXTURES / "produkt.html")

PRODUKT_URL = "https://demoshop.example/produkt/mg996r"
ROBOTS_ALLES = "User-agent: *\nAllow: /\n"


class Netzprotokoll(list):
    """Die aufgezeichneten Requests - und was dazwischen gewartet wurde."""

    def __init__(self) -> None:
        super().__init__()
        self.schlaefe: list[float] = []


@pytest.fixture
def netz(monkeypatch):
    """Fake-Netz plus angehaltene Uhr; ohne das wartet der Test sechs Sekunden."""
    aufrufe = Netzprotokoll()
    schlaefe = aufrufe.schlaefe

    def handler(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(200, html=Path(DEMO_HTML).read_text(encoding="utf-8"))

    monkeypatch.setattr(fetch, "_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(fetch, "_jetzt", lambda: 1_000.0)
    monkeypatch.setattr(fetch, "_schlafe", schlaefe.append)
    monkeypatch.setattr(fetch, "_letzter_request", {})
    monkeypatch.setattr(fetch, "_robots_cache", {})
    monkeypatch.setattr(fetch, "_robots_sperren", {})
    return aufrufe


def test_the_fixture_mode_shows_raw_text_next_to_the_parsed_value(capsys):
    code = main([DEMO_YAML, "--fixture", DEMO_HTML])

    ausgabe = capsys.readouterr().out
    assert code == 0
    assert "produktname" in ausgabe
    # Rohtext und geparster Wert stehen beide da - der Vergleich ist Handarbeit.
    assert "«CHF 12.90»" in ausgabe
    assert "= 12.90 CHF" in ausgabe
    assert "«an Lager (5 Stück)»" in ausgabe
    assert "«DEMO-4711»" in ausgabe


def test_a_missing_optional_field_is_named_as_missing(capsys, tmp_path):
    seite = tmp_path / "ohne-lager.html"
    seite.write_text(
        Path(DEMO_HTML)
        .read_text(encoding="utf-8")
        .replace('class="lager"', 'class="weg"'),
        encoding="utf-8",
    )

    code = main([DEMO_YAML, "--fixture", str(seite)])

    ausgabe = capsys.readouterr().out
    assert code == 0
    assert "lager_text" in ausgabe
    assert "nicht gefunden" in ausgabe


def test_the_price_is_checked_against_the_given_currency(capsys):
    code = main([DEMO_YAML, "--fixture", DEMO_HTML, "--waehrung", "EUR"])

    assert code == 1
    assert "CHF" in capsys.readouterr().err


def test_the_live_mode_goes_through_the_same_door_as_the_engine(capsys, netz):
    code = main([DEMO_YAML, PRODUKT_URL])

    ausgabe = capsys.readouterr().out
    assert code == 0
    assert PRODUKT_URL in ausgabe
    # robots.txt zuerst, dann die Seite - dieselbe Reihenfolge wie im Dienst.
    assert [request.url.path for request in netz] == ["/robots.txt", "/produkt/mg996r"]
    # Und mit dem Abstand, den demo.yaml verlangt, nicht mit dem Boden.
    assert netz.schlaefe == [6.0]


def test_a_url_the_adapter_does_not_cover_never_reaches_the_network(capsys, netz):
    code = main([DEMO_YAML, "https://fremdshop.example/produkt/servo"])

    assert code == 1
    assert "gehört nicht zu domain" in capsys.readouterr().err
    assert netz == []


def test_a_url_outside_the_url_pattern_never_reaches_the_network(capsys, netz):
    code = main([DEMO_YAML, "https://demoshop.example/suche?q=servo"])

    assert code == 1
    assert "url_pattern" in capsys.readouterr().err
    assert netz == []


def test_url_and_fixture_together_are_refused(capsys):
    assert main([DEMO_YAML, PRODUKT_URL, "--fixture", DEMO_HTML]) == 1
    assert "nicht beides" in capsys.readouterr().err


def test_neither_url_nor_fixture_is_refused(capsys):
    assert main([DEMO_YAML]) == 1
    assert "Entweder" in capsys.readouterr().err


def test_a_broken_adapter_fails_with_a_plain_text_reason(capsys, tmp_path):
    kaputt = tmp_path / "kaputt.yaml"
    kaputt.write_text("schema: 1\nid: 7\n", encoding="utf-8")

    code = main([str(kaputt), "--fixture", DEMO_HTML])

    assert code == 1
    fehler = capsys.readouterr().err
    assert "kaputt.yaml" in fehler
    assert "id muss ein nicht leerer Text sein" in fehler


def test_a_missing_file_fails_without_a_traceback(capsys, tmp_path):
    code = main([DEMO_YAML, "--fixture", str(tmp_path / "gibtsnicht.html")])

    assert code == 1
    assert "Fehler:" in capsys.readouterr().err


def test_a_fixture_that_is_not_utf8_fails_without_a_traceback(capsys, tmp_path):
    seite = tmp_path / "latin1.html"
    seite.write_bytes("<h1>Grösse</h1>".encode("iso-8859-1"))

    code = main([DEMO_YAML, "--fixture", str(seite)])

    assert code == 1
    assert "nicht UTF-8" in capsys.readouterr().err


def test_the_tool_stays_clear_of_the_database():
    """Abschnitt H verlangt wörtlich, dass das Werkzeug weder procurement noch eine
    Datenbank importiert."""
    baum = ast.parse(Path("app/adapter_check.py").read_text(encoding="utf-8"))
    module = set()
    for knoten in ast.walk(baum):
        if isinstance(knoten, ast.Import):
            module.update(alias.name for alias in knoten.names)
        elif isinstance(knoten, ast.ImportFrom):
            module.add(knoten.module or "")

    assert module == {
        "__future__",
        "argparse",
        "sys",
        "pathlib",
        "urllib.parse",
        "adapter",
        "fetch",
    }
