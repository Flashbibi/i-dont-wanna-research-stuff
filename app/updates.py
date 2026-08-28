"""Update-Check gegen die Releases-API von GitHub.

Nur Standardbibliothek, und still im Fehlerfall. Der Check hängt im
Seitenaufbau, also darf er ihn nie aufhalten: kein Netz, Rate-Limit, kaputtes
JSON, fremdes Tag-Format - alles endet gleich, nämlich als "kein Banner".

Gefragt wird höchstens einmal am Tag; der Prozess merkt sich die Antwort.
Ein Fehlschlag wird kürzer gemerkt, damit sich eine Instanz nach einer
Netzstörung nicht einen ganzen Tag lang blind stellt.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import NamedTuple

from .version import __version__

RELEASE_API = (
    "https://api.github.com/repos/Flashbibi/i-dont-wanna-research-stuff/releases/latest"
)
RELEASE_PAGE = "https://github.com/Flashbibi/i-dont-wanna-research-stuff/releases/tag/"
TIMEOUT_SECONDS = 3
CACHE_SECONDS_ERFOLG = 24 * 60 * 60
CACHE_SECONDS_FEHLSCHLAG = 60 * 60

TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class Release(NamedTuple):
    tag: str
    version: tuple[int, int, int]
    url: str


# (Ablauf, Ergebnis) - prozessweit, absichtlich ohne Sperre: zwei parallele
# Abfragen kosten schlimmstenfalls einen zweiten GET.
_cache: tuple[float, Release | None] | None = None


def _aktiviert() -> bool:
    return os.environ.get("BESCHAFFUNG_UPDATE_CHECK", "on").strip().lower() != "off"


def _parse_tag(tag: object) -> tuple[int, int, int] | None:
    """``vX.Y.Z`` als Tupel. Alles andere ist kein Tag, das wir vergleichen."""
    if not isinstance(tag, str):
        return None
    treffer = TAG_PATTERN.match(tag.strip())
    if treffer is None:
        return None
    major, minor, patch = treffer.groups()
    return int(major), int(minor), int(patch)


def _fetch() -> object:
    """Der einzige Netzzugriff des Moduls - in Tests der Ansatzpunkt."""
    request = urllib.request.Request(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"beschaffung/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _read_release() -> Release | None:
    try:
        payload = _fetch()
        version = _parse_tag(payload.get("tag_name") if isinstance(payload, dict) else None)
    except Exception:  # noqa: BLE001 - jeder Fehler heisst schlicht "kein Banner"
        return None
    if version is None:
        return None
    # Tag und Link entstehen aus den geprüften Zahlen, nicht aus der Antwort.
    tag = "v" + ".".join(str(teil) for teil in version)
    return Release(tag, version, RELEASE_PAGE + tag)


def latest_release() -> Release | None:
    """Neuestes Release laut GitHub, aus dem Cache oder frisch geholt."""
    global _cache
    if not _aktiviert():
        return None
    jetzt = time.monotonic()
    if _cache is not None and jetzt < _cache[0]:
        return _cache[1]
    release = _read_release()
    dauer = CACHE_SECONDS_ERFOLG if release is not None else CACHE_SECONDS_FEHLSCHLAG
    _cache = (jetzt + dauer, release)
    return release


def update_available() -> Release | None:
    """Release-Info nur, wenn GitHub strikt neuer ist als der laufende Stand."""
    laufend = _parse_tag(f"v{__version__}")
    if laufend is None:
        return None
    release = latest_release()
    if release is None or release.version <= laufend:
        return None
    return release
