"""Die eine Tür zum Internet für Shop-Adapter.

Jeder Adapter-Abruf läuft durch :func:`hole_seite`. Das ist kein Stilwunsch,
sondern der Grund, warum Höflichkeit hier strukturell ist statt optional:
robots.txt, Mindestabstand pro Domain und ein ehrlicher User-Agent sitzen in
dieser einen Funktion. Es gibt keinen Schalter, der das abstellt, und keinen
zweiten Weg daran vorbei.

Eine Weiterleitung ist hier kein Sonderfall, sondern derselbe Ablauf noch
einmal: die Kette wird von Hand gegangen, und jede Adresse darin muss erst
wieder durch robots.txt und Mindestabstand, bevor sie angefragt wird. Verlässt
die Kette die Domain, endet der Abruf - für eine fremde Domain haben wir nichts
gelesen und fragen dort auch nichts an.

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
from urllib.parse import urljoin, urlsplit, urlunsplit
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
#: Die Grenze ist Selbstschutz gegen einen Shop, der nicht aufhört zu senden -
#: und war mit 2 MB zu eng gesteckt: BerryBase liefert seine Produktseiten
#: darüber und wurde von unserem eigenen Schutz ausgesperrt, obwohl der Shop
#: nichts dagegen hatte.
MAX_BYTES = 5_000_000

#: Eine Handvoll Weiterleitungen ist normal, eine Kette davon nicht. Gezählt
#: werden sie hier selbst, denn jeder Sprung ist ein eigener Request und muss
#: durch dieselben Prüfungen wie der erste.
MAX_REDIRECTS = 5

#: Antworten, die auf eine andere Adresse zeigen statt eine Seite zu liefern.
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

#: Obergrenze für ein ``Crawl-delay`` aus robots.txt. Wer mehr verlangt, bekommt
#: keine still gekürzte Wartezeit, sondern einen Abbruch - eine Viertelstunde in
#: einem interaktiven Aufruf zu schlafen wäre kein Entgegenkommen, sondern ein
#: Hänger.
MAX_CRAWL_DELAY_S = 60.0

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

#: Domain -> monotoner Zeitpunkt des letzten Requests. Gemerkt wird der
#: Zeitpunkt selbst und kein fertig ausgerechneter nächster Slot: der wirksame
#: Abstand kann zwischen zwei Requests steigen, und dann muss die schon
#: vergangene Zeit nach der neuen Regel gemessen werden.
_letzter_request: dict[str, float] = {}

#: Herkunft -> Stand ihrer robots.txt.
_robots_cache: dict[str, _RobotsStand] = {}

#: Herkunft -> eigene Sperre. Ohne sie holen vier gleichzeitig gestartete Abrufe
#: derselben Domain viermal dieselbe robots.txt statt einmal.
_robots_sperren: dict[str, threading.Lock] = {}


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
    """Der Client, mit dem dieses Modul spricht. Für robots.txt und Seite derselbe.

    ``follow_redirects=False`` ist die tragende Zeile. httpx würde eine Kette
    still durchlaufen, und jeder Sprung darin wäre ein Request an eine Adresse,
    für die weder robots.txt ausgewertet noch der Mindestabstand gewahrt wurde -
    ein 301 des Shops würde damit beides aushebeln.
    """
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": ACCEPT_LANGUAGE,
            # Unkomprimiert, damit das Grössenlimit die Leitung misst und nicht
            # die entpackte Seite. Ein gzip-Strom kann beliebig lange senden,
            # ohne dass entpackt etwas ankommt - dann liefe das Limit ins Leere.
            "Accept-Encoding": "identity",
        },
        timeout=TIMEOUT_S,
        follow_redirects=False,
        transport=_transport(),
    )


def hole_seite(url: str, *, min_delay_s: float | None = None) -> FetchErgebnis:
    """Eine Seite höflich holen: erst robots.txt, dann warten, dann GET.

    ``min_delay_s`` kommt aus dem Adapter und wirkt nur erhöhend; der Boden aus
    :data:`DEFAULT_MIN_DELAY_S` bleibt in jedem Fall stehen, und ein
    ``Crawl-delay`` des Shops hebt ihn weiter an.

    Jede Adresse einer Weiterleitungskette durchläuft dieselbe Runde: erst
    robots.txt, dann warten, dann anfragen.
    """
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
    """URL prüfen und in Herkunft, Domain und Pfad zerlegen.

    Geprüft wird hier auch, was httpx erst beim Absenden ablehnen würde - ein
    Zeilenumbruch aus einer Zwischenablage, ein unbrauchbarer Port. Sonst käme
    der Abbruch erst nach dem robots-Abruf und als Traceback statt als Klartext.
    """
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
    """Schlüssel für den Mindestabstand.

    ``www.shop.ch`` und ``shop.ch`` sind derselbe Server; sie getrennt zu zählen
    würde den Abstand halbieren.
    """
    return hostname.lower().rstrip(".").removeprefix("www.")


def _warte(domain: str, abstand: float) -> None:
    """Den Zeitpunkt des nächsten Requests belegen und bis dahin schlafen.

    Belegt wird unter der Sperre, geschlafen wird ohne sie: zwei Threads auf
    derselben Domain reihen sich damit auf, zwei Threads auf verschiedenen
    Domains behindern sich nicht.
    """
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
    """robots.txt der Herkunft auswerten - notfalls frisch geholt.

    Der Abruf selbst ist ein Request und zählt deshalb gegen den Mindestabstand.
    Die Sperre je Herkunft hält gleichzeitig gestartete Abrufe derselben Domain
    zusammen: sie teilen sich eine robots.txt, statt sie zu vervielfachen.

    Zurück kommt ein ``Crawl-delay``, falls der Shop eines nennt - robots.txt zu
    lesen und die Hälfte davon zu übergehen wäre keine Höflichkeit.
    """
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
    """Den vom Shop gewünschten Abstand lesen; er hebt an und senkt nie.

    Über :data:`MAX_CRAWL_DELAY_S` wird nicht gekürzt, sondern abgebrochen. Eine
    still halbierte Wartezeit wäre genau die Art von Ungefähr, die dieses Modul
    sonst überall vermeidet.
    """
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
    """robots.txt holen und zu einem Cache-Eintrag machen; wirft nie.

    Zwei Ausgänge, und der Unterschied ist Absicht: eine Domain **ohne**
    robots.txt (HTTP 4xx) erlaubt alles, eine Domain, deren robots.txt wir
    **nicht lesen konnten**, erlaubt gar nichts. Im zweiten Fall wird nicht
    geraten.

    Weitergeleitet wird auch hier von Hand und nur innerhalb der Domain. Eine
    robots.txt, die auf einen fremden Host zeigt, wird nicht gelesen: sonst
    entschiede ein Dritter, was bei diesem Shop erlaubt ist.
    """
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
    # Ein führendes BOM würde die erste Zeile unkenntlich machen; robotparser
    # verwürfe dann stillschweigend jede Regel der Datei und erlaubte alles.
    regeln.parse(text.lstrip("\ufeff").splitlines())
    return _RobotsStand(_jetzt() + ROBOTS_CACHE_S_ERFOLG, regeln=regeln)


def _lade_sprung(client: httpx.Client, ziel: _Ziel) -> FetchErgebnis | _Ziel:
    """Einen Sprung holen: die fertige Seite, oder die nächste Adresse.

    Der Körper einer Weiterleitungsantwort wird nicht gelesen - er interessiert
    nicht, und die nächste Adresse muss ohnehin erst wieder durch robots.txt und
    Mindestabstand.
    """
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
    """Die Adresse hinter einer Weiterleitung - oder None, wenn es keine ist.

    Die Kette darf die Domain nicht verlassen. Für eine fremde Domain haben wir
    weder robots.txt gelesen noch ihren Mindestabstand gewahrt; sie auch nur
    einmal anzufragen wäre schon zu viel, und deshalb endet der Abruf hier statt
    erst nach dem Sprung.
    """
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
    """Antwort gestreamt lesen und beim Grössenlimit abbrechen.

    Der Abbruch bei einer komprimierten Antwort steht vor der Schleife, und er
    ist der Grund, warum die Schleife überhaupt zählen kann: httpx packt in
    ``iter_bytes`` erst aus, ein gzip-Strom kann also beliebig lange senden,
    ohne dass entpackt ein einziges Byte ankommt - das Limit liefe ins Leere.
    Ohne Packung sind die gelieferten Bytes genau die der Leitung.
    """
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
