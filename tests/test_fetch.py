# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Die eine Tür zum Internet - geprüft, ohne sie zu benutzen.

Kein Test hier fasst ein echtes Netz an: HTTP läuft über ``httpx.MockTransport``,
Uhr und Schlaf sind eine Attrappe. Geschlafen wird trotzdem nie in Echtzeit, die
Wartezeiten werden nur aufgezeichnet - sonst dauerte allein diese Datei Minuten.
"""

from __future__ import annotations

import httpx
import pytest

from app import fetch


ROBOTS_ALLES = "User-agent: *\nAllow: /\n"
ROBOTS_VERBIETET = "User-agent: *\nDisallow: /produkt/\n"
SEITE = "<html><body><h1>Servo</h1></body></html>"

PRODUKT_URL = "https://shop.example.ch/produkt/servo"


class Uhr:
    """Monotone Zeit zum Anfassen: Schlafen springt vorwärts statt zu warten."""

    def __init__(self, start: float = 1_000.0):
        self.zeit = start
        self.schlaefe: list[float] = []

    def jetzt(self) -> float:
        return self.zeit

    def schlafe(self, sekunden: float) -> None:
        self.schlaefe.append(sekunden)
        self.zeit += sekunden


@pytest.fixture
def uhr(monkeypatch):
    """Uhr, Schlaf und der prozessweite Zustand - für jeden Test frisch."""
    zeitgeber = Uhr()
    monkeypatch.setattr(fetch, "_jetzt", zeitgeber.jetzt)
    monkeypatch.setattr(fetch, "_schlafe", zeitgeber.schlafe)
    monkeypatch.setattr(fetch, "_naechster_slot", {})
    monkeypatch.setattr(fetch, "_robots_cache", {})
    return zeitgeber


def netz(monkeypatch, handler):
    """Fake-Netz einhängen und die Requests aufzeichnen."""
    aufrufe: list[httpx.Request] = []

    def merkend(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request)
        return handler(request)

    monkeypatch.setattr(fetch, "_transport", lambda: httpx.MockTransport(merkend))
    return aufrufe


def shop(robots: str = ROBOTS_ALLES, seite: str = SEITE):
    """Handler für einen gewöhnlichen Shop: robots.txt und eine Produktseite."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, html=seite)

    return handler


def erlaubte_robots(uhr: Uhr, *herkuenfte: str) -> dict[str, fetch._RobotsStand]:
    """Robots-Cache vorbelegen, damit ein Test nur die Seitenabrufe zählt."""
    return {
        herkunft: fetch._RobotsStand(uhr.zeit + fetch.ROBOTS_CACHE_S_ERFOLG)
        for herkunft in herkuenfte
    }


def pfade(aufrufe: list[httpx.Request]) -> list[str]:
    return [f"{request.url.host}{request.url.path}" for request in aufrufe]


def test_every_request_names_the_tool_its_version_and_its_address(uhr, monkeypatch):
    aufrufe = netz(monkeypatch, shop())

    ergebnis = fetch.hole_seite(PRODUKT_URL)

    assert ergebnis.status == 200
    assert ergebnis.final_url == PRODUKT_URL
    assert "<h1>Servo</h1>" in ergebnis.text
    # Auch der robots-Abruf reist unter demselben Namen.
    assert len(aufrufe) == 2
    for request in aufrufe:
        assert request.headers["User-Agent"] == fetch.USER_AGENT
        assert request.headers["Accept-Language"] == fetch.ACCEPT_LANGUAGE
    assert fetch.USER_AGENT.startswith("beschaffung/")
    assert "github.com/Flashbibi" in fetch.USER_AGENT


def test_two_pages_from_the_same_domain_keep_the_minimum_delay(uhr, monkeypatch):
    monkeypatch.setattr(fetch, "_robots_cache", erlaubte_robots(uhr, "https://shop.example.ch"))
    netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL)
    fetch.hole_seite("https://shop.example.ch/produkt/kabel")

    # Der erste Abruf darf sofort, der zweite wartet den vollen Abstand ab.
    assert uhr.schlaefe == [fetch.DEFAULT_MIN_DELAY_S]


def test_different_domains_do_not_wait_for_each_other(uhr, monkeypatch):
    monkeypatch.setattr(
        fetch,
        "_robots_cache",
        erlaubte_robots(uhr, "https://shop.example.ch", "https://anderer.example.de"),
    )
    netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL)
    fetch.hole_seite("https://anderer.example.de/produkt/servo")

    assert uhr.schlaefe == []


def test_www_and_the_bare_host_count_as_one_domain(uhr, monkeypatch):
    monkeypatch.setattr(
        fetch,
        "_robots_cache",
        erlaubte_robots(uhr, "https://shop.example.ch", "https://www.shop.example.ch"),
    )
    netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL)
    fetch.hole_seite("https://www.shop.example.ch/produkt/kabel")

    assert uhr.schlaefe == [fetch.DEFAULT_MIN_DELAY_S]


def test_an_adapter_may_raise_the_delay(uhr, monkeypatch):
    monkeypatch.setattr(fetch, "_robots_cache", erlaubte_robots(uhr, "https://shop.example.ch"))
    netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL, min_delay_s=8)
    fetch.hole_seite("https://shop.example.ch/produkt/kabel", min_delay_s=8)

    assert uhr.schlaefe == [8.0]


def test_an_adapter_may_not_lower_the_delay(uhr, monkeypatch):
    monkeypatch.setattr(fetch, "_robots_cache", erlaubte_robots(uhr, "https://shop.example.ch"))
    netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL, min_delay_s=1)
    fetch.hole_seite("https://shop.example.ch/produkt/kabel", min_delay_s=0)

    assert uhr.schlaefe == [fetch.DEFAULT_MIN_DELAY_S]


def test_a_disallowed_path_is_never_requested(uhr, monkeypatch):
    aufrufe = netz(monkeypatch, shop(robots=ROBOTS_VERBIETET))

    with pytest.raises(fetch.RobotsVerboten) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "shop.example.ch" in str(fehler.value)
    assert "/produkt/servo" in str(fehler.value)
    assert pfade(aufrufe) == ["shop.example.ch/robots.txt"]


def test_a_missing_robots_txt_allows_everything(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="nicht da")
        return httpx.Response(200, html=SEITE)

    aufrufe = netz(monkeypatch, handler)

    ergebnis = fetch.hole_seite(PRODUKT_URL)

    assert ergebnis.status == 200
    assert pfade(aufrufe) == ["shop.example.ch/robots.txt", "shop.example.ch/produkt/servo"]


def test_an_unreachable_robots_txt_is_not_guessed_around(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectTimeout("keine Antwort")
        return httpx.Response(200, html=SEITE)

    aufrufe = netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchTemporaerFehler) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "robots.txt von shop.example.ch nicht erreichbar" in str(fehler.value)
    assert pfade(aufrufe) == ["shop.example.ch/robots.txt"]


def test_a_server_error_on_robots_txt_is_temporary(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(503, text="wartung")
        return httpx.Response(200, html=SEITE)

    aufrufe = netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchTemporaerFehler) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "HTTP 503" in str(fehler.value)
    assert pfade(aufrufe) == ["shop.example.ch/robots.txt"]


def test_robots_txt_is_not_fetched_twice_within_its_ttl(uhr, monkeypatch):
    aufrufe = netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL)
    fetch.hole_seite("https://shop.example.ch/produkt/kabel")

    assert pfade(aufrufe) == [
        "shop.example.ch/robots.txt",
        "shop.example.ch/produkt/servo",
        "shop.example.ch/produkt/kabel",
    ]


def test_an_expired_ttl_reads_robots_txt_again(uhr, monkeypatch):
    aufrufe = netz(monkeypatch, shop())

    fetch.hole_seite(PRODUKT_URL)
    uhr.zeit += fetch.ROBOTS_CACHE_S_ERFOLG + 1
    fetch.hole_seite("https://shop.example.ch/produkt/kabel")

    assert pfade(aufrufe).count("shop.example.ch/robots.txt") == 2


def test_a_failed_robots_fetch_is_remembered_for_a_shorter_time(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500, text="kaputt")
        return httpx.Response(200, html=SEITE)

    aufrufe = netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchTemporaerFehler):
        fetch.hole_seite(PRODUKT_URL)
    # Innerhalb der kurzen TTL kommt derselbe Fehler ohne zweiten Abruf.
    with pytest.raises(fetch.FetchTemporaerFehler):
        fetch.hole_seite(PRODUKT_URL)
    assert len(aufrufe) == 1

    uhr.zeit += fetch.ROBOTS_CACHE_S_FEHLSCHLAG + 1
    with pytest.raises(fetch.FetchTemporaerFehler):
        fetch.hole_seite(PRODUKT_URL)
    assert len(aufrufe) == 2


def test_a_blocking_shop_is_named_honestly(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(403, text="verboten")

    netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchAbgelehnt) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "Shop blockt automatisierte Zugriffe (HTTP 403)" in str(fehler.value)


def test_too_many_requests_is_a_temporary_failure(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(429, text="langsamer")

    netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchTemporaerFehler) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "HTTP 429" in str(fehler.value)


def test_an_oversized_page_is_not_read_to_the_end(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(200, content=b"x" * (fetch.MAX_BYTES + 1))

    netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchAbgelehnt) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "Seite grösser als 2 MB" in str(fehler.value)


def test_a_redirect_leaving_the_domain_ends_the_fetch(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        if request.url.host == "shop.example.ch":
            return httpx.Response(
                302, headers={"Location": "https://fremd.example.org/produkt/servo"}
            )
        return httpx.Response(200, html=SEITE)

    netz(monkeypatch, handler)

    with pytest.raises(fetch.FetchAbgelehnt) as fehler:
        fetch.hole_seite(PRODUKT_URL)

    assert "Weiterleitung verlässt shop.example.ch" in str(fehler.value)


def test_a_redirect_inside_the_domain_yields_the_final_url(uhr, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        if request.url.path == "/produkt/servo":
            return httpx.Response(
                301, headers={"Location": "https://shop.example.ch/produkt/servo-mg996r"}
            )
        return httpx.Response(200, html=SEITE)

    netz(monkeypatch, handler)

    ergebnis = fetch.hole_seite(PRODUKT_URL)

    assert ergebnis.final_url == "https://shop.example.ch/produkt/servo-mg996r"


def test_an_unusable_url_never_reaches_the_network(uhr, monkeypatch):
    aufrufe = netz(monkeypatch, shop())

    for kaputt in ("ftp://shop.example.ch/servo", "https://nutzer:geheim@shop.example.ch/servo"):
        with pytest.raises(fetch.FetchAbgelehnt, match="HTTP\\(S\\)-URL"):
            fetch.hole_seite(kaputt)
    assert aufrufe == []


def test_the_encoding_falls_back_to_the_html_itself(uhr, monkeypatch):
    seite = '<html><head><meta charset="iso-8859-1"></head><body>Grösse</body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(
            200,
            content=seite.encode("iso-8859-1"),
            headers={"Content-Type": "text/html"},
        )

    netz(monkeypatch, handler)

    ergebnis = fetch.hole_seite(PRODUKT_URL)

    assert "Grösse" in ergebnis.text
