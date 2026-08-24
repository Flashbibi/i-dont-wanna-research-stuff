# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Die eine Tür zum Internet für Shop-Adapter.

Jeder Adapter-Abruf läuft durch :func:`hole_seite`. Das ist kein Stilwunsch,
sondern der Grund, warum Höflichkeit hier strukturell ist statt optional:
robots.txt, Mindestabstand pro Domain und ein ehrlicher User-Agent sitzen in
dieser einen Funktion. Es gibt keinen Schalter, der das abstellt, und keinen
zweiten Weg daran vorbei.

Bewusst nicht in diesem Modul: Retries, Backoff, Antwort-Caching,
Cookie-Verwaltung, POST. Ein temporärer Fehler kommt als solcher zurück und der
Aufrufer - Mensch oder Modell - entscheidet, ob er es nochmal versucht. Ein
automatischer Retry wäre genau die stille Verdopplung der Last, die der
Mindestabstand verhindern soll.

Der Warenkorb-Pfad (``cart.py``) bleibt aussen vor: das ist ein Session-Ablauf
mit eigenem, dokumentiertem Profil, kein Seitenabruf.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .version import __version__


#: Ehrlicher Absender: Name, Version und die Adresse, unter der nachlesbar ist,
#: was dieses Werkzeug tut. Gleiche Machart wie im Update-Check.
USER_AGENT = (
    f"beschaffung/{__version__} "
    "(+https://github.com/Flashbibi/i-dont-wanna-research-stuff)"
)

#: Name, unter dem wir uns in robots.txt wiedererkennen.
ROBOTS_AGENT = "beschaffung"

#: Mindestabstand zwischen zwei Requests an dieselbe Domain, prozessweit. Ein
#: Adapter darf ihn erhöhen, niemals senken - deshalb steht hier ein Boden und
#: kein Vorschlag.
DEFAULT_MIN_DELAY_S = 5.0

#: Kurz genug, dass ein hängender Shop keine Anfrage blockiert.
TIMEOUT_S = 10.0

#: Eine Produktseite ist kleiner. Alles darüber lesen wir nicht zu Ende.
MAX_BYTES = 2_000_000

#: Eine Handvoll Weiterleitungen ist normal, eine Kette davon nicht.
MAX_REDIRECTS = 5

#: Deutschsprachige Seite bevorzugen; die Texte landen wörtlich in der Datenbank.
ACCEPT_LANGUAGE = "de-CH,de;q=0.9,en;q=0.8"

#: robots.txt wird gemerkt statt vor jeder Seite neu geholt - gleiches Muster
#: wie der Update-Check: Erfolg lange, Fehlschlag kurz.
ROBOTS_CACHE_S_ERFOLG = 24 * 60 * 60
ROBOTS_CACHE_S_FEHLSCHLAG = 60 * 60

#: Zeichenkodierung aus dem HTML selbst, falls der Header keine nennt.
_META_CHARSET = re.compile(
    rb'<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9_:.+-]+)', re.IGNORECASE
)


class FetchFehler(ValueError):
    """Klartext-Fehler; die Meldung ist für Oberfläche und MCP bestimmt."""


class RobotsVerboten(FetchFehler):
    """robots.txt der Domain verbietet diesen Pfad. Wir fragen gar nicht erst."""


class FetchTemporaerFehler(FetchFehler):
    """Zeitüberschreitung, Verbindungsfehler, HTTP 5xx oder 429.

    Wiederholbar - aber die Wiederholung entscheidet der Aufrufer, nicht dieses
    Modul.
    """


class FetchAbgelehnt(FetchFehler):
    """Dauerhafte Ablehnung: übrige 4xx, unbrauchbare URL, zu grosse Seite."""


@dataclass(frozen=True)
class FetchErgebnis:
    """Was von einem Abruf übrig bleibt - die finale URL zählt, nicht die gerufene."""

    final_url: str
    status: int
    text: str


@dataclass(frozen=True)
class _Ziel:
    """Zerlegte URL: Herkunft für robots.txt, Domain für den Mindestabstand."""

    url: str
    herkunft: str
    domain: str
    pfad: str

    @property
    def robots_url(self) -> str:
        return self.herkunft + "/robots.txt"


@dataclass(frozen=True)
class _RobotsStand:
    """Was wir über die robots.txt einer Herkunft wissen, mit Ablaufzeitpunkt.

    ``regeln is None`` heisst "es gibt keine Regeln" (HTTP 4xx). Ein gesetztes
    ``fehler`` heisst "der Abruf scheiterte" - dann wird nicht geraten, sondern
    abgebrochen, bis der Eintrag abläuft.
    """

    ablauf: float
    regeln: RobotFileParser | None = None
    fehler: str | None = None


#: Prozessweiter Zustand. Ohne Sperre wäre der Mindestabstand wirkungslos:
#: FastAPI führt sync-Endpoints im Threadpool aus, zwei Threads würden beide
#: "gerade frei" sehen.
_sperre = threading.Lock()

#: Domain -> monotoner Zeitpunkt, ab dem der nächste Request starten darf.
_naechster_slot: dict[str, float] = {}

#: Herkunft -> Stand ihrer robots.txt.
_robots_cache: dict[str, _RobotsStand] = {}


def _jetzt() -> float:
    """Monotone Uhr - in Tests der Ansatzpunkt."""
    return time.monotonic()


def _schlafe(sekunden: float) -> None:
    """Warten - in Tests der Ansatzpunkt."""
    time.sleep(sekunden)


def _transport() -> Any | None:
    """Transport des Clients; ``None`` heisst echtes Netz.

    Der einzige Ansatzpunkt für Tests: dort hängt hier ein
    ``httpx.MockTransport``, und alles davor - Header, Timeout, Weiterleitungen,
    Grössenlimit - bleibt der echte Weg.
    """
    return None


def _client() -> httpx.Client:
    """Der Client, mit dem dieses Modul spricht. Für robots.txt und Seite derselbe."""
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": ACCEPT_LANGUAGE,
        },
        timeout=TIMEOUT_S,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        transport=_transport(),
    )


def hole_seite(url: str, *, min_delay_s: float | None = None) -> FetchErgebnis:
    """Eine Seite höflich holen: erst robots.txt, dann warten, dann GET.

    ``min_delay_s`` kommt aus dem Adapter und wirkt nur erhöhend; der Boden aus
    :data:`DEFAULT_MIN_DELAY_S` bleibt in jedem Fall stehen.
    """
    ziel = _zerlege(url)
    abstand = max(DEFAULT_MIN_DELAY_S, min_delay_s or 0.0)
    with _client() as client:
        _pruefe_robots(client, ziel, abstand)
        _warte(ziel.domain, abstand)
        return _lade_seite(client, ziel)


def _zerlege(url: str) -> _Ziel:
    """URL prüfen und in Herkunft, Domain und Pfad zerlegen."""
    teile = urlsplit(url)
    if teile.scheme not in {"http", "https"} or not teile.hostname or teile.username:
        raise FetchAbgelehnt(
            f"Adapter-URL muss eine HTTP(S)-URL ohne Zugangsdaten sein: {url}"
        )
    herkunft = urlunsplit((teile.scheme, teile.netloc, "", "", ""))
    return _Ziel(
        url=url,
        herkunft=herkunft,
        domain=_domain(teile.hostname),
        pfad=teile.path or "/",
    )


def _domain(hostname: str) -> str:
    """Schlüssel für den Mindestabstand.

    ``www.shop.ch`` und ``shop.ch`` sind derselbe Server; sie getrennt zu zählen
    würde den Abstand halbieren.
    """
    return hostname.lower().rstrip(".").removeprefix("www.")


def _warte(domain: str, abstand: float) -> None:
    """Den nächsten Slot dieser Domain belegen und bis dahin schlafen.

    Der Slot wird unter der Sperre reserviert, geschlafen wird ohne sie: zwei
    Threads auf derselben Domain reihen sich damit auf, zwei Threads auf
    verschiedenen Domains behindern sich nicht.
    """
    with _sperre:
        jetzt = _jetzt()
        start = max(_naechster_slot.get(domain, jetzt), jetzt)
        _naechster_slot[domain] = start + abstand
    wartezeit = start - jetzt
    if wartezeit > 0:
        _schlafe(wartezeit)


def _pruefe_robots(client: httpx.Client, ziel: _Ziel, abstand: float) -> None:
    """robots.txt der Herkunft auswerten - notfalls frisch geholt.

    Der Abruf selbst ist ein Request und zählt deshalb gegen den Mindestabstand.
    """
    stand = _robots_cache.get(ziel.herkunft)
    if stand is None or _jetzt() >= stand.ablauf:
        _warte(ziel.domain, abstand)
        stand = _frage_robots(client, ziel)
        _robots_cache[ziel.herkunft] = stand
    if stand.fehler is not None:
        raise FetchTemporaerFehler(stand.fehler)
    if stand.regeln is not None and not stand.regeln.can_fetch(ROBOTS_AGENT, ziel.url):
        raise RobotsVerboten(
            f"robots.txt von {ziel.domain} verbietet {ziel.pfad} - "
            "die Seite wird nicht abgerufen"
        )


def _frage_robots(client: httpx.Client, ziel: _Ziel) -> _RobotsStand:
    """robots.txt holen und zu einem Cache-Eintrag machen; wirft nie.

    Zwei Ausgänge, und der Unterschied ist Absicht: eine Domain **ohne**
    robots.txt (HTTP 4xx) erlaubt alles, eine Domain, deren robots.txt wir
    **nicht lesen konnten**, erlaubt gar nichts. Im zweiten Fall wird nicht
    geraten.
    """
    try:
        with client.stream("GET", ziel.robots_url) as antwort:
            if 400 <= antwort.status_code < 500 and antwort.status_code != 429:
                return _RobotsStand(_jetzt() + ROBOTS_CACHE_S_ERFOLG, regeln=None)
            if antwort.status_code >= 400:
                return _RobotsStand(
                    _jetzt() + ROBOTS_CACHE_S_FEHLSCHLAG,
                    fehler=(
                        f"robots.txt von {ziel.domain} nicht erreichbar "
                        f"(HTTP {antwort.status_code}) - ohne sie wird nicht abgerufen"
                    ),
                )
            text = _lies_text(antwort, ziel.robots_url)
    except FetchFehler as error:
        return _RobotsStand(_jetzt() + ROBOTS_CACHE_S_FEHLSCHLAG, fehler=str(error))
    except httpx.HTTPError as error:
        return _RobotsStand(
            _jetzt() + ROBOTS_CACHE_S_FEHLSCHLAG,
            fehler=(
                f"robots.txt von {ziel.domain} nicht erreichbar "
                f"({type(error).__name__}) - ohne sie wird nicht abgerufen"
            ),
        )
    regeln = RobotFileParser()
    regeln.parse(text.splitlines())
    return _RobotsStand(_jetzt() + ROBOTS_CACHE_S_ERFOLG, regeln=regeln)


def _lade_seite(client: httpx.Client, ziel: _Ziel) -> FetchErgebnis:
    """Die Produktseite holen und den Status in die Fehlertaxonomie übersetzen."""
    try:
        with client.stream("GET", ziel.url) as antwort:
            _pruefe_domain(antwort, ziel)
            _pruefe_status(antwort.status_code, ziel)
            text = _lies_text(antwort, ziel.url)
            return FetchErgebnis(
                final_url=str(antwort.url), status=antwort.status_code, text=text
            )
    except httpx.TooManyRedirects as error:
        raise FetchAbgelehnt(
            f"Mehr als {MAX_REDIRECTS} Weiterleitungen: {ziel.url}"
        ) from error
    except httpx.TimeoutException as error:
        raise FetchTemporaerFehler(
            f"{ziel.domain} antwortet nicht innerhalb von {TIMEOUT_S:.0f}s: {ziel.url}"
        ) from error
    except httpx.HTTPError as error:
        raise FetchTemporaerFehler(
            f"{ziel.domain} nicht erreichbar ({type(error).__name__}): {ziel.url}"
        ) from error


def _pruefe_domain(antwort: httpx.Response, ziel: _Ziel) -> None:
    """Eine Weiterleitungskette darf die geprüfte Domain nicht verlassen.

    Für eine fremde Domain haben wir weder robots.txt gelesen noch ihren
    Mindestabstand eingehalten - dort wird nichts weitergelesen. httpx folgt der
    Kette selbst, der Abbruch kommt deshalb nach dem letzten Sprung; gelesen
    wird die Antwort trotzdem nicht.
    """
    erreicht = antwort.url.host
    if erreicht and _domain(erreicht) != ziel.domain:
        raise FetchAbgelehnt(
            f"Weiterleitung verlässt {ziel.domain}: {antwort.url}"
        )


def _pruefe_status(status: int, ziel: _Ziel) -> None:
    if status < 400:
        return
    if status == 403:
        # Ehrlich benannt und nicht umgangen: wer automatisierte Zugriffe
        # abweist, hat das so entschieden.
        raise FetchAbgelehnt(
            f"Shop blockt automatisierte Zugriffe (HTTP 403): {ziel.url}"
        )
    if status == 429 or status >= 500:
        raise FetchTemporaerFehler(
            f"{ziel.domain} antwortet mit HTTP {status}: {ziel.url}"
        )
    raise FetchAbgelehnt(f"Shop antwortet mit HTTP {status}: {ziel.url}")


def _lies_text(antwort: httpx.Response, url: str) -> str:
    """Antwort gestreamt lesen und beim Grössenlimit abbrechen."""
    stuecke: list[bytes] = []
    groesse = 0
    for stueck in antwort.iter_bytes():
        groesse += len(stueck)
        if groesse > MAX_BYTES:
            raise FetchAbgelehnt(f"Seite grösser als {MAX_BYTES // 1_000_000} MB: {url}")
        stuecke.append(stueck)
    return _dekodiere(b"".join(stuecke), antwort.charset_encoding)


def _dekodiere(rohdaten: bytes, kopf_kodierung: str | None) -> str:
    """Bytes zu Text: Header, sonst Meta-Tag, sonst UTF-8.

    Die Reihenfolge ist nicht Kosmetik. Ein Shop, der ``ISO-8859-1`` nur im HTML
    nennt, käme als UTF-8 gelesen mit zerschossenen Umlauten in die Datenbank -
    und genau dieser Text soll dort wörtlich stehen.
    """
    for kandidat in (kopf_kodierung, _meta_kodierung(rohdaten), "utf-8"):
        if not kandidat:
            continue
        try:
            return rohdaten.decode(kandidat, errors="replace")
        except LookupError:
            continue
    return rohdaten.decode("utf-8", errors="replace")


def _meta_kodierung(rohdaten: bytes) -> str | None:
    treffer = _META_CHARSET.search(rohdaten[:2048])
    return treffer.group(1).decode("ascii", errors="ignore") if treffer else None
