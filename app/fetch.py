"""Einziger Netzzugang der Shop-Adapter: robots.txt, Mindestabstand und User-Agent sitzen hier."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .version import __version__

# Ehrlicher Absender: Name, Version und die Adresse, unter der nachlesbar ist, was
# dieses Werkzeug tut.
USER_AGENT = (
    f"beschaffung/{__version__} "
    "(+https://github.com/Flashbibi/i-dont-wanna-research-stuff)"
)

# Name, unter dem wir uns in robots.txt wiedererkennen.
ROBOTS_AGENT = "beschaffung"

# Mindestabstand zwischen zwei Requests an dieselbe Domain, prozessweit; ein Adapter
# darf ihn erhöhen, niemals senken.
DEFAULT_MIN_DELAY_S = 5.0

# Kurz genug, dass ein hängender Shop keine Anfrage blockiert.
TIMEOUT_S = 10.0

# Selbstschutz gegen Shops, die nicht aufhören zu senden; BerryBase-Seiten liegen
# über 2 MB.
MAX_BYTES = 5_000_000

# Eine Handvoll Weiterleitungen ist normal, eine Kette davon nicht - und jeder Sprung
# ist ein eigener Request durch dieselben Prüfungen wie der erste.
MAX_REDIRECTS = 5

REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

# Obergrenze für ein ``Crawl-delay`` aus robots.txt; wer mehr verlangt, bekommt einen
# Abbruch statt einer still gekürzten Wartezeit.
MAX_CRAWL_DELAY_S = 60.0

# Deutschsprachige Seite bevorzugen; die Texte landen wörtlich in der Datenbank.
ACCEPT_LANGUAGE = "de-CH,de;q=0.9,en;q=0.8"

# robots.txt wird gemerkt statt vor jeder Seite neu geholt - Erfolg lange,
# Fehlschlag kurz.
ROBOTS_CACHE_S_ERFOLG = 24 * 60 * 60
ROBOTS_CACHE_S_FEHLSCHLAG = 60 * 60

# Zeichenkodierung aus dem HTML selbst, falls der Header keine nennt.
_META_CHARSET = re.compile(
    rb'<meta[^>]+charset\s*=\s*["\']?\s*([a-zA-Z0-9_:.+-]+)', re.IGNORECASE
)


class FetchFehler(ValueError):
    """Klartext-Fehler; die Meldung ist für Oberfläche und MCP bestimmt."""


class RobotsVerboten(FetchFehler):
    """robots.txt der Domain verbietet diesen Pfad - wir fragen gar nicht erst an."""


class FetchTemporaerFehler(FetchFehler):
    """Zeitüberschreitung, Verbindungsfehler, HTTP 5xx oder 429 - wiederholbar, aber
    die Wiederholung entscheidet der Aufrufer und nicht dieses Modul."""


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
    """Was wir über die robots.txt einer Herkunft wissen, wobei ein gesetztes
    ``fehler`` jeden Abruf abbricht, statt zu raten, bis der Eintrag abläuft."""

    ablauf: float
    regeln: RobotFileParser | None = None
    fehler: str | None = None


# Ohne Sperre wäre der Mindestabstand wirkungslos, weil FastAPI sync-Endpoints im
# Threadpool ausführt und zwei Threads beide "gerade frei" sähen.
_sperre = threading.Lock()

# Domain -> monotoner Zeitpunkt des letzten Requests und kein fertig ausgerechneter
# nächster Slot, denn der wirksame Abstand kann zwischen zwei Requests steigen.
_letzter_request: dict[str, float] = {}

# Herkunft -> Stand ihrer robots.txt.
_robots_cache: dict[str, _RobotsStand] = {}

# Herkunft -> eigene Sperre, ohne die vier gleichzeitig gestartete Abrufe derselben
# Domain viermal dieselbe robots.txt holen.
_robots_sperren: dict[str, threading.Lock] = {}


def _jetzt() -> float:
    """Monotone Uhr - in Tests der Ansatzpunkt."""
    return time.monotonic()


def _schlafe(sekunden: float) -> None:
    """Warten - in Tests der Ansatzpunkt."""
    time.sleep(sekunden)


def _transport() -> Any | None:
    """Einziger Ansatzpunkt für Tests, damit Header, Timeout, Weiterleitungen und
    Grössenlimit auch dort der echte Weg bleiben; ``None`` heisst echtes Netz."""
    return None


def _client() -> httpx.Client:
    """Der Client für robots.txt und Seite; ``follow_redirects=False`` ist die tragende
    Zeile, weil httpx sonst jede Kette an robots.txt und Mindestabstand vorbei liefe."""
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": ACCEPT_LANGUAGE,
            # Unkomprimiert, damit das Grössenlimit die Leitung misst und nicht
            # die entpackte Seite.
            "Accept-Encoding": "identity",
        },
        timeout=TIMEOUT_S,
        follow_redirects=False,
        transport=_transport(),
    )


def hole_seite(url: str, *, min_delay_s: float | None = None) -> FetchErgebnis:
    """Eine Seite höflich holen: erst robots.txt, dann warten, dann GET - und für jede
    Adresse einer Weiterleitungskette dieselbe Runde noch einmal."""
    ziel = _zerlege(url)
    boden = max(DEFAULT_MIN_DELAY_S, min_delay_s or 0.0)
    with _client() as client:
        for _ in range(MAX_REDIRECTS + 1):
            wunsch = _pruefe_robots(client, ziel, boden)
            _warte(ziel.domain, max(boden, wunsch or 0.0))
            ergebnis = _lade_sprung(client, ziel)
            if isinstance(ergebnis, FetchErgebnis):
                return ergebnis
            ziel = ergebnis
    raise FetchAbgelehnt(f"Mehr als {MAX_REDIRECTS} Weiterleitungen: {url}")


def _zerlege(url: str) -> _Ziel:
    """URL prüfen und in Herkunft, Domain und Pfad zerlegen, damit auch das, was httpx
    erst beim Absenden ablehnte, sofort als Klartext auffällt statt als Traceback."""
    try:
        teile = urlsplit(url)
        rechner, nutzer, passwort = teile.hostname, teile.username, teile.password
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as error:
        raise FetchAbgelehnt(f"Unbrauchbare Adresse ({error}): {url!r}") from error
    # Zugangsdaten in der URL werden als Basic-Auth mitgeschickt und landeten
    # danach im Angebot - nicht bloss ein leerer Benutzername zählt.
    if (
        teile.scheme not in {"http", "https"}
        or not rechner
        or nutzer is not None
        or passwort is not None
    ):
        raise FetchAbgelehnt(
            f"Adapter-URL muss eine HTTP(S)-URL ohne Zugangsdaten sein: {url}"
        )
    herkunft = urlunsplit((teile.scheme, teile.netloc, "", "", ""))
    return _Ziel(
        url=url,
        herkunft=herkunft,
        domain=_domain(rechner),
        pfad=teile.path or "/",
    )


def _domain(hostname: str) -> str:
    """Schlüssel für den Mindestabstand, denn ``www.shop.ch`` und ``shop.ch`` sind
    derselbe Server und getrennt gezählt halbierten sie den Abstand."""
    return hostname.lower().rstrip(".").removeprefix("www.")


def _warte(domain: str, abstand: float) -> None:
    """Den Zeitpunkt des nächsten Requests unter der Sperre belegen und ohne sie
    schlafen, damit Threads auf verschiedenen Domains sich nicht behindern."""
    with _sperre:
        jetzt = _jetzt()
        letzter = _letzter_request.get(domain)
        start = jetzt if letzter is None else max(jetzt, letzter + abstand)
        _letzter_request[domain] = start
    wartezeit = start - jetzt
    if wartezeit > 0:
        _schlafe(wartezeit)


def _robots_sperre(herkunft: str) -> threading.Lock:
    """Eigene Sperre je Herkunft, damit ihre robots.txt einmal geholt wird."""
    with _sperre:
        return _robots_sperren.setdefault(herkunft, threading.Lock())


def _pruefe_robots(client: httpx.Client, ziel: _Ziel, abstand: float) -> float | None:
    """robots.txt der Herkunft auswerten und notfalls frisch holen, was als eigener
    Request selbst gegen den Mindestabstand zählt."""
    with _robots_sperre(ziel.herkunft):
        stand = _robots_cache.get(ziel.herkunft)
        if stand is None or _jetzt() >= stand.ablauf:
            _warte(ziel.domain, abstand)
            stand = _frage_robots(client, ziel, abstand)
            _robots_cache[ziel.herkunft] = stand
    if stand.fehler is not None:
        raise FetchTemporaerFehler(stand.fehler)
    if stand.regeln is None:
        return None
    if not stand.regeln.can_fetch(ROBOTS_AGENT, ziel.url):
        raise RobotsVerboten(
            f"robots.txt von {ziel.domain} verbietet {ziel.pfad} - "
            "die Seite wird nicht abgerufen"
        )
    return _crawl_delay(stand.regeln, ziel)


def _crawl_delay(regeln: RobotFileParser, ziel: _Ziel) -> float | None:
    """Den vom Shop gewünschten Abstand lesen, der nur anhebt und über
    ``MAX_CRAWL_DELAY_S`` nicht gekürzt, sondern abgebrochen wird."""
    wert = regeln.crawl_delay(ROBOTS_AGENT)
    if wert is None:
        return None
    try:
        wunsch = float(wert)
    except (TypeError, ValueError):  # pragma: no cover - robotparser liefert Zahlen
        return None
    if wunsch <= 0:
        return None
    if wunsch > MAX_CRAWL_DELAY_S:
        raise FetchAbgelehnt(
            f"robots.txt von {ziel.domain} verlangt {wunsch:.0f}s Abstand; "
            f"länger als {MAX_CRAWL_DELAY_S:.0f}s wartet dieser Abruf nicht"
        )
    return wunsch


def _frage_robots(client: httpx.Client, ziel: _Ziel, abstand: float) -> _RobotsStand:
    """robots.txt holen und zu einem Cache-Eintrag machen, ohne je zu werfen: eine
    fehlende robots.txt erlaubt alles, eine nicht lesbare gar nichts."""
    quelle = _zerlege(ziel.robots_url)
    try:
        for sprung in range(MAX_REDIRECTS + 1):
            if sprung:
                _warte(ziel.domain, abstand)
            with client.stream("GET", quelle.url) as antwort:
                weiter = _weiterleitung(antwort, quelle)
                if weiter is not None:
                    quelle = weiter
                    continue
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
                text = _lies_text(antwort, quelle.url)
                break
        else:
            raise FetchAbgelehnt(f"mehr als {MAX_REDIRECTS} Weiterleitungen")
    except FetchFehler as error:
        return _RobotsStand(
            _jetzt() + ROBOTS_CACHE_S_FEHLSCHLAG,
            fehler=(
                f"robots.txt von {ziel.domain} nicht lesbar ({error}) - "
                "ohne sie wird nicht abgerufen"
            ),
        )
    except httpx.HTTPError as error:
        return _RobotsStand(
            _jetzt() + ROBOTS_CACHE_S_FEHLSCHLAG,
            fehler=(
                f"robots.txt von {ziel.domain} nicht erreichbar "
                f"({type(error).__name__}) - ohne sie wird nicht abgerufen"
            ),
        )
    regeln = RobotFileParser()
    # Ein führendes BOM würde die erste Zeile unkenntlich machen, worauf robotparser
    # stillschweigend jede Regel der Datei verwürfe und alles erlaubte.
    regeln.parse(text.lstrip("\ufeff").splitlines())
    return _RobotsStand(_jetzt() + ROBOTS_CACHE_S_ERFOLG, regeln=regeln)


def _lade_sprung(client: httpx.Client, ziel: _Ziel) -> FetchErgebnis | _Ziel:
    """Einen Sprung holen: die fertige Seite, oder die nächste Adresse, deren Körper
    ungelesen bleibt und die erst wieder durch robots.txt und Mindestabstand muss."""
    try:
        with client.stream("GET", ziel.url) as antwort:
            weiter = _weiterleitung(antwort, ziel)
            if weiter is not None:
                return weiter
            _pruefe_status(antwort.status_code, ziel)
            text = _lies_text(antwort, ziel.url)
            return FetchErgebnis(
                final_url=str(antwort.url), status=antwort.status_code, text=text
            )
    except httpx.TimeoutException as error:
        raise FetchTemporaerFehler(
            f"{ziel.domain} antwortet nicht innerhalb von {TIMEOUT_S:.0f}s: {ziel.url}"
        ) from error
    except httpx.HTTPError as error:
        raise FetchTemporaerFehler(
            f"{ziel.domain} nicht erreichbar ({type(error).__name__}): {ziel.url}"
        ) from error


def _weiterleitung(antwort: httpx.Response, ziel: _Ziel) -> _Ziel | None:
    """Das Ziel einer Weiterleitung, das die Domain nicht verlassen darf - für eine
    fremde Domain ist weder robots.txt gelesen noch ihr Mindestabstand gewahrt."""
    if antwort.status_code not in REDIRECT_STATUS:
        return None
    ort = antwort.headers.get("location", "").strip()
    if not ort:
        raise FetchAbgelehnt(
            f"Weiterleitung ohne Ziel (HTTP {antwort.status_code}): {ziel.url}"
        )
    neu = _zerlege(urljoin(ziel.url, ort))
    if neu.domain != ziel.domain:
        raise FetchAbgelehnt(f"Weiterleitung verlässt {ziel.domain}: {neu.url}")
    return neu


def _pruefe_status(status: int, ziel: _Ziel) -> None:
    if status < 400:
        return
    if status == 403:
        # HTTP 403 wird gemeldet und nicht umgangen, weil der Shop automatisierte
        # Zugriffe abweist.
        raise FetchAbgelehnt(
            f"Shop blockt automatisierte Zugriffe (HTTP 403): {ziel.url}"
        )
    if status == 429 or status >= 500:
        raise FetchTemporaerFehler(
            f"{ziel.domain} antwortet mit HTTP {status}: {ziel.url}"
        )
    raise FetchAbgelehnt(f"Shop antwortet mit HTTP {status}: {ziel.url}")


def _lies_text(antwort: httpx.Response, url: str) -> str:
    """Antwort gestreamt lesen und beim Grössenlimit abbrechen, das nur zählen kann,
    weil httpx in ``iter_bytes`` auspackt und Komprimiertes vorher scheitert."""
    packung = antwort.headers.get("content-encoding", "").strip().lower()
    if packung and packung != "identity":
        raise FetchAbgelehnt(
            f"Shop antwortet komprimiert ({packung}), obwohl unkomprimiert "
            f"angefordert: {url}"
        )
    stuecke: list[bytes] = []
    groesse = 0
    for stueck in antwort.iter_bytes():
        groesse += len(stueck)
        if groesse > MAX_BYTES:
            raise FetchAbgelehnt(
                f"Seite grösser als {MAX_BYTES // 1_000_000} MB: {url}"
            )
        stuecke.append(stueck)
    return _dekodiere(b"".join(stuecke), antwort.charset_encoding)


def _dekodiere(rohdaten: bytes, kopf_kodierung: str | None) -> str:
    """Bytes zu Text in der Reihenfolge Header, Meta-Tag, UTF-8, weil ein nur im HTML
    genanntes ``ISO-8859-1`` sonst zerschossene Umlaute in die Datenbank trüge."""
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
