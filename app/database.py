from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .jobs import BomInputLine


def decode_database_value(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


def decoded_dict_row(cursor):
    """Return dict rows and normalize SQL_ASCII text values to Unicode.

    PostgreSQL databases using SQL_ASCII expose textual columns as bytes in
    psycopg. The application schema has no binary columns, so decoding here
    keeps validation and JSON serialization consistent at the DB boundary.
    """
    make_row = dict_row(cursor)

    def decode_row(values):
        return {
            key: decode_database_value(value)
            for key, value in make_row(values).items()
        }

    return decode_row


class PostgresRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=decoded_dict_row)

    def create_job(self, source_text: str, lines: list[BomInputLine]) -> int:
        with self._connect() as connection:
            with connection.transaction():
                job = connection.execute(
                    "INSERT INTO job(quelltext) VALUES (%s) RETURNING id",
                    (source_text,),
                ).fetchone()
                job_id = int(job["id"])
                for line in lines:
                    connection.execute(
                        """
                        INSERT INTO bom_line(
                            job_id, position, originaltext, suchtext, menge
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (job_id, line.position, line.originaltext, line.suchtext, line.menge),
                    )
        return job_id

    def create_e2e_test_job(self) -> dict[str, Any]:
        """Create one isolated matrix/browser-test graph with no real-job writes."""
        with self._connect() as connection:
            with connection.transaction():
                shops = connection.execute(
                    """
                    SELECT id FROM shop
                    WHERE status <> 'gesperrt'
                    ORDER BY id LIMIT 3
                    """
                ).fetchall()
                if len(shops) < 3:
                    raise ValueError("Für den Matrix-E2E-Test sind drei Shops erforderlich")
                job = connection.execute(
                    """
                    INSERT INTO job(quelltext, status, is_test)
                    VALUES ('[E2E-TEST] Matrixfluss - automatisch entsorgen', 'in_arbeit', TRUE)
                    RETURNING id
                    """
                ).fetchone()
                lines = []
                for position in range(1, 4):
                    lines.append(
                        connection.execute(
                            """
                            INSERT INTO bom_line(
                                job_id, position, originaltext, suchtext, menge, status
                            ) VALUES (%s, %s, %s, %s, 1, 'kandidaten')
                            RETURNING id
                            """,
                            (
                                job["id"],
                                position,
                                f"[E2E-TEST] Matrixposition {position}",
                                f"[E2E-TEST] Matrixposition {position}",
                            ),
                        ).fetchone()
                    )
                layout = [
                    (0, 0, "A langsam", "1.00", 4),
                    (0, 2, "C schnell", "5.00", 1),
                    (1, 1, "B langsam", "1.00", 4),
                    (1, 2, "C schnell zwei", "5.00", 1),
                    (2, 0, "A Abschluss", "1.00", 4),
                    (2, 1, "B Abschluss", "1.20", 3),
                ]
                offers = []
                for line_index, shop_index, product, price, days in layout:
                    url = (
                        f"https://e2e.invalid/jobs/{job['id']}/"
                        f"lines/{line_index + 1}/shops/{shop_index + 1}"
                    )
                    offers.append(
                        connection.execute(
                            """
                            INSERT INTO offer(
                                line_id, shop_id, produktname, produkt_url, quelle_url,
                                preis_chf, lieferzeit_tage, lieferzeit_text, lager, lager_text
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                      'E2E-Testbestand', 'E2E-Testbestand')
                            RETURNING id
                            """,
                            (
                                lines[line_index]["id"],
                                shops[shop_index]["id"],
                                f"[E2E-TEST] {product}",
                                url,
                                url,
                                price,
                                days,
                                f"{days} Testtage ab Testlager",
                            ),
                        ).fetchone()
                    )
                return {
                    "job_id": int(job["id"]),
                    "offer_id": int(offers[0]["id"]),
                    "offer_ids": [int(row["id"]) for row in offers],
                    "line_ids": [int(row["id"]) for row in lines],
                    "marker": "[E2E-TEST]",
                }

    def delete_e2e_test_job(self, job_id: int) -> dict[str, Any]:
        """Delete only a job carrying the database test marker and its artifacts."""
        with self._connect() as connection:
            with connection.transaction():
                job = connection.execute(
                    "SELECT id, is_test FROM job WHERE id = %s FOR UPDATE", (job_id,)
                ).fetchone()
                if job is None or not job["is_test"]:
                    raise ValueError("Nur markierte Test-Jobs dürfen gelöscht werden")
                connection.execute(
                    """
                    DELETE FROM purchase_item WHERE purchase_id IN (
                        SELECT id FROM purchase WHERE job_id = %s
                    )
                    """,
                    (job_id,),
                )
                connection.execute("DELETE FROM purchase WHERE job_id = %s", (job_id,))
                connection.execute(
                    """
                    DELETE FROM decision WHERE offer_id IN (
                        SELECT o.id FROM offer o
                        JOIN bom_line bl ON bl.id = o.line_id
                        WHERE bl.job_id = %s
                    )
                    """,
                    (job_id,),
                )
                connection.execute(
                    "DELETE FROM offer WHERE line_id IN (SELECT id FROM bom_line WHERE job_id = %s)",
                    (job_id,),
                )
                connection.execute("DELETE FROM bom_line WHERE job_id = %s", (job_id,))
                connection.execute("DELETE FROM job WHERE id = %s AND is_test", (job_id,))
                return {"job_id": job_id, "deleted": True}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            job = connection.execute(
                """
                SELECT id, status, quelltext, erstellt_am, aktualisiert_am
                FROM job WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                return None
            lines = connection.execute(
                """
                SELECT id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE job_id = %s ORDER BY position
                """,
                (job_id,),
            ).fetchall()
            result = dict(job)
            result["lines"] = [dict(line) for line in lines]
            return result

    def next_job(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            job = connection.execute(
                """
                SELECT j.id, j.status, j.quelltext, j.erstellt_am
                FROM job j
                WHERE j.status IN ('offen', 'in_arbeit')
                  AND NOT j.is_test
                  AND EXISTS (
                    SELECT 1 FROM bom_line bl
                    WHERE bl.job_id = j.id AND bl.status IN ('offen', 'in_arbeit')
                  )
                ORDER BY j.erstellt_am, j.id
                LIMIT 1
                """
            ).fetchone()
            if job is None:
                return None
            lines = connection.execute(
                """
                SELECT id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line
                WHERE job_id = %s AND status IN ('offen', 'in_arbeit')
                ORDER BY position
                """,
                (job["id"],),
            ).fetchall()
            result = dict(job)
            result["lines"] = [dict(line) for line in lines]
            return result

    def get_line(self, line_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, job_id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE id = %s
                """,
                (line_id,),
            ).fetchone()
            return dict(row) if row else None

    def check_line(self, line_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            line = connection.execute(
                """
                SELECT id, job_id, position, originaltext, suchtext, menge, status, kommentar
                FROM bom_line WHERE id = %s
                """,
                (line_id,),
            ).fetchone()
            if line is None:
                return None
            stock = connection.execute(
                """
                SELECT id, bezeichnung, menge, einheit, aktualisiert_am
                FROM stock
                WHERE menge > 0 AND lower(bezeichnung) = lower(CAST(%s AS TEXT))
                ORDER BY aktualisiert_am DESC
                """,
                (line["suchtext"],),
            ).fetchall()
            previous = connection.execute(
                """
                SELECT p.id AS purchase_id, p.bestellt_am, p.angekommen_am,
                       pi.menge, pi.einzelpreis_chf, o.produktname, o.produkt_url,
                       s.id AS shop_id, s.name AS shop_name
                FROM purchase_item pi
                JOIN purchase p ON p.id = pi.purchase_id
                JOIN bom_line old_line ON old_line.id = pi.line_id
                JOIN offer o ON o.id = pi.offer_id
                JOIN shop s ON s.id = o.shop_id
                WHERE lower(old_line.suchtext) = lower(CAST(%s AS TEXT))
                ORDER BY p.bestellt_am DESC
                LIMIT 10
                """,
                (line["suchtext"],),
            ).fetchall()
            cached = connection.execute(
                """
                SELECT o.id, o.produktname, o.produkt_url, o.preis_chf,
                       o.lieferzeit_tage, o.lieferzeit_text, o.lager_text,
                       o.lager, o.gesehen_am,
                       s.id AS shop_id, s.name AS shop_name, s.status AS shop_status
                FROM offer o
                JOIN bom_line cached_line ON cached_line.id = o.line_id
                JOIN shop s ON s.id = o.shop_id
                WHERE lower(cached_line.suchtext) = lower(CAST(%s AS TEXT))
                  AND o.gesehen_am >= NOW() - INTERVAL '14 days'
                  AND s.status <> 'gesperrt'
                ORDER BY o.gesehen_am DESC
                LIMIT 20
                """,
                (line["suchtext"],),
            ).fetchall()
            return {
                "line": dict(line),
                "stock": [dict(row) for row in stock],
                "previous_purchases": [dict(row) for row in previous],
                "cached_offers": [dict(row) for row in cached],
            }

    def create_shop(self, **values: Any) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    INSERT INTO shop(
                        name, url, domain, land, versand_chf, gratis_ab_chf,
                        mindestbestellwert_chf, lieferzeit_default_tage,
                        profil_quelle_url, versand_text
                    ) VALUES (%(name)s, %(url)s, %(domain)s, %(land)s,
                              %(versand_chf)s, %(gratis_ab_chf)s,
                              %(mindestbestellwert_chf)s, %(lieferzeit_default_tage)s,
                              %(profil_quelle_url)s, %(versand_text)s)
                    RETURNING *
                    """,
                    values,
                ).fetchone()
                return dict(row)
        except UniqueViolation as error:
            raise ValueError("Shop-Name oder Domain ist bereits bekannt") from error

    def get_shop(self, shop_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM shop WHERE id = %s", (shop_id,)).fetchone()
            return dict(row) if row else None

    def update_shop_profile(self, shop_id: int, **values: Any) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE shop SET
                    versand_chf = %(versand_chf)s,
                    gratis_ab_chf = %(gratis_ab_chf)s,
                    mindestbestellwert_chf = %(mindestbestellwert_chf)s,
                    lieferzeit_default_tage = %(lieferzeit_default_tage)s,
                    profil_quelle_url = %(profil_quelle_url)s,
                    versand_text = %(versand_text)s
                WHERE id = %(shop_id)s
                RETURNING *
                """,
                {"shop_id": shop_id, **values},
            ).fetchone()
            if row is None:
                raise ValueError(f"Shop {shop_id} ist unbekannt")
            return dict(row)

    def save_shop_platform(
        self, shop_id: int, plattform: str | None, plattform_beleg: str
    ) -> dict[str, Any]:
        """Ergebnis einer ABGESCHLOSSENEN Plattform-Erkennung festhalten.

        ``plattform=None`` ist ein gültiges Ergebnis und heisst "geprüft, nichts
        Bekanntes gefunden" - zusammen mit ``plattform_geprueft_am`` macht das
        den Unterschied zu "noch nie geprüft". Der Beleg ist immer Pflicht: er
        hält fest, was gesehen wurde, auch im negativen Fall.

        Der Aufrufer darf diese Methode nur nach einer abgeschlossenen Erkennung
        aufrufen. Ein Timeout ist kein Ergebnis und schreibt nichts.
        """
        if not plattform_beleg or not plattform_beleg.strip():
            raise ValueError("Plattform darf nicht ohne Beleg gespeichert werden")
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE shop SET
                    plattform = %s,
                    plattform_beleg = %s,
                    plattform_geprueft_am = NOW()
                WHERE id = %s
                RETURNING id, plattform, plattform_beleg
                """,
                (plattform, plattform_beleg.strip(), shop_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Shop {shop_id} ist unbekannt")
            return dict(row)

    def is_test_job(self, job_id: int) -> bool:
        """Nur für Gatter um E2E-Wege.

        Bewusst eine eigene schmale Abfrage statt is_test in get_job: das würde
        die Antwortform von GET /api/jobs/{id} und des MCP-Tools get_job ändern.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT is_test FROM job WHERE id = %s", (job_id,)
            ).fetchone()
            return bool(row and row["is_test"])

    def save_offer_product_ids(self, produkt_ids: dict[int, str]) -> int:
        """Shopinterne Produkt-IDs cachen. Quelle ist jeweils die Produktseite."""
        if not produkt_ids:
            return 0
        with self._connect() as connection:
            with connection.transaction():
                for offer_id, produkt_id in produkt_ids.items():
                    connection.execute(
                        "UPDATE offer SET shop_produkt_id = %s WHERE id = %s",
                        (str(produkt_id), int(offer_id)),
                    )
        return len(produkt_ids)

    def list_lieferziele(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT z.id, z.name, z.adresse, z.land, z.waehrung,
                       z.aufschlag_chf, z.zuschlag_tage,
                       (SELECT count(*) FROM shop s WHERE s.lieferziel_id = z.id) AS shop_count
                FROM lieferziel z ORDER BY z.land, z.name
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_lieferziel(self, lieferziel_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM lieferziel WHERE id = %s", (lieferziel_id,)
            ).fetchone()
            return dict(row) if row else None

    def lieferziele_fuer_land(self, land: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lieferziel WHERE land = %s ORDER BY id", (land,)
            ).fetchall()
            return [dict(row) for row in rows]

    def create_lieferziel(self, **values: Any) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    INSERT INTO lieferziel(
                        name, adresse, land, waehrung, aufschlag_chf, zuschlag_tage
                    ) VALUES (%(name)s, %(adresse)s, %(land)s, %(waehrung)s,
                              %(aufschlag_chf)s, %(zuschlag_tage)s)
                    RETURNING *
                    """,
                    values,
                ).fetchone()
                return dict(row)
        except UniqueViolation as error:
            raise ValueError(f"Lieferadresse «{values['name']}» gibt es schon") from error

    def update_lieferziel(self, lieferziel_id: int, **values: Any) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE lieferziel SET
                    adresse = %(adresse)s,
                    waehrung = %(waehrung)s,
                    aufschlag_chf = %(aufschlag_chf)s,
                    zuschlag_tage = %(zuschlag_tage)s
                WHERE id = %(lieferziel_id)s
                RETURNING *
                """,
                {"lieferziel_id": lieferziel_id, **values},
            ).fetchone()
            if row is None:
                raise ValueError(f"Lieferadresse {lieferziel_id} ist unbekannt")
            return dict(row)

    def get_kurs(self, waehrung: str) -> dict[str, Any] | None:
        """Neuesten bekannten Kurs einer Währung lesen."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT waehrung, kurs, geholt_am, quelle_url
                FROM kurs WHERE waehrung = %s
                ORDER BY geholt_am DESC LIMIT 1
                """,
                (waehrung,),
            ).fetchone()
            return dict(row) if row else None

    def save_kurs(
        self, waehrung: str, kurs: Any, geholt_am: Any, quelle_url: str
    ) -> dict[str, Any]:
        """Tageskurs samt Quelle festhalten; ein Kurs pro Währung und Tag."""
        if not quelle_url or not str(quelle_url).strip():
            raise ValueError("Kurs darf nicht ohne Quelle gespeichert werden")
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO kurs(waehrung, kurs, geholt_am, quelle_url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (waehrung, geholt_am) DO UPDATE SET
                    kurs = EXCLUDED.kurs,
                    quelle_url = EXCLUDED.quelle_url
                RETURNING waehrung, kurs, geholt_am, quelle_url
                """,
                (waehrung, str(kurs), geholt_am, str(quelle_url).strip()),
            ).fetchone()
            return dict(row)

    def save_offer_artikelnummern(self, artikelnummern: dict[int, str]) -> int:
        """Shopinterne Artikelnummern cachen. Quelle ist die Produktseite.

        Sie ist der sprachunabhängige Anker der Korb-Verifikation; die
        produkt_url bleibt daneben als Provenienz stehen.
        """
        if not artikelnummern:
            return 0
        with self._connect() as connection:
            with connection.transaction():
                for offer_id, nummer in artikelnummern.items():
                    if not str(nummer).strip():
                        continue
                    connection.execute(
                        "UPDATE offer SET artikelnummer = %s WHERE id = %s",
                        (str(nummer).strip(), int(offer_id)),
                    )
        return len(artikelnummern)

    def create_offer(self, **values: Any) -> dict[str, Any]:
        if values.get("lieferzeit_tage") is not None and not values.get("lieferzeit_text"):
            raise ValueError(
                "Lieferzeit darf nicht ohne wörtlichen Originaltext der Produktseite gesetzt sein"
            )
        try:
            with self._connect() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO offer(
                            line_id, shop_id, produktname, produkt_url, quelle_url,
                            preis_chf, lieferzeit_tage, lieferzeit_text, lager_text, lager,
                            artikelnummer, preis_original, waehrung, kurs, kurs_am, kurs_quelle
                        ) VALUES (%(line_id)s, %(shop_id)s, %(produktname)s,
                                  %(produkt_url)s, %(quelle_url)s, %(preis_chf)s,
                                  %(lieferzeit_tage)s, %(lieferzeit_text)s,
                                  %(lager_text)s, %(lager)s, %(artikelnummer)s,
                                  %(preis_original)s, %(waehrung)s, %(kurs)s,
                                  %(kurs_am)s, %(kurs_quelle)s)
                        ON CONFLICT (line_id, produkt_url, beobachtungstag) DO UPDATE SET
                            shop_id = EXCLUDED.shop_id,
                            produktname = EXCLUDED.produktname,
                            quelle_url = EXCLUDED.quelle_url,
                            preis_chf = EXCLUDED.preis_chf,
                            lieferzeit_tage = EXCLUDED.lieferzeit_tage,
                            lieferzeit_text = EXCLUDED.lieferzeit_text,
                            lager_text = EXCLUDED.lager_text,
                            lager = EXCLUDED.lager,
                            artikelnummer = COALESCE(EXCLUDED.artikelnummer, offer.artikelnummer),
                            preis_original = EXCLUDED.preis_original,
                            waehrung = EXCLUDED.waehrung,
                            kurs = EXCLUDED.kurs,
                            kurs_am = EXCLUDED.kurs_am,
                            kurs_quelle = EXCLUDED.kurs_quelle,
                            gesehen_am = NOW()
                        RETURNING *
                        """,
                        values,
                    ).fetchone()
                    connection.execute(
                        "UPDATE bom_line SET status = 'kandidaten' WHERE id = %s",
                        (values["line_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE job SET status = 'in_arbeit', aktualisiert_am = NOW()
                        WHERE id = (SELECT job_id FROM bom_line WHERE id = %s)
                        """,
                        (values["line_id"],),
                    )
                    return dict(row)
        except UniqueViolation as error:
            raise ValueError("Dieses Angebot ist fuer die Zeile bereits erfasst") from error

    def mark_line(self, line_id: int, status: str, kommentar: str | None) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.transaction():
                if status == "bestand":
                    line = connection.execute(
                        "SELECT suchtext, menge FROM bom_line WHERE id = %s FOR UPDATE",
                        (line_id,),
                    ).fetchone()
                    stock_rows = connection.execute(
                        """
                        SELECT id, menge FROM stock
                        WHERE lower(bezeichnung) = lower(CAST(%s AS TEXT)) AND menge > 0
                        ORDER BY aktualisiert_am, id FOR UPDATE
                        """,
                        (line["suchtext"],),
                    ).fetchall()
                    if sum(row["menge"] for row in stock_rows) < line["menge"]:
                        raise ValueError("Bestand deckt die benoetigte Menge nicht")
                    remaining = line["menge"]
                    for stock_row in stock_rows:
                        taken = min(remaining, stock_row["menge"])
                        connection.execute(
                            "UPDATE stock SET menge = menge - %s, aktualisiert_am = NOW() WHERE id = %s",
                            (taken, stock_row["id"]),
                        )
                        remaining -= taken
                        if remaining == 0:
                            break
                row = connection.execute(
                    """
                    UPDATE bom_line SET status = %s, kommentar = %s
                    WHERE id = %s RETURNING id, status, kommentar
                    """,
                    (status, kommentar, line_id),
                ).fetchone()
                return dict(row)

    def save_job_selection(
        self, job_id: int, assignments: dict[str, int]
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE job
                SET selected_assignments = %s, aktualisiert_am = NOW()
                WHERE id = %s
                RETURNING id, selected_assignments
                """,
                (Jsonb(assignments), job_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Job {job_id} ist unbekannt")
            return dict(row)

    def optimization_input(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            job = connection.execute(
                "SELECT status, selected_assignments FROM job WHERE id = %s",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError(f"Job {job_id} ist unbekannt")
            line_rows = connection.execute(
                """
                SELECT id, position, suchtext, menge, status, kommentar FROM bom_line
                WHERE job_id = %s
                ORDER BY position
                """,
                (job_id,),
            ).fetchall()
            required_line_ids = [
                row["id"]
                for row in line_rows
                if row["status"] not in {"bestand", "nichts_gefunden", "erledigt"}
            ]
            offers = connection.execute(
                """
                SELECT DISTINCT ON (o.line_id, o.produkt_url)
                       o.id, o.line_id, o.shop_id, o.preis_chf,
                       o.lieferzeit_tage, o.lieferzeit_text, o.lager_text,
                       o.produktname, o.produkt_url, o.quelle_url, o.gesehen_am,
                       o.shop_produkt_id, o.artikelnummer,
                       o.preis_original, o.waehrung, o.kurs, o.kurs_am, o.kurs_quelle,
                       bl.menge, bl.suchtext, bl.position,
                       d.override_status
                FROM offer o
                JOIN bom_line bl ON bl.id = o.line_id
                LEFT JOIN decision d ON d.offer_id = o.id
                JOIN shop s ON s.id = o.shop_id
                WHERE bl.job_id = %s AND s.status <> 'gesperrt'
                ORDER BY o.line_id, o.produkt_url, o.gesehen_am DESC, o.id DESC
                """,
                (job_id,),
            ).fetchall()
            shop_ids = sorted({row["shop_id"] for row in offers})
            if not shop_ids:
                return {
                    "offers": [],
                    "shops": [],
                    "required_line_ids": required_line_ids,
                    "lines": [dict(row) for row in line_rows],
                    "selected_assignments": job.get("selected_assignments"),
                    "job_status": job["status"],
                }
            shops = connection.execute(
                """
                SELECT s.id, s.name, s.url, s.versand_chf, s.gratis_ab_chf,
                       s.mindestbestellwert_chf, s.lieferzeit_default_tage,
                       s.plattform, s.plattform_beleg, s.plattform_geprueft_am,
                       s.lieferziel_id,
                       z.name AS lieferziel_name, z.land AS lieferziel_land,
                       z.waehrung AS lieferziel_waehrung,
                       z.aufschlag_chf AS lieferziel_aufschlag_chf,
                       z.zuschlag_tage AS lieferziel_zuschlag_tage
                FROM shop s LEFT JOIN lieferziel z ON z.id = s.lieferziel_id
                WHERE s.id = ANY(%s)
                """,
                (shop_ids,),
            ).fetchall()
            return {
                "offers": [dict(row) for row in offers],
                "shops": [dict(row) for row in shops],
                "required_line_ids": required_line_ids,
                "lines": [dict(row) for row in line_rows],
                "selected_assignments": job.get("selected_assignments"),
                "job_status": job["status"],
            }

    def create_purchase(
        self,
        job_id: int,
        variant: dict[str, Any],
        ordered_at: datetime,
        promised_days: dict[str, int],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.transaction():
                existing = connection.execute(
                    "SELECT id FROM purchase WHERE job_id = %s",
                    (job_id,),
                ).fetchone()
                if existing:
                    raise ValueError(f"Job {job_id} wurde bereits als Kauf erfasst")
                purchase = connection.execute(
                    """
                    INSERT INTO purchase(
                        job_id, variante, total_chf, bestellt_am, zugesagt_liefertage
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        job_id,
                        Jsonb(variant),
                        Decimal(variant["total_chf"]),
                        ordered_at,
                        Jsonb(promised_days),
                    ),
                ).fetchone()
                for line_id_text, offer_id in variant["assignments"].items():
                    line_id = int(line_id_text)
                    item = connection.execute(
                        """
                        SELECT bl.menge, o.preis_chf
                        FROM bom_line bl JOIN offer o ON o.id = %s AND o.line_id = bl.id
                        WHERE bl.id = %s AND bl.job_id = %s
                        """,
                        (offer_id, line_id, job_id),
                    ).fetchone()
                    if item is None:
                        raise ValueError("Variante enthaelt ein ungueltiges Angebot")
                    connection.execute(
                        """
                        INSERT INTO purchase_item(
                            purchase_id, line_id, offer_id, menge, einzelpreis_chf
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (purchase["id"], line_id, offer_id, item["menge"], item["preis_chf"]),
                    )
                connection.execute(
                    "UPDATE job SET status = 'bestellt', aktualisiert_am = NOW() WHERE id = %s",
                    (job_id,),
                )
                return dict(purchase)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id, j.status, j.quelltext, j.erstellt_am,
                       COUNT(bl.id)::int AS line_count
                FROM job j LEFT JOIN bom_line bl ON bl.job_id = j.id
                WHERE NOT j.is_test
                GROUP BY j.id ORDER BY j.erstellt_am DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_job_detail(self, job_id: int) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id, o.line_id, o.produktname, o.produkt_url,
                       o.preis_chf, o.lieferzeit_tage, o.lieferzeit_text,
                       o.lager_text, o.lager, o.gesehen_am,
                       s.id AS shop_id, s.name AS shop_name, s.status AS shop_status,
                       s.lieferzeit_default_tage, d.override_status AS decision
                FROM offer o JOIN shop s ON s.id = o.shop_id
                LEFT JOIN decision d ON d.offer_id = o.id
                JOIN bom_line bl ON bl.id = o.line_id
                WHERE bl.job_id = %s ORDER BY o.gesehen_am DESC
                """,
                (job_id,),
            ).fetchall()
        by_line: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_line.setdefault(row["line_id"], []).append(dict(row))
        for line in job["lines"]:
            line["offers"] = by_line.get(line["id"], [])
        return job

    def record_decision(self, offer_id: int, status: str) -> dict[str, Any]:
        if status not in {"pin", "exclude", "neutral"}:
            raise ValueError("Override muss pin, exclude oder neutral sein")
        with self._connect() as connection:
            with connection.transaction():
                offer = connection.execute(
                    "SELECT line_id FROM offer WHERE id = %s", (offer_id,)
                ).fetchone()
                if offer is None:
                    raise ValueError(f"Angebot {offer_id} ist unbekannt")
                line_id = offer["line_id"]
                if status == "pin":
                    connection.execute(
                        """
                        UPDATE decision SET override_status = NULL, entschieden_am = NOW()
                        WHERE line_id = %s AND override_status = 'pin' AND offer_id <> %s
                        """,
                        (line_id, offer_id),
                    )
                if status == "neutral":
                    connection.execute(
                        """
                        UPDATE decision SET override_status = NULL, entschieden_am = NOW()
                        WHERE offer_id = %s
                        """,
                        (offer_id,),
                    )
                    return {"offer_id": offer_id, "status": "neutral"}
                legacy_status = "bestaetigt" if status == "pin" else "verworfen"
                row = connection.execute(
                    """
                    INSERT INTO decision(line_id, offer_id, status, override_status)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (offer_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        override_status = EXCLUDED.override_status,
                        entschieden_am = NOW()
                    RETURNING offer_id, override_status AS status
                    """,
                    (line_id, offer_id, legacy_status, status),
                ).fetchone()
                return dict(row)

    def search_history(self, text: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.id AS purchase_id, p.bestellt_am,
                       p.zugesagt_liefertage AS zugesagt_liefertage_pro_shop,
                       p.angekommen_am, pi.menge, pi.einzelpreis_chf,
                       o.produktname, o.produkt_url,
                       s.id AS shop_id, s.name AS shop_name
                FROM purchase_item pi
                JOIN purchase p ON p.id = pi.purchase_id
                JOIN offer o ON o.id = pi.offer_id
                JOIN shop s ON s.id = o.shop_id
                WHERE o.produktname ILIKE '%%' || CAST(%s AS TEXT) || '%%'
                   OR s.name ILIKE '%%' || CAST(%s AS TEXT) || '%%'
                ORDER BY p.bestellt_am DESC, pi.id DESC
                LIMIT 100
                """,
                (text, text),
            ).fetchall()
        result = []
        for source in rows:
            row = dict(source)
            promised = row.pop("zugesagt_liefertage_pro_shop") or {}
            shop_id = row["shop_id"]
            row["zugesagt_liefertage"] = promised.get(str(shop_id), promised.get(shop_id))
            result.append(row)
        return result

    def get_stock(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, bezeichnung, menge, einheit, aktualisiert_am
                FROM stock
                WHERE menge > 0
                ORDER BY lower(bezeichnung), id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_purchases(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            purchases = connection.execute(
                """
                SELECT id, job_id, total_chf, bestellt_am,
                       zugesagt_liefertage, angekommen_am
                FROM purchase ORDER BY bestellt_am DESC
                """
            ).fetchall()
            result = []
            for purchase in purchases:
                item_rows = connection.execute(
                    """
                    SELECT pi.menge, pi.einzelpreis_chf, o.produktname,
                           o.produkt_url, s.name AS shop_name
                    FROM purchase_item pi
                    JOIN offer o ON o.id = pi.offer_id
                    JOIN shop s ON s.id = o.shop_id
                    WHERE pi.purchase_id = %s ORDER BY pi.id
                    """,
                    (purchase["id"],),
                ).fetchall()
                item = dict(purchase)
                item["items"] = [dict(row) for row in item_rows]
                result.append(item)
            return result

    def repeat_purchase(self, purchase_id: int) -> int:
        with self._connect() as connection:
            source = connection.execute(
                """
                SELECT j.quelltext FROM purchase p
                JOIN job j ON j.id = p.job_id WHERE p.id = %s
                """,
                (purchase_id,),
            ).fetchone()
        if source is None:
            raise ValueError(f"Kauf {purchase_id} ist unbekannt")
        from .jobs import parse_bom

        new_job_id = self.create_job(source["quelltext"], parse_bom(source["quelltext"]))
        with self._connect() as connection:
            connection.execute(
                "UPDATE job SET wiederholt_von_purchase_id = %s WHERE id = %s",
                (purchase_id, new_job_id),
            )
        return new_job_id

    def mark_purchase_arrived(self, purchase_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            with connection.transaction():
                purchase = connection.execute(
                    "SELECT id, angekommen_am FROM purchase WHERE id = %s FOR UPDATE",
                    (purchase_id,),
                ).fetchone()
                if purchase is None:
                    raise ValueError(f"Kauf {purchase_id} ist unbekannt")
                if purchase["angekommen_am"] is None:
                    purchase = connection.execute(
                        "UPDATE purchase SET angekommen_am = NOW() WHERE id = %s RETURNING *",
                        (purchase_id,),
                    ).fetchone()
                    items = connection.execute(
                        """
                        SELECT pi.id, pi.menge, bl.suchtext
                        FROM purchase_item pi
                        JOIN bom_line bl ON bl.id = pi.line_id
                        WHERE pi.purchase_id = %s
                        """,
                        (purchase_id,),
                    ).fetchall()
                    for item in items:
                        connection.execute(
                            """
                            INSERT INTO stock(bezeichnung, menge, purchase_item_id)
                            VALUES (%s, %s, %s)
                            """,
                            (item["suchtext"], item["menge"], item["id"]),
                        )
                    connection.execute(
                        """
                        UPDATE job SET status = 'abgeschlossen', aktualisiert_am = NOW()
                        WHERE id = (SELECT job_id FROM purchase WHERE id = %s)
                        """,
                        (purchase_id,),
                    )
                return dict(purchase)

    def list_shops(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, url, domain, land, versand_chf, gratis_ab_chf,
                       mindestbestellwert_chf, lieferzeit_default_tage, status,
                       profil_quelle_url, versand_text
                FROM shop ORDER BY name
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def update_shop_status(self, shop_id: int, status: str) -> dict[str, Any]:
        if status not in {"bestaetigt", "gesperrt"}:
            raise ValueError("Shop-Status muss bestaetigt oder gesperrt sein")
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE shop SET status = %s WHERE id = %s RETURNING id, status",
                (status, shop_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Shop {shop_id} ist unbekannt")
            return dict(row)
