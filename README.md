# Beschaffung

LAN-Beschaffungstool für Linus.

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

`requirements-dev.txt` zieht `httpx2`. Das sieht nach Tippfehler aus, ist aber
korrekt: der `TestClient` von Starlette 1.6 nutzt `httpx2`, nicht `httpx`.

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

**Wenn die Klasse erneut zuschlägt, ist die Antwort kein weiterer
SQL-String-Test**, sondern ein dünner Integrationstest-Layer für die
Repository-Schicht gegen das Docker-Postgres: wenige Tests, die echte Rows
schreiben und lesen, statt Abfragetexte zu inspizieren.

### 5. E2E

Der MCP-Smoketest ist read-only und lässt sich per `BASE_URL` auf eine beliebige
Instanz richten:

```sh
BASE_URL=http://127.0.0.1:8000 .venv/Scripts/python tests/e2e/mcp_tools_call.py
```

Der Browser-Klickpfad (`tests/e2e/decision_click.mjs`) legt sich über
`/api/e2e/jobs` einen eigenen, als `[E2E-TEST]` markierten Wegwerf-Job an und
räumt ihn wieder ab. Er fasst reale Jobs nicht an.

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

### Commits

Keine Werkzeug-Attribution in Commits oder PRs: keine `Co-Authored-By`-Trailer,
keine «Generated with»-Zeilen, keine Emoji-Signaturen. Abgesichert über
`.claude/settings.json` und einen `commit-msg`-Hook. Der Hook liegt im Repo und
muss pro Clone einmal aktiviert werden:

```sh
git config core.hooksPath .githooks
```

### Migrationen

Migrationen sind additiv — kein `DROP`, kein `DELETE`. Die Tests in
`tests/test_migrations.py` erzwingen das.
