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
                        mindestbestellwert_chf, lieferzeit_default_tage
                    ) VALUES (%(name)s, %(url)s, %(domain)s, %(land)s,
                              %(versand_chf)s, %(gratis_ab_chf)s,
                              %(mindestbestellwert_chf)s, %(lieferzeit_default_tage)s)
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
                            preis_chf, lieferzeit_tage, lieferzeit_text, lager_text, lager
                        ) VALUES (%(line_id)s, %(shop_id)s, %(produktname)s,
                                  %(produkt_url)s, %(quelle_url)s, %(preis_chf)s,
                                  %(lieferzeit_tage)s, %(lieferzeit_text)s,
                                  %(lager_text)s, %(lager)s)
                        ON CONFLICT (line_id, produkt_url, beobachtungstag) DO UPDATE SET
                            shop_id = EXCLUDED.shop_id,
                            produktname = EXCLUDED.produktname,
                            quelle_url = EXCLUDED.quelle_url,
                            preis_chf = EXCLUDED.preis_chf,
                            lieferzeit_tage = EXCLUDED.lieferzeit_tage,
                            lieferzeit_text = EXCLUDED.lieferzeit_text,
                            lager_text = EXCLUDED.lager_text,
                            lager = EXCLUDED.lager,
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

    def optimization_input(self, job_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            required_rows = connection.execute(
                """
                SELECT id, position, suchtext FROM bom_line
                WHERE job_id = %s AND status NOT IN ('bestand', 'nichts_gefunden', 'erledigt')
                ORDER BY position
                """,
                (job_id,),
            ).fetchall()
            required_line_ids = [row["id"] for row in required_rows]
            offers = connection.execute(
                """
                SELECT DISTINCT ON (o.line_id, o.produkt_url)
                       o.id, o.line_id, o.shop_id, o.preis_chf,
                       o.lieferzeit_tage, o.lieferzeit_text,
                       o.produktname, o.produkt_url, o.gesehen_am,
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
                    "lines": [dict(row) for row in required_rows],
                }
            shops = connection.execute(
                """
                SELECT id, name, url, versand_chf, gratis_ab_chf,
                       mindestbestellwert_chf, lieferzeit_default_tage
                FROM shop WHERE id = ANY(%s)
                """,
                (shop_ids,),
            ).fetchall()
            return {
                "offers": [dict(row) for row in offers],
                "shops": [dict(row) for row in shops],
                "required_line_ids": required_line_ids,
                "lines": [dict(row) for row in required_rows],
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
                       mindestbestellwert_chf, lieferzeit_default_tage, status
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
