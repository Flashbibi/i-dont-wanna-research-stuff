# Beschaffung

[![CI](https://github.com/Flashbibi/i-dont-wanna-research-stuff/actions/workflows/ci.yml/badge.svg)](https://github.com/Flashbibi/i-dont-wanna-research-stuff/actions/workflows/ci.yml)

LAN-Beschaffungstool für Linus.

## Quick start (Docker)

Für eine eigene Instanz genügt Docker, ein Clone ist nicht nötig:

```sh
curl -O https://raw.githubusercontent.com/Flashbibi/i-dont-wanna-research-stuff/main/docker-compose.yml
export POSTGRES_PASSWORD="ein-eigenes-passwort"
docker compose up -d
```

Danach läuft die Anwendung auf `http://localhost:8000`; die Migrationen hat der
Start bereits eingespielt.

Das Image `ghcr.io/flashbibi/i-dont-wanna-research-stuff` existiert erst ab dem
ersten Release `v0.1.0`. Wer vorher oder auf einem eigenen Stand starten will,
ersetzt im Clone die `image:`-Zeile durch `build: .`.

Beim Seitenaufbau prüft die Anwendung, ob auf GitHub ein neueres Release liegt,
und blendet dann ein Banner ein. Der Check ist ein unauthentifizierter GET auf
die GitHub-API ohne jegliche Nutzdaten, höchstens einmal täglich;
`BESCHAFFUNG_UPDATE_CHECK=off` schaltet ihn vollständig ab, dann findet gar kein
Netzwerkzugriff statt.

## Deployment

Produktiv läuft das Tool auf CT 104 (`192.168.1.60`), Web-UI unter
`http://192.168.1.60:8000`.

Es gibt zwei Remotes, und die Reihenfolge ist nicht beliebig:

| Remote   | Ziel                                          | Wirkung                                            |
| -------- | --------------------------------------------- | -------------------------------------------------- |
| `deploy` | `deploy@192.168.1.60:/srv/git/beschaffung.git` | **Deployt.** post-receive checkt nach `/opt/beschaffung` aus und startet den Service neu. |
| `github` | GitHub                                         | Reiner Spiegel, deployt nichts.                     |

```sh
git push deploy main     # zuerst - das ist das Deployment
git push github main     # danach - nur Spiegel
```

Nach jedem Deploy `/health` prüfen (liefert Status und Schema-Version):

```sh
curl -s http://192.168.1.60:8000/health
```

Debugzugang: `ssh deploy@192.168.1.60`. Logs unter
`/var/log/beschaffung/service.log`. Erlaubtes sudo ist exakt
`systemctl restart beschaffung` und `systemctl status beschaffung`.

## Lokale Entwicklung

Voraussetzungen: Python 3.12+ und Docker.

### 1. Virtualenv

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Windows
# .venv/bin/python -m pip install -r requirements-dev.txt     # Linux/macOS
```

Ausser `pytest` kommt nichts dazu. Der `TestClient` von Starlette 1.6 greift
bevorzugt zu `httpx2`, fällt aber auf das ohnehin vorhandene `httpx` zurück -
und genau darauf läuft die CI seit jeher, die nur `requirements.txt` und
`pytest` installiert.

### 2. Postgres

Die Anwendung braucht Postgres; die Testsuite nicht (siehe unten).

```sh
docker run -d \
  --name beschaffung-db \
  --restart unless-stopped \
  -e POSTGRES_USER=beschaffung \
  -e POSTGRES_PASSWORD=beschaffung \
  -e POSTGRES_DB=beschaffung \
  -v beschaffung-pgdata:/var/lib/postgresql/data \
  -p 5433:5432 \
  postgres:16
```

Hostport ist bewusst **5433**, nicht 5432 — auf dem Entwicklungsrechner belegt
ein anderes Projekt bereits 5432. Die Daten liegen im Volume
`beschaffung-pgdata` und überleben ein `docker rm` des Containers.

### 3. Anwendung starten

Die einzige nötige Env-Variable ist `DATABASE_URL`. `python -m app` spielt beim
Start fehlende Migrationen ein (idempotent) und serviert dann auf Port 8000.

```sh
export DATABASE_URL="postgresql://beschaffung:beschaffung@127.0.0.1:5433/beschaffung"
.venv/Scripts/python -m app
```

Beim Entwickeln besser mit Auto-Reload, sonst liefert der Server alten Code aus:

```sh
.venv/Scripts/python -m uvicorn app.web:app --reload --port 8000
```

Ein frisch migrierter Stand hat weder Shops noch Angebote. Ohne Shop-Profile
kann der Optimierer nichts rechnen, Szenarien bleiben dann leer — das ist kein
Fehler, sondern fehlende Datengrundlage.

### 4. Tests

```sh
.venv/Scripts/python -m pytest
```

Zwei Dinge, die sonst Zeit kosten:

- **Aus dem Repo-Wurzelverzeichnis starten.** Mehrere Tests lesen Fixtures über
  relative Pfade wie `static/job-matrix.js`; aus einem Unterordner scheitern sie.
- **Keine Datenbank nötig.** Die Tests arbeiten durchgehend mit Fake-Connection-
  Objekten. Wer eine DB erwartet und keine findet, sucht am falschen Ende.

Adapter-Tests laufen gegen gemockte HTTP-Antworten und rufen **nie** einen
echten Shop auf.

#### Bekannte Schwachstelle: Fake-Repository-Drift

Weil die Tests mit Fake-Repositories arbeiten, können sie eine ganze Fehlerklasse
nicht sehen: Ein Fake liefert einen Schlüssel *immer* mit, auch wenn die echte
SQL-Abfrage die Spalte gar nicht selektiert. So blieb einmal
`plattform_geprueft_am` aus `optimization_input` unbemerkt — mit der Folge, dass
der Füllknopf niemals hätte verschwinden können.

Als Sofortmassnahme prüft ein Test die *Spaltenliste* dieser einen Abfrage
(`test_optimization_input_selects_the_columns_the_cart_handover_needs`). Das
schliesst den konkreten Fall, nicht die Klasse.

Die Klasse hat danach zweimal erneut zugeschlagen — `shop.land` war im Schema
noch auf `'CH'` festgenagelt, und `create_shop` verwarf `lieferziel_id` im
INSERT. Beides sah kein Fake, beides fand erst der Live-Versuch.

Deshalb gibt es jetzt `tests/test_repository_integration.py`: ein dünner
Integrationslayer, der eine Wegwerf-Datenbank anlegt, die Migrationen fährt,
echte Rows schreibt und zurückliest — und sie danach wieder abräumt. Ohne
erreichbares Postgres überspringen sich diese Tests, die übrige Suite bleibt
also datenbankfrei.

```sh
# nutzt standardmässig die Dev-DB auf 5433; überschreibbar:
BESCHAFFUNG_TEST_DATABASE_URL=postgresql://... .venv/Scripts/python -m pytest tests/test_repository_integration.py
```

**Neue Repository-Spalten gehören dorthin**, nicht in weitere SQL-String-Tests.

### 5. E2E

Der MCP-Smoketest ist read-only und lässt sich per `BASE_URL` auf eine beliebige
Instanz richten:

```sh
BASE_URL=http://127.0.0.1:8000 .venv/Scripts/python tests/e2e/mcp_tools_call.py
```

Der Browser-Klickpfad (`tests/e2e/decision_click.mjs`) legt sich über
`/api/e2e/jobs` einen eigenen, als `[E2E-TEST]` markierten Wegwerf-Job an und
räumt ihn wieder ab. Er fasst reale Jobs nicht an.

## Shop adapters

Ein Shop-Adapter ist eine YAML-Datei mit CSS-Selektoren; die Engine holt die
Produktseite selbst und liest Name, Preis, Liefer- und Lagertext deterministisch
daraus, statt sie sich diktieren zu lassen. Gebündelte Adapter liegen in
`adapters/`, eigene in dem Verzeichnis, auf das `BESCHAFFUNG_ADAPTER_DIR` zeigt -
bei gleicher `id` gewinnt die eigene Datei. Schema, Grenzen und die Regeln, nach
denen gefetcht wird, stehen in `adapters/README.md`.

## Extension

Die Browser-Erweiterung unter `extension/` setzt das Gast-Session-Cookie im Shop
und öffnet den gefüllten Warenkorb — der Ein-Klick-Abschluss der Warenkorb-
Übergabe. Sie ist nötig, weil die Same-Origin-Policy die Job-Seite von der
Shop-Origin trennt; nur Code im Shop-Kontext darf dort ein Cookie setzen.

**Der Kopierflow bleibt vollständig bestehen.** Ohne Erweiterung ändert sich an
der Job-Seite nichts — sie ist der Fallback für jeden Browser.

### Laden

**Chromium-Familie** (Chrome, Edge, Brave):
`chrome://extensions` → Entwicklermodus → «Entpackte Erweiterung laden» →
`extension/` im Clone auswählen. Alternativ ohne Clone: `extension.zip` vom Tool
herunterladen (Link steht im Übergabe-Kasten), entpacken, denselben Weg.

**Firefox:** `about:debugging#/runtime/this-firefox` → «Temporäres Add-on
laden» → `extension/manifest.json` auswählen. Temporär geladene Add-ons
verschwinden beim Neustart und müssen dann erneut geladen werden.

Ein Codebase trägt beide: das Manifest führt `background.service_worker` für
Chromium und `background.scripts` für Firefox, der Code spricht die API über
`globalThis.browser ?? globalThis.chrome` an.

### Update

Clone: `git pull`, dann in `chrome://extensions` auf «Neu laden». Ohne Clone:
`extension.zip` neu herunterladen und die entpackte Erweiterung erneut laden.

`GET /extension.zip` zippt das `extension/`-Verzeichnis des **deployten** Standes
im Moment des Abrufs — der Download ist damit per Konstruktion immer das, was auf
dem Server liegt. Kein Build, kein CI, kein Hook. Die angezeigte Version kommt
aus `manifest.json`; sie wird bei jeder Änderung um einen Patch erhöht, damit auf
der Seite und in `chrome://extensions` sichtbar ist, welcher Stand geladen ist.

### Neuer Shop

`host_permissions` sind bewusst eng — Tool-Origin plus `*://*.bastelgarage.ch/*`,
kein `<all_urls>`. Cookie-Zugriff auf alle Domains wäre ein unnötig breiter
Radius für ein Werkzeug, das genau zwei Origins braucht.

Kommt ein Shop mit unterstützter Plattform dazu, kostet das:

1. eine Zeile in `host_permissions` (`*://*.neuer-shop.ch/*`),
2. einen Patch-Bump der `version` in `manifest.json`,
3. Erweiterung neu laden.

Bis dahin greift bei diesem Shop der Kopierflow — der Ein-Klick-Knopf schlägt
dort sonst mit einer Rechte-Fehlermeldung fehl.

## Konventionen

### Kodierung

Das gesamte Repo ist UTF-8, und die Tests prüfen deutsche Oberflächentexte
wörtlich. Jeder Datei-Zugriff muss die Kodierung deshalb explizit angeben:

```python
Path("static/job-matrix.js").read_text(encoding="utf-8")
```

Ohne das Argument nimmt Python die *Locale*-Kodierung — UTF-8 auf dem Linux-
Deployziel, `cp1252` unter Windows. Code ohne explizite Angabe läuft auf dem
Server durch und fällt lokal um.

### Sichtbare Meldungen und E2E

Ändert sich der Wortlaut oder die Form einer Meldung, die die Oberfläche zeigt,
werden die E2E-Erwartungen **im selben Commit** mitgezogen. Sonst steht der
Klickpfad rot, obwohl die Anwendung richtig ist — genau das ist bei der
Netto/Brutto-Korrektur passiert, die den Diff von einer Summenzeile auf
Pro-Position-Zeilen umgestellt hat.

Lässt sich der Lauf nicht selbst fahren (Playwright liegt in CT 103), gehört in
die Meldung der Satz **«E2E-Erwartung angepasst, Lauf ausstehend»** — angepasst
zählt nicht als bestanden, nur gelaufen zählt.

### Migrationen

Migrationen sind additiv — kein `DROP`, kein `DELETE`. Die Tests in
`tests/test_migrations.py` erzwingen das.

Genauer gilt eine dreistufige Regel, weil «additiv» nicht jeden Fall trifft:

| Stufe | Beispiel | Vorgehen |
| ----- | -------- | -------- |
| **Rein additiv** | neue Spalte, neue Tabelle, neuer Index | normale Kadenz, ohne Rückfrage |
| **Erweiternd** | Constraint-Tausch, der nur erlaubt was vorher verboten war; `DROP NOT NULL` | normale Kadenz, aber **ausdrücklich melden** (so geschehen bei 013) |
| **Verengend oder potenziell datenverlierend** | Spalte entfernen, Constraint verschärfen, Daten löschen oder umschreiben | **vorher fragen**, nie direkt deployen |

**Vor jedem Deploy, der eine Migration enthält, sind die Integrationstests
Pflicht:**

```sh
.venv/Scripts/python -m pytest tests/test_repository_integration.py
```

Sie fahren die Migrationen auf einer leeren Wegwerf-Datenbank und lesen danach
echte Rows zurück. Genau dort fällt auf, wenn eine Migration auf grüner Wiese
scheitert oder eine Spalte nie geschrieben wird — beides ist schon passiert.

## Lizenz

Dieses Projekt steht unter der Lizenz AGPL-3.0-only; siehe `LICENSE`.

Forks und fremde Instanzen bitte nicht unter dem Namen dieses Projekts betreiben.
